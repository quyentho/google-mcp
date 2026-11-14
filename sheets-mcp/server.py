#!/usr/bin/env python3
"""
Self-hosted Google Sheets MCP Server — read and write, no third-party service.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Sheets API (+ Drive API for listing) —
with OAuth 2.0 or a Service Account. Nothing else ever sees your
credentials or your sheets.

Unlike Gmail, there is NO App Password mode for Sheets: Google offers
no SMTP/IMAP-style password protocol for Sheets. Sheets requires
an OAuth token or a Service Account. Both are direct-to-Google,
self-hosted, and covered here.

  1. OAuth 2.0 — SHEETS_AUTH_METHOD=oauth
     - Your own Google account, Desktop OAuth client in Google Cloud.
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scopes: spreadsheets (read/write) + drive.readonly (list/search)
     - Best for: your personal sheets.

  2. Service Account — SHEETS_AUTH_METHOD=service_account
     - A Google Cloud service account, download its JSON key as
       service-account.json in this folder.
     - Share each sheet you want it to touch with the service
       account's email (like sharing with a person), Viewer or Editor.
     - No browser login, no token refresh, good for automation.
     - Best for: shared / automated sheets, no interactive login.

  AUTO (default): Service Account if SHEETS_SERVICE_ACCOUNT_FILE
  exists, otherwise OAuth if token.json exists.

Tools:
  list_spreadsheets(query, max_results) — find sheets by name (Drive search)
  get_spreadsheet(spreadsheet_id)       — metadata + tabs/sheets
  read_range(spreadsheet_id, range, as_objects) — read values, optionally as dicts
  update_range(spreadsheet_id, range, values, value_input_option) — overwrite a range
  append_rows(spreadsheet_id, range, values, value_input_option)  — append rows
  clear_range(spreadsheet_id, range)    — clear values in a range
  create_spreadsheet(title, sheet_titles) — create a new spreadsheet
  add_sheet(spreadsheet_id, title, rows, columns) — add a tab
  sheets_status() — check config without touching any sheet or revealing secrets

Env vars (see env.example):
  SHEETS_AUTH_METHOD=auto|oauth|service_account
  SHEETS_CREDENTIALS_FILE, SHEETS_TOKEN_FILE, SHEETS_SERVICE_ACCOUNT_FILE
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("googlesheet")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("googlesheet")

BASE_DIR = Path(__file__).parent
AUTH_METHOD = os.environ.get("SHEETS_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("SHEETS_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("SHEETS_TOKEN_FILE", BASE_DIR / "token.json"))
SERVICE_ACCOUNT_FILE = Path(
    os.environ.get("SHEETS_SERVICE_ACCOUNT_FILE", BASE_DIR / "service-account.json")
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# https://docs.google.com/spreadsheets/d/<id>/edit...
SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
MAX_ROWS = 10000
MAX_LIST_RESULTS = 50


# ---------------------------------------------------------------- helpers
def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("oauth", "service_account"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(
            f"SHEETS_AUTH_METHOD must be auto, oauth or service_account, got '{AUTH_METHOD}'"
        )
    if SERVICE_ACCOUNT_FILE.exists():
        return "service_account"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Google Sheets auth configured. For OAuth run `python auth_oauth.py` once, "
        "or place a service-account JSON at service-account.json "
        "and set SHEETS_AUTH_METHOD=service_account."
    )


def _get_credentials():
    method = _resolve_auth_method()
    if method == "service_account":
        try:
            from google.oauth2 import service_account
        except ImportError as e:
            raise ValueError(
                "Google auth libraries not installed. Run: pip install -r requirements.txt. "
                f"Original error: {e}"
            )
        if not SERVICE_ACCOUNT_FILE.exists():
            raise ValueError(
                f"Service account file not found at {SERVICE_ACCOUNT_FILE}. "
                "Download the JSON key from Google Cloud and save it there."
            )
        return service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
        ), method

    # oauth
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise ValueError(
            "Google auth libraries not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    if not TOKEN_FILE.exists():
        raise ValueError(f"OAuth token not found at {TOKEN_FILE}. Run once: python auth_oauth.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise ValueError(
                "OAuth token expired and no refresh token, or scopes changed. "
                "Delete token.json and re-run: python auth_oauth.py"
            )
    return creds, method


def _sheets_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "google-api-python-client not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    creds, _method = _get_credentials()
    return build("sheets", "v4", credentials=creds)


def _drive_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "google-api-python-client not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    creds, _method = _get_credentials()
    return build("drive", "v3", credentials=creds)


def _extract_spreadsheet_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("`spreadsheet_id` is required — use an `id` from list_spreadsheets, or a full Google Sheets URL")
    match = SPREADSHEET_URL_RE.search(value)
    if match:
        return match.group(1)
    # A bare id is a long base64-ish token; accept anything non-empty here
    # and let the API return a clear 404 if it is wrong.
    return value


def _require_range(a1_range: str) -> str:
    a1_range = (a1_range or "").strip()
    if not a1_range:
        raise ValueError("`range` is required — e.g. 'Sheet1!A1:D10' or 'A1:D10'")
    return a1_range


def _validate_values(values) -> list:
    if not values or not isinstance(values, list):
        raise ValueError("`values` is required — a list of rows, e.g. [[\"a\", \"b\"], [1, 2]]")
    rows = []
    for row in values:
        if isinstance(row, (list, tuple)):
            rows.append([None if v is None else v for v in row])
        else:
            rows.append([row])
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Too many rows: {len(rows)} (max {MAX_ROWS})")
    return rows


def _rows_to_objects(values: list) -> list:
    if not values or len(values) < 2:
        return []
    headers = [str(h).strip() if h is not None else "" for h in values[0]]
    objects = []
    for row in values[1:]:
        obj = {}
        for idx, header in enumerate(headers):
            key = header or f"col_{idx + 1}"
            obj[key] = row[idx] if idx < len(row) else ""
        objects.append(obj)
    return objects


def _method() -> str:
    return _resolve_auth_method()


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def sheets_status() -> dict:
    """Check which Google Sheets auth is configured, without touching any sheet or revealing secrets."""
    try:
        method = _resolve_auth_method()
    except Exception as e:
        method, error = None, str(e)
    else:
        error = None
    service_account_email = None
    if SERVICE_ACCOUNT_FILE.exists():
        try:
            import json

            service_account_email = json.loads(SERVICE_ACCOUNT_FILE.read_text()).get(
                "client_email"
            )
        except Exception:
            service_account_email = None
    return {
        "configured": method is not None,
        "auth_method_requested": AUTH_METHOD,
        "auth_method_resolved": method,
        "oauth_token_exists": TOKEN_FILE.exists(),
        "oauth_credentials_exists": CREDENTIALS_FILE.exists(),
        "service_account_file_exists": SERVICE_ACCOUNT_FILE.exists(),
        "service_account_email": service_account_email,
        "oauth_scopes": SCOPES,
        "note": "Sheets has no App Password mode — Google requires OAuth or a Service Account.",
        "capabilities": [
            "list_spreadsheets",
            "get_spreadsheet",
            "read_range",
            "update_range",
            "append_rows",
            "clear_range",
            "create_spreadsheet",
            "add_sheet",
        ],
        "error": error,
    }


@mcp.tool()
def list_spreadsheets(query: str = "", max_results: int = 10) -> dict:
    """
    Find Google Sheets by name. Read-only.

    Args:
        query: Substring of the spreadsheet name, e.g. "Budget". Empty = most recently modified.
        max_results: How many to return (1-50, default 10). Most recently modified first.
    Returns id, name, modifiedTime and webViewLink for each sheet — use the id in the other tools.
    """
    try:
        service = _drive_service()
        max_results = max(1, min(int(max_results or 10), MAX_LIST_RESULTS))
        q_parts = ["mimeType = 'application/vnd.google-apps.spreadsheet'", "trashed = false"]
        if query and query.strip():
            safe = query.strip().replace("'", "\\'")
            q_parts.append(f"name contains '{safe}'")
        result = (
            service.files()
            .list(
                q=" and ".join(q_parts),
                orderBy="modifiedTime desc",
                pageSize=max_results,
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
            )
            .execute()
        )
        files = result.get("files", [])
        return {
            "ok": True,
            "auth_method": _method(),
            "query": query or "",
            "count": len(files),
            "spreadsheets": files,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_spreadsheet(spreadsheet_id: str) -> dict:
    """
    Get a spreadsheet's metadata: title, locale, timezone, and its tabs (sheets)
    with their titles, ids, row/column counts and frozen rows/columns.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
    """
    try:
        service = _sheets_service()
        sid = _extract_spreadsheet_id(spreadsheet_id)
        result = service.spreadsheets().get(spreadsheetId=sid).execute()
        sheets = []
        for sheet in result.get("sheets", []):
            props = sheet.get("properties", {})
            grid = props.get("gridProperties", {})
            sheets.append(
                {
                    "sheet_id": props.get("sheetId"),
                    "title": props.get("title"),
                    "index": props.get("index"),
                    "sheet_type": props.get("sheetType"),
                    "row_count": grid.get("rowCount"),
                    "column_count": grid.get("columnCount"),
                    "frozen_row_count": grid.get("frozenRowCount", 0),
                    "frozen_column_count": grid.get("frozenColumnCount", 0),
                }
            )
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": result.get("spreadsheetId"),
            "title": result.get("properties", {}).get("title"),
            "locale": result.get("properties", {}).get("locale"),
            "time_zone": result.get("properties", {}).get("timeZone"),
            "url": result.get("spreadsheetUrl"),
            "sheets": sheets,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def read_range(spreadsheet_id: str, range: str, as_objects: bool = False) -> dict:
    """
    Read values from a range. Read-only.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
        range: A1 notation, e.g. "Sheet1!A1:D10", "A1:D10", or just "Sheet1" for the whole tab.
        as_objects: If True, treat the first row as headers and return a list of dicts
            instead of a 2D array. Default False.
    Returns the values as a 2D array (rows of cells), plus row/column counts.
    Empty cells are returned as "".
    """
    try:
        service = _sheets_service()
        sid = _extract_spreadsheet_id(spreadsheet_id)
        a1_range = _require_range(range)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=a1_range)
            .execute()
        )
        values = result.get("values", [])
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": sid,
            "range": result.get("range", a1_range),
            "major_dimension": result.get("majorDimension", "ROWS"),
            "row_count": len(values),
            "column_count": max((len(r) for r in values), default=0),
            "values": values if not as_objects else _rows_to_objects(values),
            "as_objects": as_objects,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def update_range(
    spreadsheet_id: str,
    range: str,
    values: list,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """
    Overwrite values in a range. Existing cells in the range are replaced.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
        range: A1 notation, e.g. "Sheet1!A1:B2".
        values: A list of rows, e.g. [["Name", "Age"], ["Alice", 30]].
        value_input_option: "USER_ENTERED" (default — parses numbers/dates/formulas
            like typing in Sheets) or "RAW" (store exactly as given, no parsing).
    Returns the updated range and how many cells were written.
    """
    try:
        sid = _extract_spreadsheet_id(spreadsheet_id)
        a1_range = _require_range(range)
        rows = _validate_values(values)
        if value_input_option not in ("USER_ENTERED", "RAW"):
            raise ValueError("value_input_option must be USER_ENTERED or RAW")
        service = _sheets_service()
        result = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=sid,
                range=a1_range,
                valueInputOption=value_input_option,
                body={"range": a1_range, "majorDimension": "ROWS", "values": rows},
            )
            .execute()
        )
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": sid,
            "updated_range": result.get("updatedRange"),
            "updated_rows": result.get("updatedRows"),
            "updated_columns": result.get("updatedColumns"),
            "updated_cells": result.get("updatedCells"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def append_rows(
    spreadsheet_id: str,
    range: str,
    values: list,
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """
    Append rows to the end of a table in a sheet.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
        range: The table to append to, e.g. "Sheet1!A:B" or "Sheet1". New rows go after the last row.
        values: A list of rows to append, e.g. [["Alice", 30], ["Bob", 25]].
        value_input_option: "USER_ENTERED" (default) or "RAW".
    Returns the range the rows were written to and how many cells were added.
    """
    try:
        sid = _extract_spreadsheet_id(spreadsheet_id)
        a1_range = _require_range(range)
        rows = _validate_values(values)
        if value_input_option not in ("USER_ENTERED", "RAW"):
            raise ValueError("value_input_option must be USER_ENTERED or RAW")
        service = _sheets_service()
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=sid,
                range=a1_range,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body={"range": a1_range, "majorDimension": "ROWS", "values": rows},
            )
            .execute()
        )
        updates = result.get("updates", {})
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": sid,
            "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows"),
            "updated_columns": updates.get("updatedColumns"),
            "updated_cells": updates.get("updatedCells"),
            "table_range": result.get("tableRange"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def clear_range(spreadsheet_id: str, range: str) -> dict:
    """
    Clear values (not formatting) in a range.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
        range: A1 notation, e.g. "Sheet1!A1:D10".
    """
    try:
        service = _sheets_service()
        sid = _extract_spreadsheet_id(spreadsheet_id)
        a1_range = _require_range(range)
        result = (
            service.spreadsheets()
            .values()
            .clear(spreadsheetId=sid, range=a1_range, body={})
            .execute()
        )
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": sid,
            "cleared_range": result.get("clearedRange", a1_range),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_spreadsheet(title: str, sheet_titles: list[str] | None = None) -> dict:
    """
    Create a new spreadsheet in your Google Drive.

    Args:
        title: Title of the new spreadsheet, e.g. "Monthly Budget".
        sheet_titles: Optional list of tab names to create, e.g. ["2026", "Summary"].
            Defaults to one tab named "Sheet1".
    Returns the new spreadsheet id and URL — share/save it for the other tools.
    """
    try:
        service = _sheets_service()
        if not title or not str(title).strip():
            raise ValueError("`title` is required")
        titles = sheet_titles or ["Sheet1"]
        body = {
            "properties": {"title": str(title).strip()},
            "sheets": [{"properties": {"title": str(t)}} for t in titles if str(t).strip()],
        }
        if not body["sheets"]:
            body["sheets"] = [{"properties": {"title": "Sheet1"}}]
        result = service.spreadsheets().create(body=body).execute()
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": result.get("spreadsheetId"),
            "title": result.get("properties", {}).get("title"),
            "url": result.get("spreadsheetUrl"),
            "sheets": [s.get("properties", {}).get("title") for s in result.get("sheets", [])],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def add_sheet(spreadsheet_id: str, title: str, rows: int = 1000, columns: int = 26) -> dict:
    """
    Add a new tab to an existing spreadsheet.

    Args:
        spreadsheet_id: The id from list_spreadsheets, or a full Google Sheets URL.
        title: Title of the new tab, e.g. "February".
        rows: Row count for the new tab (default 1000).
        columns: Column count for the new tab (default 26).
    """
    try:
        service = _sheets_service()
        sid = _extract_spreadsheet_id(spreadsheet_id)
        if not title or not str(title).strip():
            raise ValueError("`title` is required")
        body = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": str(title).strip(),
                            "gridProperties": {
                                "rowCount": max(1, int(rows)),
                                "columnCount": max(1, int(columns)),
                            },
                        }
                    }
                }
            ]
        }
        result = service.spreadsheets().batchUpdate(spreadsheetId=sid, body=body).execute()
        replies = result.get("replies", [])
        props = replies[0].get("addSheet", {}).get("properties", {}) if replies else {}
        return {
            "ok": True,
            "auth_method": _method(),
            "spreadsheet_id": sid,
            "sheet_id": props.get("sheetId"),
            "title": props.get("title"),
            "row_count": props.get("gridProperties", {}).get("rowCount"),
            "column_count": props.get("gridProperties", {}).get("columnCount"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
