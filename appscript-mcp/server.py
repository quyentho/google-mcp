#!/usr/bin/env python3
"""
Self-hosted Google Apps Script MCP Server — read and write, no third-party service.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Apps Script API (and the Drive API only
to list/find script projects by name) — with OAuth 2.0 or a Service
Account. Nothing else ever sees your credentials or your scripts.

Like Sheets, Calendar, Drive and Docs, there is NO App Password mode
for Apps Script: Google offers no password protocol for it. Apps
Script requires an OAuth token or a Service Account. Both are
direct-to-Google, self-hosted.

  1. OAuth 2.0 — APPSCRIPT_AUTH_METHOD=oauth
     - Your own Google account, Desktop OAuth client in Google Cloud.
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scopes: script.projects + script.deployments (manage projects
       and deployments) + drive.readonly (list/find projects)
     - Best for: your personal script projects.

  2. Service Account — APPSCRIPT_AUTH_METHOD=service_account
     - A Google Cloud service account, download its JSON key as
       service-account.json in this folder.
     - Share each script project you want it to touch with the service
       account's email (like sharing with a person), Editor.
     - No browser login, no token refresh, good for automation.
     - Best for: shared / automated script projects, no interactive login.

  AUTO (default): Service Account if service-account.json exists,
  otherwise OAuth if token.json exists.

Tools:
  list_projects(query, max_results) — find script projects by name (Drive search)
  get_project(script_id) — metadata for one project
  get_project_content(script_id) — all files in a project (code, HTML, manifest)
  create_project(title, parent_folder_id) — create a script project
  update_project_content(script_id, files) — replace a project's files (whole project)
  create_deployment(script_id, version_number, description) — deploy a version
  list_deployments(script_id) — deployments of a project
  appscript_status() — check config without touching any project or revealing secrets

A script_id is the id from list_projects, or a full Apps Script URL —
the server extracts the id from
https://script.google.com/d/<id>/edit and
https://script.google.com/home/projects/<id> for you.
For file operations (move, rename, trash, share) use the Drive MCP
server (drive-mcp/) — Apps Script here is for editing project code.

This server manages project code and deployments. It does NOT execute
a script function (scripts.run) — that needs a separate execution
setup with a deployed executable / web app.

Env vars (see env.example):
  APPSCRIPT_AUTH_METHOD=auto|oauth|service_account
  APPSCRIPT_CREDENTIALS_FILE, APPSCRIPT_TOKEN_FILE, APPSCRIPT_SERVICE_ACCOUNT_FILE
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("google-apps-script")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("google-apps-script")

BASE_DIR = Path(__file__).parent
AUTH_METHOD = os.environ.get("APPSCRIPT_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("APPSCRIPT_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("APPSCRIPT_TOKEN_FILE", BASE_DIR / "token.json"))
SERVICE_ACCOUNT_FILE = Path(
    os.environ.get("APPSCRIPT_SERVICE_ACCOUNT_FILE", BASE_DIR / "service-account.json")
)

SCOPES = [
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/drive.readonly",
]

SCRIPT_URL_RE = re.compile(r"/(?:d|home/projects)/([a-zA-Z0-9-_]+)")
MAX_RESULTS = 50
MAX_SOURCE_CHARS = 20000
VALID_FILE_TYPES = ("SERVER_JS", "HTML", "JSON")


# ---------------------------------------------------------------- helpers
def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("oauth", "service_account"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(
            f"APPSCRIPT_AUTH_METHOD must be auto, oauth or service_account, got '{AUTH_METHOD}'"
        )
    if SERVICE_ACCOUNT_FILE.exists():
        return "service_account"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Google Apps Script auth configured. For OAuth run `python auth_oauth.py` once, "
        "or place a service-account JSON at service-account.json "
        "and set APPSCRIPT_AUTH_METHOD=service_account."
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


def _script_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "google-api-python-client not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    creds, _method = _get_credentials()
    return build("script", "v1", credentials=creds)


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


def _extract_script_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("`script_id` is required — use an `id` from list_projects, or a full Apps Script URL")
    match = SCRIPT_URL_RE.search(value)
    if match:
        return match.group(1)
    return value


def _validate_files(files) -> list:
    if not files or not isinstance(files, list):
        raise ValueError(
            "`files` is required — a list of {name, type, source}, e.g. "
            '[{"name": "Code", "type": "SERVER_JS", "source": "function myFunction() {}"}]'
        )
    validated = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each file must be an object with name, type and source")
        name = str(item.get("name") or "").strip()
        file_type = str(item.get("type") or "").strip().upper()
        source = item.get("source")
        if not name:
            raise ValueError("Each file needs a `name` (without extension, e.g. 'Code', 'Index', 'appsscript')")
        if file_type not in VALID_FILE_TYPES:
            raise ValueError(f"File type must be one of {', '.join(VALID_FILE_TYPES)}, got {file_type!r}")
        if source is None:
            raise ValueError(f"File {name!r} needs a `source` string (use '' for an empty file)")
        validated.append({"name": name, "type": file_type, "source": str(source)})
    return validated


def _format_file(item: dict) -> dict:
    source = item.get("source", "") or ""
    return {
        "name": item.get("name"),
        "type": item.get("type"),
        "source": source[:MAX_SOURCE_CHARS],
        "source_truncated": len(source) > MAX_SOURCE_CHARS,
        "source_length": len(source),
    }


def _format_deployment(item: dict) -> dict:
    config = item.get("deploymentConfig", {}) or {}
    return {
        "deployment_id": item.get("deploymentId"),
        "version_number": config.get("versionNumber"),
        "description": config.get("description", ""),
        "manifest_file_name": config.get("manifestFileName"),
        "script_id": config.get("scriptId"),
        "update_time": item.get("updateTime"),
        "entry_points": item.get("entryPoints", []),
    }


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def appscript_status() -> dict:
    """Check which Google Apps Script auth is configured, without touching any project or revealing secrets."""
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
        "note": "Apps Script has no App Password mode — Google requires OAuth or a Service Account.",
        "capabilities": [
            "list_projects",
            "get_project",
            "get_project_content",
            "create_project",
            "update_project_content",
            "create_deployment",
            "list_deployments",
        ],
        "error": error,
    }


@mcp.tool()
def list_projects(query: str = "", max_results: int = 10) -> dict:
    """
    Find Apps Script projects by name. Read-only.

    Args:
        query: Substring of the project name, e.g. "Invoice mailer". Empty = most recently modified.
        max_results: How many to return (1-50, default 10). Most recently modified first.
    Returns id, name, modifiedTime and webViewLink for each project — use the id in the other tools.
    """
    try:
        service = _drive_service()
        max_results = max(1, min(int(max_results or 10), MAX_RESULTS))
        q_parts = ["mimeType = 'application/vnd.google-apps.script'", "trashed = false"]
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
            "projects": result.get("files", []),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_project(script_id: str) -> dict:
    """
    Get a script project's metadata: title, creator, created/updated time and parent folder.

    Args:
        script_id: The id from list_projects, or a full Apps Script URL.
    """
    try:
        service = _script_service()
        sid = _extract_script_id(script_id)
        result = service.projects().get(scriptId=sid).execute()
        creator = result.get("creator", {}) or {}
        return {
            "ok": True,
            "auth_method": _method(),
            "script_id": result.get("scriptId"),
            "title": result.get("title"),
            "creator_name": creator.get("name"),
            "creator_email": creator.get("email"),
            "create_time": result.get("createTime"),
            "update_time": result.get("updateTime"),
            "parent_id": result.get("parentId"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_project_content(script_id: str) -> dict:
    """
    Get all files in a script project: code (.gs), HTML and the manifest.
    Read-only.

    Args:
        script_id: The id from list_projects, or a full Apps Script URL.
    Returns each file's name, type and source (truncated at 20,000 chars per file).
    Use update_project_content() to write the files back.
    """
    try:
        service = _script_service()
        sid = _extract_script_id(script_id)
        result = service.projects().getContent(scriptId=sid).execute()
        files = [_format_file(f) for f in result.get("files", [])]
        return {
            "ok": True,
            "auth_method": _method(),
            "script_id": result.get("scriptId"),
            "count": len(files),
            "files": files,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_project(title: str, parent_folder_id: str = "") -> dict:
    """
    Create a new Apps Script project.

    Args:
        title: Title of the new project, e.g. "Invoice mailer".
        parent_folder_id: Drive folder to create the project in (id or Drive folders URL).
            Empty = Drive root. The project is created with a default Code.gs and appsscript.json.
    Returns the new script id — share/save it for the other tools.
    """
    try:
        if not title or not str(title).strip():
            raise ValueError("`title` is required")
        body: dict = {"title": str(title).strip()}
        if parent_folder_id and parent_folder_id.strip():
            # Reuse the Drive URL parser pattern for a folder id.
            folder_id = parent_folder_id.strip()
            folder_match = re.search(r"/folders/([a-zA-Z0-9-_]+)", folder_id)
            body["parentId"] = folder_match.group(1) if folder_match else folder_id
        service = _script_service()
        result = service.projects().create(body=body).execute()
        return {
            "ok": True,
            "auth_method": _method(),
            "script_id": result.get("scriptId"),
            "title": result.get("title"),
            "creator_email": (result.get("creator") or {}).get("email"),
            "create_time": result.get("createTime"),
            "parent_id": result.get("parentId"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def update_project_content(script_id: str, files: list) -> dict:
    """
    Replace a script project's files. This overwrites the WHOLE project —
    send the complete file list, not just the changed file. Read the
    current content first with get_project_content(), edit it, then send it all back.

    Args:
        script_id: The id from list_projects, or a full Apps Script URL.
        files: A list of {name, type, source}. Name has no extension, e.g.
            {"name": "Code", "type": "SERVER_JS", "source": "function myFunction() {}"}
            {"name": "Index", "type": "HTML", "source": "<html>...</html>"}
            {"name": "appsscript", "type": "JSON", "source": "{...}"}
            Types: SERVER_JS, HTML, JSON.
    Returns the updated file list (names and types).
    """
    try:
        validated = _validate_files(files)
        service = _script_service()
        sid = _extract_script_id(script_id)
        result = service.projects().updateContent(scriptId=sid, body={"files": validated}).execute()
        return {
            "ok": True,
            "auth_method": _method(),
            "script_id": result.get("scriptId"),
            "files": [{"name": f.get("name"), "type": f.get("type")} for f in result.get("files", [])],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_deployment(script_id: str, version_number: int | None = None, description: str = "") -> dict:
    """
    Create a deployment of a script project.

    Args:
        script_id: The id from list_projects, or a full Apps Script URL.
        version_number: The version to deploy, e.g. 1. Omit to deploy the
            current code as a new version (the API creates a version for you).
            Note: a deployment freezes that version — later code edits need a new deployment.
        description: Optional description for this deployment.
    Returns the deployment id, version and entry points.
    """
    try:
        service = _script_service()
        sid = _extract_script_id(script_id)
        body: dict = {"manifestFileName": "appsscript"}
        if version_number is not None:
            body["versionNumber"] = int(version_number)
        if description and str(description).strip():
            body["description"] = str(description).strip()
        result = service.projects().deployments().create(scriptId=sid, body=body).execute()
        return {"ok": True, "auth_method": _method(), **_format_deployment(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_deployments(script_id: str) -> dict:
    """
    List the deployments of a script project. Read-only.

    Args:
        script_id: The id from list_projects, or a full Apps Script URL.
    Returns each deployment's id, version, description, update time and entry points.
    """
    try:
        service = _script_service()
        sid = _extract_script_id(script_id)
        result = service.projects().deployments().list(scriptId=sid).execute()
        deployments = [_format_deployment(d) for d in result.get("deployments", [])]
        return {
            "ok": True,
            "auth_method": _method(),
            "script_id": sid,
            "count": len(deployments),
            "deployments": deployments,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
