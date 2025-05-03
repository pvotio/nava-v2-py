"""
app/db.py – connection-string helper only.

The actual database work happens through the async engine that is
constructed in deps.py, so we no longer create a synchronous
SQLAlchemy engine here (which would have required pyodbc).
"""
from __future__ import annotations

import os
import urllib.parse

SQL_SERVER = os.environ["SQL_SERVER"]     # e.g. navadb.database.windows.net
SQL_DB     = os.environ["SQL_DB"]         # e.g. PdfCore
DRIVER     = "ODBC Driver 18 for SQL Server"


def build_url(*, async_driver: bool = False) -> str:
    """
    Return a SQLAlchemy URL for Azure SQL using AAD MSI auth.

    Set  async_driver=True  for  mssql+aioodbc://…
    """
    params = urllib.parse.quote_plus(
        f"Driver={DRIVER};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Authentication=ActiveDirectoryMsi"
    )
    dialect = "aioodbc" if async_driver else "pyodbc"
    return f"mssql+{dialect}://@{SQL_SERVER}:1433/{SQL_DB}?odbc_connect={params}"

