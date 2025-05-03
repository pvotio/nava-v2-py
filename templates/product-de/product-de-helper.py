"""
Helper for the product-de template:
- PDF_OPTIONS: A4 portrait with margins
- async get_header_html(params): inlines logo SVG and shows date
- async get_footer_html(params): simple page number footer
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
    "margin": {"top": "20mm", "bottom": "20mm", "left": "10mm", "right": "10mm"},
}

# ─── Header / Footer generators ─────────────────────────────────────────────
async def get_header_html(params: dict) -> str:
    """
    Inline an SVG logo from Blob (if provided) and show the product date.
    Expects params to include:
      - logo_url: full blob URL to the SVG
      - logo_container, logo_blob (optional overrides)
      - product_date
    """
    logo_html = ""
    logo_url = params.get("logo_url")
    if logo_url:
        cred = DefaultAzureCredential()
        # parse container & blob from URL path
        parsed = logo_url.replace("https://", "").split("/")
        container, blob_name = parsed[1], "/".join(parsed[2:])
        client = BlobClient(account_url=f"https://{parsed[0]}",
                            container_name=container,
                            blob_name=blob_name,
                            credential=cred)
        blob_data = await client.download_blob()
        data = await blob_data.readall()
        b64 = base64.b64encode(data).decode("utf-8")
        logo_html = f'<img src="data:image/svg+xml;base64,{b64}" style="height:20px;"/>'

    date_str = params.get("product_date", datetime.utcnow().strftime("%d.%m.%Y"))
    return f"""
<div style="width:90%;margin:0 auto;font-size:8px;display:flex;justify-content:space-between;">
  <div>{logo_html}</div>
  <div>{date_str}</div>
</div>
"""

async def get_footer_html(params: dict) -> str:
    """
    Simple centered page numbers footer.
    """
    return """
<div style="width:90%;margin:0 auto;font-size:8px;text-align:center;">
  <span class="pageNumber"></span> / <span class="totalPages"></span>
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

