#!/usr/bin/env python3
"""
Self-hosted Google Docs MCP Server — read and write, no third-party service.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Docs API (and the Drive API only to
list/find documents by name) — with OAuth 2.0 or a Service Account.
Nothing else ever sees your credentials or your documents.

Like Sheets, Calendar and Drive, there is NO App Password mode for
Docs: Google offers no password protocol for it. Docs requires an
OAuth token or a Service Account. Both are direct-to-Google,
self-hosted.

  1. OAuth 2.0 — DOCS_AUTH_METHOD=oauth
     - Your own Google account, Desktop OAuth client in Google Cloud.
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scopes: documents (read/write) + drive.readonly (list/find docs)
     - Best for: your personal documents.

  2. Service Account — DOCS_AUTH_METHOD=service_account
     - A Google Cloud service account, download its JSON key as
       service-account.json in this folder.
     - Share each document you want it to touch with the service
       account's email (like sharing with a person), Viewer or Editor.
     - No browser login, no token refresh, good for automation.
     - Best for: shared / automated documents, no interactive login.

  AUTO (default): Service Account if service-account.json exists,
  otherwise OAuth if token.json exists.

Tools:
  list_documents(query, max_results) — find documents by name (Drive search)
  get_document(document_id) — title, revision and full plain-text content
  create_document(title, text) — create a document, optionally with initial text
  append_text(document_id, text) — add text to the end of a document
  replace_text(document_id, find, replace, match_case) — find-and-replace in a document
  docs_status() — check config without touching any document or revealing secrets

A document_id is the id from list_documents, or a full Docs URL —
the server extracts the id from
https://docs.google.com/document/d/<id>/... for you.
For file operations (move, rename, trash, share, upload) use the
Drive MCP server (drive-mcp/) — Docs here is for editing content.

Env vars (see env.example):
  DOCS_AUTH_METHOD=auto|oauth|service_account
  DOCS_CREDENTIALS_FILE, DOCS_TOKEN_FILE, DOCS_SERVICE_ACCOUNT_FILE
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("google-docs")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("google-docs")

BASE_DIR = Path(__file__).parent
AUTH_METHOD = os.environ.get("DOCS_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("DOCS_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("DOCS_TOKEN_FILE", BASE_DIR / "token.json"))
SERVICE_ACCOUNT_FILE = Path(
    os.environ.get("DOCS_SERVICE_ACCOUNT_FILE", BASE_DIR / "service-account.json")
)

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
]

DOC_URL_RE = re.compile(r"/document/d/([a-zA-Z0-9-_]+)")
MAX_RESULTS = 50
MAX_TEXT_CHARS = 20000


# ---------------------------------------------------------------- helpers
def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("oauth", "service_account"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(
            f"DOCS_AUTH_METHOD must be auto, oauth or service_account, got '{AUTH_METHOD}'"
        )
    if SERVICE_ACCOUNT_FILE.exists():
        return "service_account"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Google Docs auth configured. For OAuth run `python auth_oauth.py` once, "
        "or place a service-account JSON at service-account.json "
        "and set DOCS_AUTH_METHOD=service_account."
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


def _docs_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "google-api-python-client not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    creds, _method = _get_credentials()
    return build("docs", "v1", credentials=creds)


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


def _extract_document_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("`document_id` is required — use an `id` from list_documents, or a full Google Docs URL")
    match = DOC_URL_RE.search(value)
    if match:
        return match.group(1)
    return value


def _text_from_structural_elements(elements: list) -> str:
    """Recursively extract plain text from Docs structural elements,
    marking headings with # so structure survives as plain text."""
    parts: list[str] = []
    for element in elements or []:
        if "paragraph" in element:
            para = element["paragraph"]
            style = (para.get("paragraphStyle") or {}).get("namedStyleType", "")
            prefix = ""
            if style.startswith("HEADING_"):
                level = style.rsplit("_", 1)[-1]
                prefix = "#" * int(level) + " " if level.isdigit() else ""
            text = ""
            for run in para.get("elements", []):
                text_run = run.get("textRun")
                if text_run:
                    text += text_run.get("content", "")
            if text.strip():
                parts.append(prefix + text.rstrip("\n"))
            elif text:
                parts.append("")
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    cell_text = _text_from_structural_elements(cell.get("content", []))
                    if cell_text.strip():
                        parts.append(cell_text)
        elif "tableOfContents" in element:
            parts.append(_text_from_structural_elements(element["tableOfContents"].get("content", [])))
        elif "sectionBreak" in element:
            parts.append("")
    return "\n".join(p for p in parts if p != "" or True).strip("\n") if parts else ""


def _doc_to_text(doc: dict) -> str:
    body = doc.get("body", {})
    text = _text_from_structural_elements(body.get("content", []))
    # Collapse runs of blank lines left by empty paragraphs.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _doc_end_index(doc: dict) -> int:
    content = doc.get("body", {}).get("content", [])
    if not content:
        return 1
    # The last element's endIndex is 1 past the final newline.
    return max(1, content[-1].get("endIndex", 2) - 1)


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def docs_status() -> dict:
    """Check which Google Docs auth is configured, without touching any document or revealing secrets."""
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
        "note": "Docs has no App Password mode — Google requires OAuth or a Service Account.",
        "capabilities": [
            "list_documents",
            "get_document",
            "create_document",
            "append_text",
            "replace_text",
        ],
        "error": error,
    }


@mcp.tool()
def list_documents(query: str = "", max_results: int = 10) -> dict:
    """
    Find Google Docs by name. Read-only.

    Args:
        query: Substring of the document name, e.g. "Meeting notes". Empty = most recently modified.
        max_results: How many to return (1-50, default 10). Most recently modified first.
    Returns id, name, modifiedTime and webViewLink for each document — use the id in the other tools.
    """
    try:
        service = _drive_service()
        max_results = max(1, min(int(max_results or 10), MAX_RESULTS))
        q_parts = ["mimeType = 'application/vnd.google-apps.document'", "trashed = false"]
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
        return {
            "ok": True,
            "auth_method": _method(),
            "query": query or "",
            "count": len(result.get("files", [])),
            "documents": result.get("files", []),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_document(document_id: str) -> dict:
    """
    Get a document's title and full plain-text content. Read-only.
    Headings are marked with # so structure survives as plain text.

    Args:
        document_id: The id from list_documents, or a full Google Docs URL.
    Returns title, revision id, URL, text (truncated at 20,000 chars) and its length.
    """
    try:
        service = _docs_service()
        did = _extract_document_id(document_id)
        doc = service.documents().get(documentId=did).execute()
        text = _doc_to_text(doc)
        return {
            "ok": True,
            "auth_method": _method(),
            "document_id": doc.get("documentId"),
            "title": doc.get("title"),
            "revision_id": doc.get("revisionId"),
            "url": f"https://docs.google.com/document/d/{doc.get('documentId')}/edit",
            "text": text[:MAX_TEXT_CHARS],
            "text_truncated": len(text) > MAX_TEXT_CHARS,
            "text_length": len(text),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_document(title: str, text: str = "") -> dict:
    """
    Create a new Google Doc.

    Args:
        title: Title of the new document, e.g. "Meeting notes 2026-11-14".
        text: Optional initial text to put in the document.
    Returns the new document id and URL — share/save it for the other tools.
    """
    try:
        if not title or not str(title).strip():
            raise ValueError("`title` is required")
        service = _docs_service()
        doc = service.documents().create(body={"title": str(title).strip()}).execute()
        did = doc.get("documentId")
        if text and str(text).strip():
            service.documents().batchUpdate(
                documentId=did,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": str(text)}}]},
            ).execute()
        return {
            "ok": True,
            "auth_method": _method(),
            "document_id": did,
            "title": doc.get("title"),
            "url": f"https://docs.google.com/document/d/{did}/edit",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def append_text(document_id: str, text: str) -> dict:
    """
    Add text to the end of a document. A blank line is added first if the
    document does not already end with one, so appended text starts on its own line.

    Args:
        document_id: The id from list_documents, or a full Google Docs URL.
        text: The text to append.
    """
    try:
        if not text or not str(text).strip():
            raise ValueError("`text` is required")
        service = _docs_service()
        did = _extract_document_id(document_id)
        doc = service.documents().get(documentId=did).execute()
        existing = _doc_to_text(doc)
        to_insert = str(text)
        if existing and not existing.endswith("\n") and not to_insert.startswith("\n"):
            to_insert = "\n" + to_insert
        end_index = _doc_end_index(doc)
        service.documents().batchUpdate(
            documentId=did,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": to_insert}}]},
        ).execute()
        return {"ok": True, "auth_method": _method(), "document_id": did, "appended_chars": len(to_insert)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def replace_text(document_id: str, find: str, replace: str, match_case: bool = False) -> dict:
    """
    Find-and-replace text in a document. Replaces every occurrence.

    Args:
        document_id: The id from list_documents, or a full Google Docs URL.
        find: The text to find.
        replace: The text to replace it with (empty string deletes it).
        match_case: Match case exactly (default False).
    Returns how many occurrences were replaced.
    """
    try:
        if not find or not str(find).strip():
            raise ValueError("`find` is required")
        if replace is None:
            raise ValueError("`replace` is required (use '' to delete the text)")
        service = _docs_service()
        did = _extract_document_id(document_id)
        result = service.documents().batchUpdate(
            documentId=did,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": str(find), "matchCase": bool(match_case)},
                            "replaceText": str(replace),
                        }
                    }
                ]
            },
        ).execute()
        occurrences = 0
        for reply in result.get("replies", []):
            occurrences += reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
        return {
            "ok": True,
            "auth_method": _method(),
            "document_id": did,
            "find": str(find),
            "occurrences_replaced": occurrences,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
