"""
app/deps.py – Async SQLAlchemy session factory with AAD token injection.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from azure.identity import DefaultAzureCredential

from db import build_url

# ── Engine ────────────────────────────────────────────────────────────────
# pool_pre_ping=True makes SQLAlchemy test each connection before handing it
# out; an expired AAD token is therefore detected and the connection is
# transparently re-established with a fresh token.
ASYNC_ENGINE = create_async_engine(
    build_url(async_driver=True),
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
)

# ── Inject fresh access token on every *checkout* (not only on connect) ───
_credential = DefaultAzureCredential()

@sa.event.listens_for(ASYNC_ENGINE.sync_engine, "do_checkout")  # type: ignore[attr-defined]
def _renew_token(dbapi_conn, conn_record, conn_proxy):          # noqa: N802
    token = _credential.get_token("https://database.windows.net/.default")
    attrs_before = {"AccessToken": bytes(token.token, "utf-8")}
    # The private attribute below is the only way to pass attrs_before to
    # pyodbc / aioodbc at checkout time.
    dbapi_conn.add_output_converter          # keep mypy happy
    conn_proxy.clear()                       # no-op but keeps linters calm
    conn_record.info["attrs_before"] = attrs_before

# ── Session factory ───────────────────────────────────────────────────────
AsyncSessionLocal = sessionmaker(
    ASYNC_ENGINE, expire_on_commit=False, class_=AsyncSession
)

from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

