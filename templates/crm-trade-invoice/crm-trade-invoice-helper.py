"""
Helper for the crm-trade-invoice template:
- PDF_OPTIONS: A4 portrait invoice settings
- async get_header_html(params): inlines logo SVG and displays mandator + client info
- async get_footer_html(params): simple footer with page numbers and optional note
- async authenticate_blob_routes(page): inject AD Bearer token for Blob urls
"""

import os
import base64
from datetime import datetime
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobClient
from playwright.async_api import Page

# ─── Configuration ─────────────────────────────────────────────────────────
STORAGE_URL = os.getenv("STORAGE_URL")

# ─── Playwright PDF options ────────────────────────────────────────────────
PDF_OPTIONS = {
    "format": "A4",
    "landscape": False,
    "print_background": True,
    "prefer_css_page_size": True,
    "display_header_footer": True,
    "margin": {"top": "15mm", "bottom": "15mm", "left": "15mm", "right": "15mm"},
}

# ─── Header / Footer generators ─────────────────────────────────────────────
async def get_header_html(params: dict) -> str:
    """
    Inline an SVG logo from Blob and render mandator + client info.
    Expects params to include:
      - logo_url, logo_container, logo_blob
      - mandatorName, clientName, invoicedate
    """
    logo_html = ""
    logo_url = params.get("logo_url")
    if logo_url:
        cred = DefaultAzureCredential()
        parsed = logo_url.replace("https://", "").split("/")
        container, blob_name = parsed[1], "/".join(parsed[2:])
        client = BlobClient(account_url=f"https://{parsed[0]}",
                            container_name=container,
                            blob_name=blob_name,
                            credential=cred)
        download = await client.download_blob()
        data = await download.readall()
        b64 = base64.b64encode(data).decode("utf-8")
        logo_html = f'<img src="data:image/svg+xml;base64,{b64}" style="height:24px;"/>'

    mandator = params.get("mandatorName", "")
    client = params.get("clientName", "")
    date_str = params.get("invoicedate", datetime.utcnow().strftime("%d.%m.%Y"))
    return f"""
<div style="display:flex;justify-content:space-between;font-size:9px;width:95%;margin:0 auto;">
  <div>
    {logo_html}
    <div><strong>Mandator:</strong> {mandator}</div>
  </div>
  <div>
    <div><strong>Client:</strong> {client}</div>
    <div><strong>Date:</strong> {date_str}</div>
  </div>
</div>
"""

async def get_footer_html(params: dict) -> str:
    """
    Builds footer with optional VAT note and page numbers.
    """
    note = params.get("footer_note", "")
    note_html = f'<div style="font-size:7px;text-align:center;margin-bottom:4px;">{note}</div>' if note else ""
    return f"""
<div style="width:95%;margin:0 auto;font-size:8px;text-align:center;">
  {note_html}
  <span class="pageNumber"></span>/<span class="totalPages"></span>
</div>
"""

# ─── Blob auth injection ────────────────────────────────────────────────────
async def authenticate_blob_routes(page: Page) -> None:
    """
    Intercept all requests to Azure Blob Storage and attach an Azure AD Bearer token.
    """
    cred = DefaultAzureCredential()
    token = (await cred.get_token("https://storage.azure.com/.default")).token
    bearer = f"Bearer {token}"
    await page.route(
        "https://*.blob.core.windows.net/*",
        lambda route, request: route.continue_(
            headers={**request.headers, "Authorization": bearer}
        )
    )

