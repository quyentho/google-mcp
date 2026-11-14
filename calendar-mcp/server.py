#!/usr/bin/env python3
"""
Self-hosted Google Calendar MCP Server — read and write, no third-party service.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Calendar API — with OAuth 2.0 or a
Service Account. Nothing else ever sees your credentials or your
calendar.

Like Sheets, there is NO App Password mode for Calendar: Google
offers no password protocol for it. Calendar requires an OAuth
token or a Service Account. Both are direct-to-Google, self-hosted.

  1. OAuth 2.0 — CALENDAR_AUTH_METHOD=oauth
     - Your own Google account, Desktop OAuth client in Google Cloud.
     - Run once: python auth_oauth.py  -> creates token.json locally
     - Scope: calendar (read calendars/events, create/update/delete events)
     - Best for: your personal calendar.

  2. Service Account — CALENDAR_AUTH_METHOD=service_account
     - A Google Cloud service account, download its JSON key as
       service-account.json in this folder.
     - Share the calendar you want it to touch with the service
       account's email (Settings > Share with specific people),
       "Make changes to events" permission.
     - No browser login, no token refresh, good for automation.
     - Best for: shared / automated calendars, no interactive login.
     - Note: a service account cannot see your "primary" calendar
       unless you share it with it. Use the shared calendar's id
       (usually the owner's email) as calendar_id.

  AUTO (default): Service Account if service-account.json exists,
  otherwise OAuth if token.json exists.

Tools:
  list_calendars() — calendars you can access
  list_events(calendar_id, time_min, time_max, max_results, query) — upcoming events
  get_event(calendar_id, event_id) — one event in full
  create_event(calendar_id, summary, start, end, ...) — create an event
  update_event(calendar_id, event_id, ...) — patch an event (only given fields)
  delete_event(calendar_id, event_id) — delete an event
  calendar_status() — check config without touching any calendar or revealing secrets

Times: ISO 8601, e.g. "2026-11-15T09:00:00+07:00" or "2026-11-15T09:00:00"
(a timezone is assumed from CALENDAR_TIMEZONE if none is given).
All-day: pass dates "2026-11-15" with all_day=True, or a date-only start/end.
The default calendar_id is "primary" (your main calendar, OAuth mode).

Env vars (see env.example):
  CALENDAR_AUTH_METHOD=auto|oauth|service_account
  CALENDAR_CREDENTIALS_FILE, CALENDAR_TOKEN_FILE, CALENDAR_SERVICE_ACCOUNT_FILE
  CALENDAR_TIMEZONE (default Asia/Saigon)
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# mcp 1.x: FastMCP. mcp 2.x renamed it to MCPServer — support both.
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("google-calendar")
except (ImportError, ModuleNotFoundError):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("google-calendar")

BASE_DIR = Path(__file__).parent
AUTH_METHOD = os.environ.get("CALENDAR_AUTH_METHOD", "auto").strip().lower() or "auto"
CREDENTIALS_FILE = Path(os.environ.get("CALENDAR_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("CALENDAR_TOKEN_FILE", BASE_DIR / "token.json"))
SERVICE_ACCOUNT_FILE = Path(
    os.environ.get("CALENDAR_SERVICE_ACCOUNT_FILE", BASE_DIR / "service-account.json")
)
DEFAULT_TIMEZONE = os.environ.get("CALENDAR_TIMEZONE", "Asia/Saigon").strip() or "Asia/Saigon"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_RESULTS = 50
MAX_DESC_CHARS = 5000


# ---------------------------------------------------------------- helpers
def _resolve_auth_method() -> str:
    if AUTH_METHOD in ("oauth", "service_account"):
        return AUTH_METHOD
    if AUTH_METHOD not in ("auto", ""):
        raise ValueError(
            f"CALENDAR_AUTH_METHOD must be auto, oauth or service_account, got '{AUTH_METHOD}'"
        )
    if SERVICE_ACCOUNT_FILE.exists():
        return "service_account"
    if TOKEN_FILE.exists():
        return "oauth"
    raise ValueError(
        "No Google Calendar auth configured. For OAuth run `python auth_oauth.py` once, "
        "or place a service-account JSON at service-account.json "
        "and set CALENDAR_AUTH_METHOD=service_account."
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


def _calendar_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ValueError(
            "google-api-python-client not installed. Run: pip install -r requirements.txt. "
            f"Original error: {e}"
        )
    creds, _method = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def _method() -> str:
    return _resolve_auth_method()


def _normalise_attendees(value):
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in value]
    addrs = [p for p in parts if p]
    bad = [a for a in addrs if not EMAIL_RE.match(a)]
    if bad:
        raise ValueError(f"Invalid attendee email address(es): {', '.join(bad)}")
    return [{"email": a} for a in addrs]


def _parse_iso(value: str) -> datetime:
    value = (value or "").strip()
    if not value:
        raise ValueError("A start/end time is required")
    if DATE_RE.match(value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # fromisoformat handles +07:00, and "Z" from Python 3.11+
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Invalid time {value!r} — use ISO 8601, e.g. '2026-11-15T09:00:00+07:00' or a date '2026-11-15'"
        )
    return dt


def _event_time(value: str, timezone_name: str, all_day: bool) -> dict:
    """Build a Calendar API start/end object from an ISO string or date."""
    value = (value or "").strip()
    if not value:
        raise ValueError("A start/end time is required")
    if all_day or DATE_RE.match(value):
        date_part = value[:10]
        # Validate the date parses.
        datetime.strptime(date_part, "%Y-%m-%d")
        return {"date": date_part}
    dt = _parse_iso(value)
    if dt.tzinfo is None:
        # Naive datetime: attach the configured timezone name; the API
        # interprets dateTime + timeZone together.
        return {"dateTime": dt.isoformat(), "timeZone": timezone_name or DEFAULT_TIMEZONE}
    return {"dateTime": dt.isoformat(), "timeZone": timezone_name or DEFAULT_TIMEZONE}


def _all_day_end(start: str, end: str) -> str:
    """Calendar API all-day end dates are exclusive: a 1-day event
    2026-11-15 -> end 2026-11-16. If the user gave the same date for
    both, bump the end by one day so the event actually shows up."""
    start_date = datetime.strptime(start[:10], "%Y-%m-%d").date()
    end_date = datetime.strptime(end[:10], "%Y-%m-%d").date()
    if end_date <= start_date:
        end_date = start_date + timedelta(days=1)
    return end_date.isoformat()


def _format_event(item: dict) -> dict:
    start = item.get("start", {})
    end = item.get("end", {})
    return {
        "id": item.get("id"),
        "summary": item.get("summary", "(no title)"),
        "status": item.get("status"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "start_timezone": start.get("timeZone"),
        "end_timezone": end.get("timeZone"),
        "all_day": "date" in start,
        "location": item.get("location", ""),
        "description": (item.get("description") or "")[:MAX_DESC_CHARS],
        "attendees": [a.get("email") for a in item.get("attendees", []) if a.get("email")],
        "creator": (item.get("creator") or {}).get("email"),
        "organizer": (item.get("organizer") or {}).get("email"),
        "html_link": item.get("htmlLink"),
        "recurring_event_id": item.get("recurringEventId"),
    }


def _require_calendar_id(calendar_id: str) -> str:
    calendar_id = (calendar_id or "").strip() or "primary"
    return calendar_id


# ---------------------------------------------------------------- MCP tools
@mcp.tool()
def calendar_status() -> dict:
    """Check which Google Calendar auth is configured, without touching any calendar or revealing secrets."""
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
        "default_timezone": DEFAULT_TIMEZONE,
        "oauth_token_exists": TOKEN_FILE.exists(),
        "oauth_credentials_exists": CREDENTIALS_FILE.exists(),
        "service_account_file_exists": SERVICE_ACCOUNT_FILE.exists(),
        "service_account_email": service_account_email,
        "oauth_scopes": SCOPES,
        "note": "Calendar has no App Password mode — Google requires OAuth or a Service Account.",
        "capabilities": [
            "list_calendars",
            "list_events",
            "get_event",
            "create_event",
            "update_event",
            "delete_event",
        ],
        "error": error,
    }


@mcp.tool()
def list_calendars() -> dict:
    """
    List the calendars you can access: your primary calendar and any
    calendars shared with you (or with the service account).
    Returns each calendar's id (use it as calendar_id), summary, timezone and access role.
    """
    try:
        service = _calendar_service()
        result = service.calendarList().list().execute()
        calendars = [
            {
                "id": c.get("id"),
                "summary": c.get("summary"),
                "description": c.get("description", ""),
                "time_zone": c.get("timeZone"),
                "primary": bool(c.get("primary")),
                "access_role": c.get("accessRole"),
            }
            for c in result.get("items", [])
        ]
        return {"ok": True, "auth_method": _method(), "count": len(calendars), "calendars": calendars}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
    query: str = "",
) -> dict:
    """
    List events on a calendar. Read-only.

    Args:
        calendar_id: Calendar id from list_calendars, or "primary" for your main calendar.
        time_min: Earliest event start, ISO 8601, e.g. "2026-11-15T00:00:00+07:00".
            Empty = now.
        time_max: Latest event start, ISO 8601. Empty = 7 days from time_min.
        max_results: How many to return (1-50, default 10). Soonest first.
        query: Free-text search over summary, description and location.
    """
    try:
        service = _calendar_service()
        cal_id = _require_calendar_id(calendar_id)
        max_results = max(1, min(int(max_results or 10), MAX_RESULTS))
        now = datetime.now(timezone.utc)
        start_dt = _parse_iso(time_min) if time_min and time_min.strip() else now
        end_dt = (
            _parse_iso(time_max)
            if time_max and time_max.strip()
            else start_dt + timedelta(days=7)
        )
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        result = (
            service.events()
            .list(
                calendarId=cal_id,
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                q=query.strip() or None,
            )
            .execute()
        )
        events = [_format_event(item) for item in result.get("items", [])]
        return {
            "ok": True,
            "auth_method": _method(),
            "calendar_id": cal_id,
            "time_min": start_dt.isoformat(),
            "time_max": end_dt.isoformat(),
            "count": len(events),
            "events": events,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def get_event(calendar_id: str = "primary", event_id: str = "") -> dict:
    """
    Get one event in full by its id from list_events.

    Args:
        calendar_id: Calendar id, or "primary".
        event_id: The event id.
    """
    try:
        service = _calendar_service()
        cal_id = _require_calendar_id(calendar_id)
        if not event_id or not str(event_id).strip():
            raise ValueError("`event_id` is required — use an `id` from list_events")
        result = service.events().get(calendarId=cal_id, eventId=str(event_id).strip()).execute()
        return {"ok": True, "auth_method": _method(), "calendar_id": cal_id, **_format_event(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: list[str] | str | None = None,
    timezone: str = "",
    all_day: bool = False,
) -> dict:
    """
    Create an event.

    Args:
        summary: Event title, e.g. "Dentist".
        start: Start time, ISO 8601, e.g. "2026-11-15T09:00:00+07:00",
            or a date "2026-11-15" for an all-day event.
        end: End time, same format. For all-day events the end date is
            exclusive in Google Calendar — an end equal to the start is
            treated as a 1-day event.
        calendar_id: Calendar id, or "primary".
        description: Optional notes.
        location: Optional location.
        attendees: Optional list (or comma-separated string) of email addresses to invite.
        timezone: Timezone for times without an offset, e.g. "Asia/Saigon".
            Defaults to CALENDAR_TIMEZONE.
        all_day: If True, start/end are treated as dates.
    Returns the new event id and link.
    """
    try:
        if not summary or not str(summary).strip():
            raise ValueError("`summary` is required")
        tz = (timezone or "").strip() or DEFAULT_TIMEZONE
        is_all_day = bool(all_day) or (DATE_RE.match((start or "").strip()) and DATE_RE.match((end or "").strip()))
        if is_all_day:
            if not DATE_RE.match((start or "").strip()) or not DATE_RE.match((end or "").strip()):
                raise ValueError("All-day events need date start/end, e.g. start='2026-11-15', end='2026-11-16'")
            start_obj = {"date": (start or "").strip()[:10]}
            end_obj = {"date": _all_day_end(start, end)}
        else:
            start_obj = _event_time(start, tz, False)
            end_obj = _event_time(end, tz, False)
        body = {
            "summary": str(summary).strip(),
            "start": start_obj,
            "end": end_obj,
        }
        if description:
            body["description"] = str(description)
        if location:
            body["location"] = str(location)
        attendee_list = _normalise_attendees(attendees)
        if attendee_list:
            body["attendees"] = attendee_list
        service = _calendar_service()
        cal_id = _require_calendar_id(calendar_id)
        result = service.events().insert(calendarId=cal_id, body=body, sendUpdates="all" if attendee_list else "none").execute()
        return {"ok": True, "auth_method": _method(), "calendar_id": cal_id, **_format_event(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | str | None = None,
    timezone: str = "",
    all_day: bool = False,
) -> dict:
    """
    Update an event — only the fields you pass are changed, the rest stay as they are.

    Args:
        event_id: The event id from list_events.
        calendar_id: Calendar id, or "primary".
        summary, description, location: New values (omit to leave unchanged).
        start, end: New times, ISO 8601. Pass both together to move the event.
            Dates "2026-11-15" with all_day=True for an all-day event.
        attendees: New attendee list (replaces the old list). Omit to leave unchanged.
        timezone: Timezone for times without an offset.
        all_day: If True, start/end are treated as dates.
    """
    try:
        if not event_id or not str(event_id).strip():
            raise ValueError("`event_id` is required — use an `id` from list_events")
        tz = (timezone or "").strip() or DEFAULT_TIMEZONE
        body: dict = {}
        if summary is not None:
            if not str(summary).strip():
                raise ValueError("`summary` cannot be empty")
            body["summary"] = str(summary).strip()
        if description is not None:
            body["description"] = str(description)
        if location is not None:
            body["location"] = str(location)
        if attendees is not None:
            body["attendees"] = _normalise_attendees(attendees) or []
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("Pass both `start` and `end` together to move an event")
            is_all_day = bool(all_day) or (DATE_RE.match(start.strip()) and DATE_RE.match(end.strip()))
            if is_all_day:
                body["start"] = {"date": start.strip()[:10]}
                body["end"] = {"date": _all_day_end(start, end)}
            else:
                body["start"] = _event_time(start, tz, False)
                body["end"] = _event_time(end, tz, False)
        if not body:
            raise ValueError("Nothing to update — pass at least one field to change")
        service = _calendar_service()
        cal_id = _require_calendar_id(calendar_id)
        result = (
            service.events()
            .patch(calendarId=cal_id, eventId=str(event_id).strip(), body=body, sendUpdates="all" if "attendees" in body else "none")
            .execute()
        )
        return {"ok": True, "auth_method": _method(), "calendar_id": cal_id, **_format_event(result)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def delete_event(calendar_id: str = "primary", event_id: str = "") -> dict:
    """
    Delete an event.

    Args:
        calendar_id: Calendar id, or "primary".
        event_id: The event id from list_events.
    """
    try:
        service = _calendar_service()
        cal_id = _require_calendar_id(calendar_id)
        if not event_id or not str(event_id).strip():
            raise ValueError("`event_id` is required — use an `id` from list_events")
        service.events().delete(calendarId=cal_id, eventId=str(event_id).strip(), sendUpdates="all").execute()
        return {"ok": True, "auth_method": _method(), "calendar_id": cal_id, "deleted_event_id": str(event_id).strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
