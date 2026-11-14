#!/usr/bin/env python3
"""
Self-hosted Gmail MCP Server — send AND read, no third-party service.

Two auth methods, both direct to Google, no middleman:

  1. App Password — GMAIL_AUTH_METHOD=app_password
     - Send: SMTP smtp.gmail.com:587
     - Read:  IMAP imap.gmail.com:993 (SSL)
     - Turn on 2-Step Verification, create an App Password for Mail.
       Also enable IMAP in Gmail Settings > Forwarding and POP/IMAP.
     - No Google Cloud project needed.

  2. OAuth 2.0 — GMAIL_AUTH_METHOD=oauth
     - Send + Read via Gmail API
     - Create a Desktop OAuth client in Google Cloud, download credentials.json
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scopes: gmail.send + gmail.readonly (read-only, cannot delete/modify)
     - If you already have a token.json from the send-only version,
       delete it and re-run auth_oauth.py to grant the new read scope.

  AUTO (default): uses App Password if GMAIL_APP_PASSWORD is set,
  otherwise OAuth if token.json exists.

Tools:
  send_email(to, subject, body, ...)  — send
  list_emails(query, max_results, unread_only, folder) — list/search, headers + snippet
  get_email(message_id, folder) — read one full message
  gmail_status() — check config without sending/revealing secrets

Env vars (see env.example):
  GMAIL_USER, GMAIL_APP_PASSWORD, GMAIL_AUTH_METHOD=auto|app_password|oauth
  GMAIL_SENDER_NAME, GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE
  GMAIL_SMTP_HOST/PORT, GMAIL_IMAP_HOST/PORT (defaults smtp.gmail.com:587, imap.gmail.com:993)
"""

import base64
import email
import imaplib
import os
import re
import smtplib
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("gmail-smtp")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("gmail-smtp")

BASE_DIR = Path(__file__).parent
SMTP_HOST = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("GMAIL_SMTP_PORT", "587"))
IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("GMAIL_IMAP_PORT", "993"))
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
SENDER_NAME = os.environ.get("GMAIL_SENDER_NAME", "").strip()
AUTH_METHOD = os.environ.get("GMAIL_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN_FILE", BASE_DIR / "token.json"))

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BODY_CHARS = 20000


# ---------------------------------------------------------------- helpers
def _normalise_addresses(value):
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in value]
    addrs = [p for p in parts if p]
    bad = [a for a in addrs if not EMAIL_RE.match(a)]
    if bad:
        raise ValueError(f"Invalid email address(es): {', '.join(bad)}")
    return addrs


def _require_app_password():
    if not GMAIL_USER or not EMAIL_RE.match(GMAIL_USER):
        raise ValueError("GMAIL_USER is missing or invalid. Set it in .env")
    if not GMAIL_APP_PASSWORD or len(GMAIL_APP_PASSWORD) < 16:
        raise ValueError("GMAIL_APP_PASSWORD is missing. Set your 16-char Gmail App Password in .env")


def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("app_password", "oauth"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(f"GMAIL_AUTH_METHOD must be auto, app_password or oauth, got '{AUTH_METHOD}'")
    if GMAIL_APP_PASSWORD:
        return "app_password"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Gmail auth configured. Set GMAIL_APP_PASSWORD for App Password mode, "
        "or run `python auth_oauth.py` once for OAuth mode."
    )


def _get_oauth_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "OAuth libraries not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    if not TOKEN_FILE.exists():
        raise ValueError(f"OAuth token not found at {TOKEN_FILE}. Run once: python auth_oauth.py")
    if not GMAIL_USER:
        raise ValueError("GMAIL_USER is missing. Set it in .env (must match the OAuth account)")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), OAUTH_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise ValueError(
                "OAuth token expired and no refresh token, or scopes changed. "
                "Delete token.json and re-run: python auth_oauth.py"
            )
    # If the stored token lacks the readonly scope, the API will return 403.
    return build("gmail", "v1", credentials=creds)


def _extract_text(msg) -> str:
    """Best-effort plain-text body from a parsed email.message.Message."""
    if msg.is_multipart():
        # Prefer text/plain, fall back to first text/* part.
        fallback = ""
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            try:
                text = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else ""
            if part.get_content_subtype() == "plain" and text.strip():
                return text
            if not fallback and text.strip():
                fallback = text
        return fallback
    try:
        return msg.get_content()
    except Exception:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else str(msg.get_payload() or "")


def _parse_raw_email(raw: bytes, message_id: str, folder: str = "INBOX", flags: str = "") -> dict:
    msg = email.message_from_bytes(raw, policy=email_policy)
    body = _extract_text(msg)
    truncated = len(body) > MAX_BODY_CHARS
    return {
        "id": message_id,
        "folder": folder,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "cc": str(msg.get("Cc", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "unread": "UNSEEN" in flags.upper() or "\\Seen" not in flags,
        "snippet": body.strip().replace("\r", " ").replace("\n", " ")[:200],
        "body": body[:MAX_BODY_CHARS],
        "body_truncated": truncated,
        "has_attachments": any(
            part.get_content_disposition() == "attachment" for part in msg.walk()
        ) if msg.is_multipart() else False,
    }


# ---------------------------------------------------------------- send
def _build_message(to, subject, body, cc=None, bcc=None, is_html=False, reply_to=None):
    to_addrs = _normalise_addresses(to)
    if not to_addrs:
        raise ValueError("`to` is required — at least one recipient")
    if not subject or not str(subject).strip():
        raise ValueError("`subject` is required")
    if body is None or not str(body).strip():
        raise ValueError("`body` is required")
    cc_addrs = _normalise_addresses(cc)
    bcc_addrs = _normalise_addresses(bcc)
    reply_to_addrs = _normalise_addresses(reply_to)

    msg = EmailMessage()
    msg["From"] = formataddr((SENDER_NAME, GMAIL_USER)) if SENDER_NAME else GMAIL_USER
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if reply_to_addrs:
        msg["Reply-To"] = ", ".join(reply_to_addrs)
    msg["Subject"] = str(subject)
    if is_html:
        msg.set_content("This email requires an HTML-capable client.")
        msg.add_alternative(str(body), subtype="html")
    else:
        msg.set_content(str(body))
    return msg, to_addrs, cc_addrs, bcc_addrs


def _send_via_smtp(msg, to_addrs, cc_addrs, bcc_addrs):
    _require_app_password()
    all_recipients = to_addrs + cc_addrs + bcc_addrs
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg, from_addr=GMAIL_USER, to_addrs=all_recipients)
    return {"via": f"{SMTP_HOST}:{SMTP_PORT} (SMTP + App Password)"}


def _send_via_oauth(msg):
    service = _get_oauth_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"via": "Gmail API (OAuth 2.0, scopes gmail.send + gmail.readonly)", "gmail_message_id": result.get("id")}


def _send(to, subject, body, cc=None, bcc=None, is_html=False, reply_to=None):
    msg, to_addrs, cc_addrs, bcc_addrs = _build_message(to, subject, body, cc=cc, bcc=bcc, is_html=is_html, reply_to=reply_to)
    method = _resolve_auth_method()
    info = _send_via_smtp(msg, to_addrs, cc_addrs, bcc_addrs) if method == "app_password" else _send_via_oauth(msg)
    return {"ok": True, "auth_method": method, "from": GMAIL_USER, "to": to_addrs, "cc": cc_addrs,
            "bcc_count": len(bcc_addrs), "subject": str(subject), **info}


# ---------------------------------------------------------------- read: IMAP
def _imap_criteria(query: str, unread_only: bool) -> str:
    """Translate a simple Gmail-style query to IMAP search criteria."""
    criteria = []
    if unread_only:
        criteria.append("UNSEEN")
    q = (query or "").strip()
    if not q:
        criteria.append("ALL")
        return " ".join(criteria)
    # Support from: / subject: / to: prefixes, rest as free text.
    for m in re.finditer(r'(from|to|subject):("([^"]+)"|(\S+))', q, re.IGNORECASE):
        field, val = m.group(1).upper(), (m.group(3) or m.group(4))
        criteria.append(f'{field} "{val}"')
    rest = re.sub(r'(from|to|subject):("([^"]+)"|(\S+))', "", q, flags=re.IGNORECASE).strip()
    rest = re.sub(r'\bis:unread\b', "", rest, flags=re.IGNORECASE).strip()
    if "is:unread" in q.lower():
        if "UNSEEN" not in criteria:
            criteria.append("UNSEEN")
    if rest:
        criteria.append(f'TEXT "{rest}"')
    return " ".join(criteria) if criteria else "ALL"


def _list_via_imap(query, max_results, unread_only, folder, include_body):
    _require_app_password()
    folder = folder or "INBOX"
    max_results = max(1, min(int(max_results or 10), 50))
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        status, _ = mail.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise ValueError(f"Could not open folder '{folder}'. Try INBOX, or '[Gmail]/Sent Mail' for sent mail.")
        status, data = mail.uid("search", None, _imap_criteria(query, unread_only))
        if status != "OK":
            raise ValueError(f"IMAP search failed for query: {query!r}")
        uids = data[0].split() if data and data[0] else []
        uids = uids[-max_results:][::-1]  # newest first
        emails = []
        for uid in uids:
            uid_s = uid.decode()
            if include_body:
                status, fetched = mail.uid("fetch", uid, "(RFC822 FLAGS)")
            else:
                status, fetched = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] FLAGS RFC822.SIZE)")
            if status != "OK" or not fetched or fetched[0] is None:
                continue
            raw = fetched[0][1]
            flags = ""
            for part in fetched:
                if isinstance(part, tuple) and len(part) > 0 and isinstance(part[0], bytes) and b"FLAGS" in part[0]:
                    flags = part[0].decode(errors="ignore")
            parsed = _parse_raw_email(raw, f"imap:{folder}:{uid_s}", folder=folder, flags=flags)
            if not include_body:
                parsed.pop("body", None)
                parsed.pop("body_truncated", None)
            emails.append(parsed)
        return {"ok": True, "auth_method": "app_password", "via": f"{IMAP_HOST}:{IMAP_PORT} (IMAP + App Password)",
                "folder": folder, "query": query or "", "count": len(emails), "emails": emails}


def _get_via_imap(message_id, folder):
    _require_app_password()
    # Accept "imap:INBOX:123" or a bare UID (folder defaults to INBOX).
    folder = folder or "INBOX"
    uid_s = message_id
    if message_id.startswith("imap:"):
        parts = message_id.split(":", 2)
        if len(parts) == 3:
            folder, uid_s = parts[1], parts[2]
    if not uid_s.isdigit():
        raise ValueError(f"For App Password / IMAP mode, message_id must be an IMAP UID like 'imap:INBOX:123', got {message_id!r}")
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        status, _ = mail.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise ValueError(f"Could not open folder '{folder}'")
        status, fetched = mail.uid("fetch", uid_s, "(RFC822 FLAGS)")
        if status != "OK" or not fetched or fetched[0] is None:
            raise ValueError(f"Message not found: {message_id} in {folder}")
        raw = fetched[0][1]
        result = _parse_raw_email(raw, message_id, folder=folder)
        result.update({"ok": True, "auth_method": "app_password", "via": f"{IMAP_HOST}:{IMAP_PORT} (IMAP + App Password)"})
        return result


# ---------------------------------------------------------------- read: Gmail API
def _oauth_header(payload, name):
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _oauth_decode_body(payload) -> str:
    """Recursively find text/plain (else text/html stripped crudely) in a Gmail payload."""
    def decode_part(part):
        data = part.get("body", {}).get("data", "")
        if not data:
            return ""
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")

    text_plain, fallback = "", ""
    for part in [payload] + payload.get("parts", []):
        mime = part.get("mimeType", "")
        if mime == "text/plain" and not text_plain:
            text_plain = decode_part(part)
        elif mime.startswith("multipart/"):
            for sub in part.get("parts", []):
                if sub.get("mimeType") == "text/plain" and not text_plain:
                    text_plain = decode_part(sub)
                elif sub.get("mimeType") == "text/html" and not fallback:
                    fallback = re.sub(r"<[^>]+>", " ", decode_part(sub))
    return text_plain or fallback


def _list_via_oauth(query, max_results, unread_only, folder, include_body):
    service = _get_oauth_service()
    q = (query or "").strip()
    if unread_only and "is:unread" not in q.lower():
        q = f"is:unread {q}".strip()
    # Gmail API labelIds: INBOX is a label; other folders map loosely.
    label_ids = ["INBOX"] if (folder or "INBOX").upper() == "INBOX" else []
    max_results = max(1, min(int(max_results or 10), 50))
    result = service.users().messages().list(
        userId="me", q=q or None, maxResults=max_results, labelIds=label_ids or None
    ).execute()
    emails = []
    for item in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=item["id"], format="full" if include_body else "metadata",
            metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
        ).execute()
        payload = msg.get("payload", {})
        body = _oauth_decode_body(payload) if include_body else ""
        emails.append({
            "id": msg["id"],
            "folder": folder or "INBOX",
            "from": _oauth_header(payload, "From"),
            "to": _oauth_header(payload, "To"),
            "cc": _oauth_header(payload, "Cc"),
            "subject": _oauth_header(payload, "Subject"),
            "date": _oauth_header(payload, "Date"),
            "unread": "UNREAD" in msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
            **({"body": body[:MAX_BODY_CHARS], "body_truncated": len(body) > MAX_BODY_CHARS} if include_body else {}),
        })
    return {"ok": True, "auth_method": "oauth", "via": "Gmail API (OAuth 2.0, gmail.readonly)",
            "folder": folder or "INBOX", "query": q, "count": len(emails), "emails": emails}


def _get_via_oauth(message_id, folder):
    service = _get_oauth_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    body = _oauth_decode_body(payload)
    return {
        "ok": True, "auth_method": "oauth", "via": "Gmail API (OAuth 2.0, gmail.readonly)",
        "id": msg["id"], "folder": folder or "INBOX",
        "from": _oauth_header(payload, "From"), "to": _oauth_header(payload, "To"),
        "cc": _oauth_header(payload, "Cc"), "subject": _oauth_header(payload, "Subject"),
        "date": _oauth_header(payload, "Date"), "unread": "UNREAD" in msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""), "body": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "labels": msg.get("labelIds", []),
    }


def _list_emails(query, max_results, unread_only, folder, include_body):
    method = _resolve_auth_method()
    if method == "app_password":
        return _list_via_imap(query, max_results, unread_only, folder, include_body)
    return _list_via_oauth(query, max_results, unread_only, folder, include_body)


def _get_email(message_id, folder):
    if not message_id or not str(message_id).strip():
        raise ValueError("`message_id` is required — use an `id` from list_emails")
    method = _resolve_auth_method()
    if method == "app_password":
        return _get_via_imap(str(message_id), folder)
    return _get_via_oauth(str(message_id), folder)


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def send_email(to: list[str] | str, subject: str, body: str,
               cc: list[str] | str | None = None, bcc: list[str] | str | None = None,
               is_html: bool = False, reply_to: list[str] | str | None = None) -> dict:
    """
    Send an email from your Gmail account.
    App Password: SMTP. OAuth: Gmail API (gmail.send).
    """
    try:
        return _send(to, subject, body, cc=cc, bcc=bcc, is_html=is_html, reply_to=reply_to)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_emails(query: str = "", max_results: int = 10, unread_only: bool = False,
                folder: str = "INBOX", include_body: bool = False) -> dict:
    """
    List / search emails in your Gmail. Read-only — never deletes or modifies mail.

    Args:
        query: Gmail-style search, e.g. "from:alice@example.com", "subject:invoice",
               "is:unread", or free text. Empty = most recent mail in the folder.
        max_results: How many to return (1-50, default 10). Newest first.
        unread_only: Only unread mail.
        folder: Mailbox folder, default INBOX. IMAP: e.g. "[Gmail]/Sent Mail".
        include_body: Also include the full plain-text body for each message.
    Returns headers, snippet and (optionally) body. Use get_email() for one full message.
    """
    try:
        return _list_emails(query, max_results, unread_only, folder, include_body)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_email(message_id: str, folder: str = "INBOX") -> dict:
    """
    Read one full email by its id from list_emails.
    IMAP mode: id looks like "imap:INBOX:123". OAuth mode: the Gmail message id.
    Returns From/To/Subject/Date, snippet and full plain-text body (truncated at 20,000 chars).
    """
    try:
        return _get_email(message_id, folder)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def gmail_status() -> dict:
    """Check which Gmail auth is configured, without sending/reading anything or revealing secrets."""
    try:
        method = _resolve_auth_method()
    except Exception as e:
        method, error = None, str(e)
    else:
        error = None
    return {
        "configured": method is not None,
        "auth_method_requested": AUTH_METHOD,
        "auth_method_resolved": method,
        "user": GMAIL_USER or None,
        "smtp": f"{SMTP_HOST}:{SMTP_PORT}",
        "imap": f"{IMAP_HOST}:{IMAP_PORT}",
        "sender_name": SENDER_NAME or None,
        "app_password_set": bool(GMAIL_APP_PASSWORD),
        "oauth_token_exists": TOKEN_FILE.exists(),
        "oauth_credentials_exists": CREDENTIALS_FILE.exists(),
        "oauth_scopes": OAUTH_SCOPES,
        "capabilities": ["send_email", "list_emails", "get_email"],
        "error": error,
    }


if __name__ == "__main__":
    mcp.run()
