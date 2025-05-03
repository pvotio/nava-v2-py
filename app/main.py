"""
app/main.py – FastAPI edge  
Adds one-time, short-lived token links around the PDF-enqueue call.
"""
from __future__ import annotations

import os, sys, re, json, asyncio, hashlib, time, hmac, base64
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobClient
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

from auth import verify_jwt                       # local helper

app = FastAPI()

# ─── Configuration ───────────────────────────────────────────────────────
SB_NAMESPACE = os.getenv("SB_NAMESPACE")
SB_QUEUE     = os.getenv("SB_QUEUE", "pdf-jobs")
STORAGE_URL  = os.getenv("STORAGE_URL")
PAYLOAD_CTN  = os.getenv("PAYLOAD_CONTAINER", "pdfpayloads")
OUTPUT_CTN   = os.getenv("OUTPUT_CONTAINER", "pdfs")
CACHE_TTL    = int(os.getenv("PDF_CACHE_TTL", "30"))                # seconds

# one-time link signing key (URL-safe base64, 32 bytes recommended)
HMAC_SECRET = base64.urlsafe_b64decode(
    os.getenv("HMAC_SECRET_B64", base64.urlsafe_b64encode(os.urandom(32)))
)

CRED      = DefaultAzureCredential()
SB_FQDN   = f"{SB_NAMESPACE}.servicebus.windows.net"
SB_CLIENT = ServiceBusClient(SB_FQDN, credential=CRED)

# ─── Template handling ───────────────────────────────────────────────────
TPL_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}$")
TEMPLATE_DIR = Path(os.getenv("SCRIPTS_DIR", "/opt/app/scripts")).resolve()
if str(TEMPLATE_DIR) not in sys.path:
    sys.path.insert(0, str(TEMPLATE_DIR))

class ParamDict(BaseModel, extra="allow"):
    """Arbitrary JSON body forwarded to Report"""
    pass

def _import_report(template: str):
    """Import and sanity-check a Report class for *template*."""
    if not TPL_RE.fullmatch(template):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="invalid template name")
    mod_name = template.replace("-", "_")
    try:
        mod = __import__(mod_name)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"template '{template}' not found") from exc
    report_cls = getattr(mod, "Report", None)
    if not report_cls:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"{template}.Report missing")
    return report_cls

async def run_report(template: str, params: dict[str, Any]) -> dict[str, Any]:
    """Instantiate Report, await its result if needed, return placeholders."""
    report_cls = _import_report(template)
    obj = report_cls(params)
    result = obj.fetch()
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Report.fetch() did not return dict")
    return result

# ─── Helper: deterministic cache key ─────────────────────────────────────
def _make_cache_key(template: str, body_dict: dict[str, Any]) -> str:
    body_json = json.dumps(body_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{template}|{body_json}".encode()).hexdigest()

# ─── HMAC helpers for one-time links ─────────────────────────────────────
def _sign(tpl: str, sub: str, exp: int) -> str:
    msg = f"{tpl}|{sub}|{exp}".encode()
    # 128-bit hex digest is short yet secure enough
    return hmac.new(HMAC_SECRET, msg, hashlib.sha256).hexdigest()[:32]

def _verify_sig(tpl: str, sub: str, exp: int, sig: str) -> None:
    if time.time() > exp:
        raise HTTPException(410, "link expired")
    if not hmac.compare_digest(sig, _sign(tpl, sub, exp)):
        raise HTTPException(403, "invalid or reused link")

# ─── Core enqueue logic (factored out so both routes can reuse it) ───────
async def _enqueue_core(template: str,
                        body_dict: dict[str, Any],
                        claims: dict) -> dict[str, Any]:
    file_id = _make_cache_key(template, body_dict)
    pdf_blob_name = f"{file_id}.pdf"
    pdf_blob = BlobClient(account_url=STORAGE_URL,
                          container_name=OUTPUT_CTN,
                          blob_name=pdf_blob_name,
                          credential=CRED)

    # ── Cache check ──────────────────────────────────────────────────
    try:
        props = await pdf_blob.get_blob_properties()
        age   = time.time() - props.last_modified.timestamp()
        if age < CACHE_TTL:
            return {"status": "cached", "id": file_id, "age_seconds": int(age)}
    except ResourceNotFoundError:
        pass  # not cached

    # ── Render job payload ───────────────────────────────────────────
    placeholders = await run_report(template, body_dict)
    payload = {"template": template, "params": placeholders}

    payload_blob = BlobClient(account_url=STORAGE_URL,
                              container_name=PAYLOAD_CTN,
                              blob_name=file_id,      # same key, no .pdf
                              credential=CRED)
    await payload_blob.upload_blob(json.dumps(payload),
                                   overwrite=True,
                                   content_type="application/json")

    async with SB_CLIENT.get_queue_sender(SB_QUEUE) as sender:
        await sender.send_messages(ServiceBusMessage(file_id))

    return {"status": "queued", "id": file_id}

# ─── 1. Public “issue link” endpoint ─────────────────────────────────────
@app.get("/link/{template}")
async def generate_link(template: str,
                        ttl: int = Query(30, ge=5, le=300),
                        claims: dict = Depends(verify_jwt)):
    """
    Returns a one-time POST URL valid for *ttl* seconds, bound to the caller’s sub.
    """
    exp   = int(time.time()) + ttl
    token = _sign(template, claims["sub"], exp)
    return {
        "url": f"/generate-secure/{template}?t={token}&exp={exp}",
        "expires": exp
    }

# ─── 2. Secure POST: only accepts fresh, caller-bound token ──────────────
@app.post("/generate-secure/{template}")
async def enqueue_secure_pdf(template: str,
                             body: ParamDict = Depends(),
                             t: str = Query(...),
                             exp: int = Query(...),
                             claims: dict = Depends(verify_jwt)):
    _verify_sig(template, claims["sub"], exp, t)
    return await _enqueue_core(template, body.dict(), claims)

# ─── (Optional) legacy route – keep for internal clients if you wish ────
@app.post("/generate-pdf/{template}")
async def enqueue_pdf(template: str,
                      body: ParamDict = Depends(),
                      claims: dict = Depends(verify_jwt)):
    """
    Legacy entry point – behaves exactly as before but remains shareable.
    """
    return await _enqueue_core(template, body.dict(), claims)

# ─── Stream PDF (unchanged) ──────────────────────────────────────────────
@app.get("/pdf/{payload_id}")
async def get_pdf(payload_id: str, _: dict = Depends(verify_jwt)):
    blob_name = f"{payload_id}.pdf"
    blob = BlobClient(account_url=STORAGE_URL,
                      container_name=OUTPUT_CTN,
                      blob_name=blob_name,
                      credential=CRED)
    try:
        downloader = await blob.download_blob()
    except ResourceNotFoundError:
        raise HTTPException(404, "PDF not found")
    return StreamingResponse(
        downloader.chunks(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename=\"{payload_id}.pdf\"'}
    )

# ─── Health probes ───────────────────────────────────────────────────────
@app.get("/live")
async def live():  return {"status": "ok"}

@app.get("/ready")
async def ready(): return {"status": "ok"}
