# Google Calendar MCP Server — self-hosted, read + write

An MCP server that lets an AI agent read and write your Google Calendar,
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Calendar API. Nothing else ever sees your
credentials or your calendar.

## What it does

| Tool | What it does |
|---|---|
| `list_calendars()` | Calendars you can access — id, name, timezone, access role |
| `list_events(calendar_id?, time_min?, time_max?, max_results?, query?)` | Events, soonest first. Defaults: `primary`, now → 7 days ahead |
| `get_event(calendar_id?, event_id)` | One event in full |
| `create_event(summary, start, end, calendar_id?, description?, location?, attendees?, timezone?, all_day?)` | Create an event, optionally inviting attendees |
| `update_event(event_id, calendar_id?, summary?, start?, end?, description?, location?, attendees?, timezone?, all_day?)` | Patch an event — only given fields change |
| `delete_event(calendar_id?, event_id)` | Delete an event |
| `calendar_status()` | Checks config without touching any calendar or revealing secrets |

## Auth — no App Password for Calendar

Like Sheets, Google offers **no App Password login for Calendar**.
Calendar requires OAuth 2.0 or a Service Account. Both are
direct-to-Google and self-hosted here — pick one:

Set `CALENDAR_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — Service Account if `service-account.json` exists, otherwise OAuth if `token.json` exists
- `oauth` — your own Google account
- `service_account` — a Google Cloud service account

### Option 1 — OAuth 2.0 (best for your personal calendar)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project (or reuse one)
2. APIs & Services → Library → enable **Google Calendar API**
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```bash
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve the `calendar` scope.
   That creates `token.json` locally.
7. In `.env`:
   ```
   CALENDAR_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

The scope is `calendar` — it can read your calendars/events and
create/update/delete events. It cannot access Gmail, Drive, or anything else.

### Option 2 — Service Account (best for automation / shared calendars)

1. Google Cloud Console → IAM & Admin → Service Accounts → Create service account
2. Keys → Add key → **JSON** → download, save as `service-account.json` in this folder
3. Open the calendar in Google Calendar → Settings → Share with specific
   people → add the service account's email (the `client_email` inside
   that JSON) → **Make changes to events**
4. In `.env`:
   ```
   CALENDAR_AUTH_METHOD=service_account
   ```
5. No browser login, no token to refresh. Use that calendar's id in
   `calendar_id` — a service account has no `primary` calendar of its own
   unless you share yours with it. To revoke: remove the sharing, or delete the key in Google Cloud.

## Times

ISO 8601. Timed events:

```
2026-11-15T09:00:00+07:00
2026-11-15T09:00:00        # timezone from CALENDAR_TIMEZONE (default Asia/Saigon)
```

All-day events — pass dates, with `all_day=True` (or date-only start/end):

```
create_event("Holiday", start="2026-12-24", end="2026-12-24", all_day=True)
# end is exclusive in Google Calendar, so a same-day end is treated as 1 day
```

## Examples

```python
list_calendars()
list_events()                                            # primary, next 7 days
list_events(query="dentist", time_min="2026-11-01T00:00:00+07:00", time_max="2026-12-01T00:00:00+07:00")
get_event(event_id="abc123")

create_event("Dentist", start="2026-11-20T09:00:00+07:00", end="2026-11-20T10:00:00+07:00",
             location="123 Main St", description="Check-up")
create_event("Team sync", start="2026-11-18T14:00:00+07:00", end="2026-11-18T15:00:00+07:00",
             attendees=["alice@example.com", "bob@example.com"])

update_event(event_id="abc123", start="2026-11-20T10:00:00+07:00", end="2026-11-20T11:00:00+07:00")
update_event(event_id="abc123", location="456 Other St")  # only location changes
delete_event(event_id="abc123")
```

Attendees get an invite email when an event is created/updated/deleted with them on it (`sendUpdates=all`).

## Run it

```bash
cd google-mcp/calendar-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp env.example .env   # then edit .env
.venv/bin/python server.py
```

> Note: `requirements.txt` pins `mcp>=1.9,<2`. `mcp` 2.x renamed
> `FastMCP` to `MCPServer`; `server.py` handles both, but the pin
> avoids surprise API changes.

## Wire it into an MCP client

Example for Claude Desktop / Cursor (`claude_desktop_config.json` or `mcp.json` — see `mcp.json` in this folder, edit the absolute path):

```json
{
  "mcpServers": {
    "google-calendar": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/calendar-mcp/server.py"],
      "env": {
        "CALENDAR_AUTH_METHOD": "auto"
      }
    }
  }
}
```

Prefer the `.env` file over putting secrets in the client config. For OAuth, run `auth_oauth.py` once first.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or `service-account.json` — they are in `.gitignore`.
- The OAuth token is scoped to `calendar` only.
- A Service Account only sees calendars shared with it.
- Errors never include your token or key.
- `create_event` / `update_event` / `delete_event` **do** modify your calendar and can send invites — the agent should confirm the calendar, time and attendees before writing, especially on shared calendars.

## Files

- `server.py` — the MCP server (read + write, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `calendar_status` registered
- `calendar_status()` with no credentials fails gracefully, without crashing

Live read/write was **not** tested — that needs your real Google credentials, which I don't have and didn't ask for in chat. Try `calendar_status()` first, then `list_calendars()`.
