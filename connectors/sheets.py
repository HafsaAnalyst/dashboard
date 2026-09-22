"""
Google Sheets connector — READ ONLY, by construction.

Three independent guards stop this module ever writing to the sheet:

  1. SCOPE: spreadsheets.readonly. The token Google issues cannot write.
     Any update/append/clear call fails with 403 insufficient scopes
     before it reaches the spreadsheet.
  2. PERMISSION: share the sheet with the service-account email as
     *Viewer*, not Editor. No write grant exists on the file itself.
  3. SURFACE: only values().get() is called below. Never add
     values().update / append / clear / batchUpdate here -- if a write
     is ever genuinely needed, it belongs in a separate module with its
     own scope, not in this one.

.env:
    SHEETS_SERVICE_ACCOUNT_FILE=path/to/service-account.json
    SHEETS_SPREADSHEET_ID=1AbC...          # the long id in the sheet URL
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import find_dotenv, load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Read-only. Do not widen this.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _resolve_sa_path(p: str) -> str:
    """Resolve a service-account JSON path. Handles both absolute paths and
    relative paths (resolved against the .env file's directory, not CWD)."""
    if not p:
        return p
    if os.path.isabs(p) and os.path.exists(p):
        return p
    if os.path.exists(p):
        return p
    env = find_dotenv()
    if env:
        candidate = os.path.normpath(os.path.join(os.path.dirname(env), p))
        if os.path.exists(candidate):
            return candidate
    return p


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_file = os.getenv("SHEETS_SERVICE_ACCOUNT_FILE") or os.getenv(
        "GSC_SERVICE_ACCOUNT_FILE"
    )
    if not sa_file:
        raise RuntimeError("SHEETS_SERVICE_ACCOUNT_FILE not set in .env")
    sa_file = _resolve_sa_path(sa_file)
    creds = service_account.Credentials.from_service_account_file(
        sa_file, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _spreadsheet_id(explicit: Optional[str] = None) -> str:
    sid = explicit or os.getenv("SHEETS_SPREADSHEET_ID")
    if not sid:
        raise RuntimeError("SHEETS_SPREADSHEET_ID not set in .env")
    return sid


def fetch_range(
    a1_range: str,
    spreadsheet_id: Optional[str] = None,
) -> list[list[str]]:
    """Raw cell values for an A1 range, e.g. "Sheet1!A1:H".

    Returns the rows as Google sends them: ragged (trailing empty cells are
    omitted) and all strings, because UNFORMATTED_VALUE is not requested.
    """
    resp = (
        _service()
        .spreadsheets()
        .values()
        .get(
            spreadsheetId=_spreadsheet_id(spreadsheet_id),
            range=a1_range,
        )
        .execute()
    )
    rows = resp.get("values", [])
    logger.info("sheets: %s -> %d rows", a1_range, len(rows))
    return rows


def fetch_table(
    a1_range: str,
    spreadsheet_id: Optional[str] = None,
    header_row: int = 0,
):
    """fetch_range() as a DataFrame, first row treated as the header.

    Ragged rows are padded to the header width so pandas doesn't drop or
    misalign columns when trailing cells are blank.
    """
    import pandas as pd

    rows = fetch_range(a1_range, spreadsheet_id)
    if len(rows) <= header_row:
        return pd.DataFrame()

    header = [str(c).strip() for c in rows[header_row]]
    width = len(header)
    body = [(r + [""] * width)[:width] for r in rows[header_row + 1:]]
    return pd.DataFrame(body, columns=header)
