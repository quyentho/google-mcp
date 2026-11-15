#!/usr/bin/env python3
"""
Self-hosted Google Drive MCP Server — read and write, no third-party service.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Drive API — with OAuth 2.0 or a Service
Account. Nothing else ever sees your credentials or your files.

Like Sheets and Calendar, there is NO App Password mode for Drive:
Google offers no password protocol for it. Drive requires an OAuth
token or a Service Account. Both are direct-to-Google, self-hosted.

  1. OAuth 2.0 — DRIVE_AUTH_METHOD=oauth
     - Your own Google account, Desktop OAuth client in Google Cloud.
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scope: drive (read/write all Drive files)
     - Best for: your personal Drive.

  2. Service Account — DRIVE_AUTH_METHOD=service_account
     - A Google Cloud service account, download its JSON key as
       service-account.json in this folder.
     - Share the folder/file you want it to touch with the service
       account's email, Viewer or Editor. Files it creates live in
       its own Drive unless you move/share them.
     - No browser login, no token refresh, good for automation.

  AUTO (default): Service Account if service-account.json exists,
  otherwise OAuth if token.json exists.

Tools:
  list_files(query, folder_id, mime_type, max_results) — find files, newest first
  get_file(file_id) — metadata for one file/folder
  read_file(file_id) — text content of a file (exported as text for Google Docs/Sheets/Slides)
  upload_file(local_path, name, parent_folder_id, mime_type) — upload a local file
  create_folder(name, parent_folder_id) — create a folder
  copy_file(file_id, name, parent_folder_id) — copy a file
  rename_file(file_id, new_name) — rename a file/folder
  trash_file(file_id) — move to trash (recoverable, not permanent delete)
  share_file(file_id, email, role, notify) — share with a person
  drive_status() — check config without touching any file or revealing secrets

A file_id is the id from list_files, or a full Drive URL — the server
extracts the id from https://drive.google.com/file/d/<id>/... and
https://drive.google.com/drive/folders/<id>.

Env vars (see env.example):
  DRIVE_AUTH_METHOD=auto|oauth|service_account
  DRIVE_CREDENTIALS_FILE, DRIVE_TOKEN_FILE, DRIVE_SERVICE_ACCOUNT_FILE
"""

import io
import mimetypes
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("google-drive")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("google-drive")

BASE_DIR = Path(__file__).parent
AUTH_METHOD = os.environ.get("DRIVE_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("DRIVE_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("DRIVE_TOKEN_FILE", BASE_DIR / "token.json"))
SERVICE_ACCOUNT_FILE = Path(
    os.environ.get("DRIVE_SERVICE_ACCOUNT_FILE", BASE_DIR / "service-account.json")
)

SCOPES = ["https://www.googleapis.com/auth/drive"]

FILE_URL_RE = re.compile(r"/file/d/([a-zA-Z0-9-_]+)")
FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9-_]+)")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_RESULTS = 50
MAX_TEXT_CHARS = 20000
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,createdTime,webViewLink,"
    "parents,owners(displayName,emailAddress),description,shared,starred,trashed"
)

# Native Google files -> export format for read_file()
EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")


# ---------------------------------------------------------------- helpers
def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("oauth", "service_account"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(
            f"DRIVE_AUTH_METHOD must be auto, oauth or service_account, got '{AUTH_METHOD}'"
        )
    if SERVICE_ACCOUNT_FILE.exists():
        return "service_account"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Google Drive auth configured. For OAuth run `python auth_oauth.py` once, "
        "or place a service-account JSON at service-account.json "
        "and set DRIVE_AUTH_METHOD=service_account."
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


def _method() -> str:
    return _resolve_auth_method()


def _extract_file_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("`file_id` is required — use an `id` from list_files, or a full Google Drive URL")
    for pattern in (FILE_URL_RE, FOLDER_URL_RE):
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value


def _format_file(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "size": item.get("size"),
        "modified_time": item.get("modifiedTime"),
        "created_time": item.get("createdTime"),
        "web_view_link": item.get("webViewLink"),
        "parents": item.get("parents", []),
        "owners": [o.get("emailAddress") for o in item.get("owners", []) if o.get("emailAddress")],
        "description": item.get("description", ""),
        "shared": bool(item.get("shared")),
        "starred": bool(item.get("starred")),
        "trashed": bool(item.get("trashed")),
    }


def _download_bytes(service, file_id: str, mime_type: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buffer = io.BytesIO()
    if mime_type in EXPORT_MIME:
        request = service.files().export_media(fileId=file_id, mimeType=EXPORT_MIME[mime_type])
    else:
        request = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buffer.getvalue()


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def drive_status() -> dict:
    """Check which Google Drive auth is configured, without touching any file or revealing secrets."""
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

            service_account_email = json.loads(SERVICE_ACCOUNT_FILE.read_text()).get("client_email")
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
        "note": "Drive has no App Password mode — Google requires OAuth or a Service Account. Scope is 'drive' (read/write).",
        "capabilities": [
            "list_files",
            "get_file",
            "read_file",
            "upload_file",
            "create_folder",
            "copy_file",
            "rename_file",
            "trash_file",
            "share_file",
        ],
        "error": error,
    }


@mcp.tool()
def list_files(
    query: str = "",
    folder_id: str = "",
    mime_type: str = "",
    max_results: int = 10,
) -> dict:
    """
    Find files and folders in Drive. Read-only.

    Args:
        query: Substring of the file name, e.g. "Invoice". Empty = most recently modified.
        folder_id: Only files inside this folder (a folder id from list_files, or a Drive folders URL).
        mime_type: Only files of this type, e.g. "application/pdf" or
            "application/vnd.google-apps.document". Empty = any type.
        max_results: How many to return (1-50, default 10). Most recently modified first.
    Returns id, name, mime type, size, modified time and link for each file.
    """
    try:
        service = _drive_service()
        max_results = max(1, min(int(max_results or 10), MAX_RESULTS))
        q_parts = ["trashed = false"]
        if query and query.strip():
            safe = query.strip().replace("'", "\\'")
            q_parts.append(f"name contains '{safe}'")
        if folder_id and folder_id.strip():
            fid = _extract_file_id(folder_id)
            q_parts.append(f"'{fid}' in parents")
        if mime_type and mime_type.strip():
            safe_mime = mime_type.strip().replace("'", "\\'")
            q_parts.append(f"mimeType = '{safe_mime}'")
        result = (
            service.files()
            .list(
                q=" and ".join(q_parts),
                orderBy="modifiedTime desc",
                pageSize=max_results,
                fields=f"files({FILE_FIELDS})",
            )
            .execute()
        )
        files = [_format_file(f) for f in result.get("files", [])]
        return {
            "ok": True,
            "auth_method": _method(),
            "query": query or "",
            "folder_id": folder_id or "",
            "count": len(files),
            "files": files,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_file(file_id: str) -> dict:
    """
    Get metadata for one file or folder: name, type, size, owners,
    modified/created time, parents, link and whether it is shared/starred/trashed.

    Args:
        file_id: The id from list_files, or a full Google Drive URL.
    """
    try:
        service = _drive_service()
        fid = _extract_file_id(file_id)
        result = service.files().get(fileId=fid, fields=FILE_FIELDS).execute()
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def read_file(file_id: str) -> dict:
    """
    Read the text content of a file. Read-only.

    Google Docs are exported as plain text, Sheets as CSV, Slides as plain
    text. Plain-text, JSON, CSV, Markdown and similar files are downloaded
    as text (truncated at 20,000 characters). Binary files (images, PDFs
    with no text export, videos, zips, ...) are NOT returned as text —
    you get their metadata and link instead, so the agent knows what they are.

    Args:
        file_id: The id from list_files, or a full Google Drive URL.
    """
    try:
        service = _drive_service()
        fid = _extract_file_id(file_id)
        meta = service.files().get(fileId=fid, fields=FILE_FIELDS).execute()
        mime_type = meta.get("mimeType", "")
        if mime_type == "application/vnd.google-apps.folder":
            raise ValueError("That id is a folder, not a file. Use list_files(folder_id=...) to see what is inside it.")
        is_text = mime_type in EXPORT_MIME or mime_type.startswith(TEXT_MIME_PREFIXES)
        if not is_text:
            return {
                "ok": True,
                "auth_method": _method(),
                **_format_file(meta),
                "content": None,
                "content_note": f"Binary file ({mime_type}) — text content is not returned. Open it via web_view_link.",
            }
        raw = _download_bytes(service, fid, mime_type)
        text = raw.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "auth_method": _method(),
            **_format_file(meta),
            "exported_as": EXPORT_MIME.get(mime_type, mime_type),
            "content": text[:MAX_TEXT_CHARS],
            "content_truncated": len(text) > MAX_TEXT_CHARS,
            "content_length": len(text),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def upload_file(
    local_path: str,
    name: str = "",
    parent_folder_id: str = "",
    mime_type: str = "",
) -> dict:
    """
    Upload a file from the machine this server runs on into Drive.

    Args:
        local_path: Path to the local file, e.g. "/home/matthew/report.pdf".
        name: Name in Drive. Defaults to the local file name.
        parent_folder_id: Folder to upload into (id or Drive folders URL).
            Empty = Drive root (My Drive).
        mime_type: MIME type, e.g. "application/pdf". Guessed from the file name if empty.
    Returns the new file id, name and link. Max upload size 100 MB.
    """
    try:
        path = Path((local_path or "").strip())
        if not str(local_path).strip():
            raise ValueError("`local_path` is required")
        if not path.exists() or not path.is_file():
            raise ValueError(f"Local file not found: {local_path}")
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"File is larger than the 100 MB upload limit: {local_path}")
        from googleapiclient.http import MediaFileUpload

        file_name = (name or "").strip() or path.name
        guessed = mime_type.strip() if mime_type and mime_type.strip() else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body: dict = {"name": file_name}
        if parent_folder_id and parent_folder_id.strip():
            body["parents"] = [_extract_file_id(parent_folder_id)]
        media = MediaFileUpload(str(path), mimetype=guessed, resumable=True)
        service = _drive_service()
        result = service.files().create(body=body, media_body=media, fields=FILE_FIELDS).execute()
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_folder(name: str, parent_folder_id: str = "") -> dict:
    """
    Create a folder in Drive.

    Args:
        name: Folder name, e.g. "Invoices 2026".
        parent_folder_id: Parent folder (id or Drive folders URL). Empty = Drive root.
    Returns the new folder id, name and link.
    """
    try:
        if not name or not str(name).strip():
            raise ValueError("`name` is required")
        body: dict = {
            "name": str(name).strip(),
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_folder_id and parent_folder_id.strip():
            body["parents"] = [_extract_file_id(parent_folder_id)]
        service = _drive_service()
        result = service.files().create(body=body, fields=FILE_FIELDS).execute()
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def copy_file(file_id: str, name: str = "", parent_folder_id: str = "") -> dict:
    """
    Copy a file in Drive.

    Args:
        file_id: The id of the file to copy, or a full Google Drive URL.
        name: Name for the copy. Defaults to "Copy of <original name>".
        parent_folder_id: Folder to put the copy in. Empty = same folder as the original.
    Returns the copy's id, name and link.
    """
    try:
        fid = _extract_file_id(file_id)
        body: dict = {}
        if name and str(name).strip():
            body["name"] = str(name).strip()
        if parent_folder_id and parent_folder_id.strip():
            body["parents"] = [_extract_file_id(parent_folder_id)]
        service = _drive_service()
        result = service.files().copy(fileId=fid, body=body, fields=FILE_FIELDS).execute()
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def rename_file(file_id: str, new_name: str) -> dict:
    """
    Rename a file or folder in Drive.

    Args:
        file_id: The id from list_files, or a full Google Drive URL.
        new_name: The new name.
    """
    try:
        fid = _extract_file_id(file_id)
        if not new_name or not str(new_name).strip():
            raise ValueError("`new_name` is required")
        service = _drive_service()
        result = (
            service.files()
            .update(fileId=fid, body={"name": str(new_name).strip()}, fields=FILE_FIELDS)
            .execute()
        )
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def trash_file(file_id: str) -> dict:
    """
    Move a file or folder to trash. Recoverable from Drive trash — this is
    NOT a permanent delete.

    Args:
        file_id: The id from list_files, or a full Google Drive URL.
    """
    try:
        fid = _extract_file_id(file_id)
        service = _drive_service()
        result = (
            service.files()
            .update(fileId=fid, body={"trashed": True}, fields=FILE_FIELDS)
            .execute()
        )
        return {"ok": True, "auth_method": _method(), **_format_file(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def share_file(file_id: str, email: str, role: str = "reader", notify: bool = True) -> dict:
    """
    Share a file or folder with a person by email.

    Args:
        file_id: The id from list_files, or a full Google Drive URL.
        email: The person's email address.
        role: "reader" (default), "commenter", or "writer".
        notify: Email them about the share (default True).
    Returns the permission id and role granted.
    """
    try:
        fid = _extract_file_id(file_id)
        email = (email or "").strip()
        if not email or not EMAIL_RE.match(email):
            raise ValueError("A valid `email` is required")
        if role not in ("reader", "commenter", "writer"):
            raise ValueError("`role` must be reader, commenter or writer")
        service = _drive_service()
        result = (
            service.permissions()
            .create(
                fileId=fid,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=bool(notify),
                fields="id,type,role,emailAddress",
            )
            .execute()
        )
        return {
            "ok": True,
            "auth_method": _method(),
            "file_id": fid,
            "permission_id": result.get("id"),
            "email": result.get("emailAddress"),
            "role": result.get("role"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
