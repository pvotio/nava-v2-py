"""
worker.py – Playwright-based PDF renderer (queue & helpers)
"""
from __future__ import annotations

import os, json, uuid, asyncio, signal, logging, traceback, contextlib, importlib, re
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

from jinja2 import Environment, BaseLoader, select_autoescape
import sqlalchemy as sa
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.storage.blob.aio import BlobClient
from playwright.async_api import async_playwright

from deps import ASYNC_ENGINE

# ── Environment & config ────────────────────────────────────────────────
SB_NAMESPACE = os.getenv("SB_NAMESPACE")
SB_QUEUE     = os.getenv("SB_QUEUE")
STORAGE_URL  = os.getenv("STORAGE_URL")
PAYLOAD_CTN  = os.getenv("PAYLOAD_CONTAINER")
OUTPUT_CTN   = os.getenv("OUTPUT_CONTAINER", "pdfs")
CONCURRENCY  = int(os.getenv("WORKER_CONCURRENCY", "3"))
MAX_DELIVERY = int(os.getenv("MAX_DELIVERY", "5"))
AUDIT_TABLE  = os.getenv("PDF_AUDIT_TABLE", "PdfLog")

# Template path & validation
TPL_RE       = re.compile(r"[A-Za-z0-9_-]{1,64}$")
TEMPLATE_DIR = Path(os.getenv("SCRIPTS_DIR", "/opt/app/scripts")).resolve()

DEFAULT_PDF_OPTIONS = {
    "format": "A4",
    "landscape": False,
    "print_background": True,
    "prefer_css_page_size": True,
    "margin": {"top": "20mm", "bottom": "20mm", "left": "10mm", "right": "10mm"},
}

sem         = asyncio.Semaphore(CONCURRENCY)
stop_event  = asyncio.Event()
credential  = DefaultAzureCredential()
logger      = logging.getLogger("pdf-worker")
logging.basicConfig(level=logging.INFO, format="%(message)s")
BROWSER:   asyncio.AbstractAsyncContextManager | None = None
_active_tasks: set[asyncio.Task] = set()

_ts   = lambda: datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
_log  = lambda ev, **kv: logger.info(json.dumps({"ts": _ts(), "event": ev, **kv}))

# ── helpers ─────────────────────────────────────────────────────────────
def _load_template(name: str):
    if not TPL_RE.fullmatch(name):
        raise ValueError("invalid template name")
    html = (TEMPLATE_DIR / f"{name}.html").resolve()
    js   = (TEMPLATE_DIR / f"{name}.js").resolve()
    py   = (TEMPLATE_DIR / f"{name}.py").resolve()

    for p in (html, js, py):
        if p.exists() and not str(p).startswith(str(TEMPLATE_DIR)):
            raise ValueError("template path escape detected")

    mod = None
    if py.is_file():
        spec = importlib.util.spec_from_file_location(f"tpl_{name}", py)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[arg-type]

    if not html.is_file():
        raise FileNotFoundError(html)
    return mod, html, js if js.is_file() else None


async def _insert_log(run_id, payload_id, tpl, dur_ms, ok, err):
    stmt = sa.text(f"""
        INSERT INTO {AUDIT_TABLE}
               (id, template, payload_id, duration_ms, success, error_msg)
        VALUES (:run_id, :tpl, :pid, :dur, :succ, :err)
    """)
    async with ASYNC_ENGINE.begin() as conn:
        await conn.execute(
            stmt,
            dict(run_id=run_id, tpl=tpl, pid=payload_id,
                 dur=dur_ms, succ=ok, err=err)
        )

# ── core render routine ────────────────────────────────────────────────
async def _render_pdf(payload_id: str):
    async with sem:
        run_id, start = str(uuid.uuid4()), datetime.utcnow()
        tpl_name = "<unknown>"
        tmp_path = None
        try:
            # 1. Download payload JSON
            blob = BlobClient(account_url=STORAGE_URL,
                              container_name=PAYLOAD_CTN,
                              blob_name=payload_id,
                              credential=credential)
            payload = json.loads(await blob.download_blob().readall())
            tpl_name, params = payload["template"], payload.get("params", {})

            # 2. Load template + optional data fetch
            mod, html_path, js_path = _load_template(tpl_name)
            if mod and hasattr(mod, "Report"):
                report = mod.Report(params, ASYNC_ENGINE.sync_engine)  # type: ignore[arg-type]
                placeholders = await asyncio.get_running_loop().run_in_executor(None, report.fetch)
                params |= placeholders

            # 3. Render Jinja (auto-escaped)
            env = Environment(loader=BaseLoader(),
                              autoescape=select_autoescape(default_for_string=True))
            rendered = env.from_string(html_path.read_text()).render(**params)

            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as tmp:
                tmp.write(rendered)
                tmp_path = tmp.name

            # 4. Playwright
            page = await BROWSER.new_page()          # type: ignore[arg-type]
            await page.emulate_media(media="screen")

            # Optional helper module
            try:
                helper_mod = importlib.import_module(f"templates.{tpl_name.replace('-', '_')}_helper")
                await helper_mod.authenticate_blob_routes(page)
            except ModuleNotFoundError:
                helper_mod = None

            await page.goto(f"file://{tmp_path}", wait_until="networkidle")
            if js_path:
                await page.add_script_tag(path=str(js_path))
            await page.evaluate("(d)=>window.render && window.render(d)", params)

            if helper_mod:
                pdf_opts = {**DEFAULT_PDF_OPTIONS, **getattr(helper_mod, 'PDF_OPTIONS', {})}
                header   = await helper_mod.get_header_html(params)
                footer   = await helper_mod.get_footer_html(params)
            else:
                pdf_opts, header, footer = DEFAULT_PDF_OPTIONS, "", ""

            pdf_bytes = await page.pdf(**pdf_opts,
                                       header_template=header,
                                       footer_template=footer)
            await page.close()

            # 5. Upload PDF
            out_blob = BlobClient(account_url=STORAGE_URL,
                                  container_name=OUTPUT_CTN,
                                  blob_name=f"{payload_id}.pdf",
                                  credential=credential)
            await out_blob.upload_blob(pdf_bytes, overwrite=True, content_type="application/pdf")

            dur = int((datetime.utcnow() - start).total_seconds() * 1000)
            await _insert_log(run_id, payload_id, tpl_name, dur, True, None)
            _log("pdf.done", tpl=tpl_name, pid=payload_id, dur_ms=dur)

        except Exception as exc:
            dur = int((datetime.utcnow() - start).total_seconds() * 1000)
            await _insert_log(run_id, payload_id, tpl_name, dur, False, str(exc))
            _log("pdf.error", tpl=tpl_name, pid=payload_id, err=str(exc))
            traceback.print_exc()
            raise

        finally:
            if tmp_path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)

# ── queue consumer loop ────────────────────────────────────────────────
async def _handle_msg(receiver, msg):
    try:
        pid = msg.body.decode() if isinstance(msg.body, (bytes, bytearray)) else msg.body
        await _render_pdf(pid)
        await receiver.complete_message(msg)
    except Exception:
        if msg.delivery_count >= MAX_DELIVERY:
            await receiver.dead_letter_message(msg, reason="render-failed", error_description="max attempts")
        else:
            await receiver.abandon_message(msg)
    finally:
        _active_tasks.discard(asyncio.current_task())

async def _sb_consumer():
    async with ServiceBusClient(f"{SB_NAMESPACE}.servicebus.windows.net", credential=credential) as sb:
        receiver = sb.get_queue_receiver(
            SB_QUEUE,
            max_wait_time=5,
            max_auto_lock_renewal_duration=timedelta(minutes=10),
        )
        async with receiver:
            async for msg in receiver:
                if stop_event.is_set():
                    break
                task = asyncio.create_task(_handle_msg(receiver, msg))
                _active_tasks.add(task)
    if _active_tasks:
        await asyncio.gather(*_active_tasks, return_exceptions=True)

async def main():
    global BROWSER
    _log("worker.start", concurrency=CONCURRENCY)
    async with async_playwright() as p:
        BROWSER = await p.chromium.launch(args=["--no-sandbox"])
        consumer = asyncio.create_task(_sb_consumer())
        await stop_event.wait()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await BROWSER.close()
    _log("worker.stop")

for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: stop_event.set())

if __name__ == "__main__":
    asyncio.run(main())
