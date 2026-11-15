# google-mcp

Self-hosted MCP servers for Google services — **no third-party service
in the middle**. Each server runs locally over stdio and talks directly
to Google. One repo, one folder per service.

| Folder | Service | Auth | Tools |
|---|---|---|---|
| [`gmail-mcp/`](./gmail-mcp) | Gmail — send + read | App Password (SMTP/IMAP) or OAuth 2.0 | `send_email`, `list_emails`, `get_email`, `gmail_status` |
| [`sheets-mcp/`](./sheets-mcp) | Google Sheets — read + write | OAuth 2.0 or Service Account | `list_spreadsheets`, `get_spreadsheet`, `read_range`, `update_range`, `append_rows`, `clear_range`, `create_spreadsheet`, `add_sheet`, `sheets_status` |
| [`calendar-mcp/`](./calendar-mcp) | Google Calendar — read + write | OAuth 2.0 or Service Account | `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `calendar_status` |
| [`drive-mcp/`](./drive-mcp) | Google Drive — read + write | OAuth 2.0 or Service Account | `list_files`, `get_file`, `read_file`, `upload_file`, `create_folder`, `copy_file`, `rename_file`, `trash_file`, `share_file`, `drive_status` |
| [`docs-mcp/`](./docs-mcp) | Google Docs — read + write | OAuth 2.0 or Service Account | `list_documents`, `get_document`, `create_document`, `append_text`, `replace_text`, `docs_status` |
| [`appscript-mcp/`](./appscript-mcp) | Google Apps Script — read + write | OAuth 2.0 or Service Account | `list_projects`, `get_project`, `get_project_content`, `create_project`, `update_project_content`, `create_deployment`, `list_deployments`, `appscript_status` |

Each folder is a standalone MCP server with its own `server.py`,
`auth_oauth.py`, `requirements.txt`, `env.example`, `mcp.json` and
README — run only the ones you need, each with its own `.env` /
`token.json` / `credentials.json` in its folder.

## Why one repo

These servers share the same pattern (local stdio MCP + direct Google
API, OAuth or Service Account, `mcp>=1.9,<2`), the same trust model
(self-hosted, no middleman), and the same Google Cloud project can
provide the OAuth client for all of them. Keeping them together avoids
a repo per service.

The old standalone repos are superseded by this one:
`quyentho/mail-mcp` (Gmail) is now `gmail-mcp/` here, and the Sheets
server that was going to be `quyentho/googlesheet-mcp` is `sheets-mcp/`
here instead.

## Quick start (per service)

```bash
git clone https://github.com/quyentho/google-mcp.git
cd google-mcp/<service>          # gmail-mcp, sheets-mcp, calendar-mcp, drive-mcp, docs-mcp, appscript-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp env.example .env              # then edit .env
# OAuth only, once per service:
python auth_oauth.py             # creates token.json (not needed for App Password or Service Account)
.venv/bin/python server.py
```

See each folder's README for its auth setup, scopes, examples and
security notes — the auth method differs per service:

- **Gmail** is the only one with an App Password option (SMTP/IMAP).
  Sheets, Calendar, Drive, Docs and Apps Script have no password login from Google, so they
  use OAuth 2.0 or a Service Account.
- **Sheets / Calendar / Drive / Docs / Apps Script, Service Account:** share the sheet /
  calendar / file / document / script project with the service account's email first — it can only see what is
  shared with it.
- **Docs vs Drive:** Docs edits document *content*; Drive manages
  *files* (upload, move, rename, trash, share). Use both together for documents.
- **Apps Script:** manages script project code and deployments only — it does
  not execute script functions. File operations for a script project live in Drive.

## Wire it into an MCP client

Example for Claude Desktop / Cursor — add one entry per service you use:

```json
{
  "mcpServers": {
    "gmail-smtp": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/gmail-mcp/server.py"],
      "env": { "GMAIL_USER": "you@gmail.com", "GMAIL_AUTH_METHOD": "auto" }
    },
    "googlesheet": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/sheets-mcp/server.py"],
      "env": { "SHEETS_AUTH_METHOD": "auto" }
    },
    "google-calendar": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/calendar-mcp/server.py"],
      "env": { "CALENDAR_AUTH_METHOD": "auto" }
    },
    "google-drive": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/drive-mcp/server.py"],
      "env": { "DRIVE_AUTH_METHOD": "auto" }
    },
    "google-docs": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/docs-mcp/server.py"],
      "env": { "DOCS_AUTH_METHOD": "auto" }
    },
    "google-apps-script": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/appscript-mcp/server.py"],
      "env": { "APPSCRIPT_AUTH_METHOD": "auto" }
    }
  }
}
```

Each folder also has a ready `mcp.json` example. Prefer the `.env`
file over putting secrets in the client config.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or
  `service-account.json` — they are in the root `.gitignore` (and each
  folder's `.gitignore`).
- Each OAuth token is scoped to its own service only.
- Write tools **do** modify Gmail / Sheets / Calendar / Drive / Docs / Apps Script — confirm the
  recipient, spreadsheet/range, calendar/time, file, document, or script project before writing,
  especially on shared sheets, calendars and folders.

## Tested

- `python -m py_compile` for all six `server.py` / `auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — all tools registered
  (Gmail 4, Sheets 9, Calendar 7, Drive 10, Docs 6, Apps Script 8 = 44 total)
- `*_status()` with no credentials fails gracefully, without crashing

Live send/read/write was **not** tested — that needs real Google
credentials. Try each server's `*_status()` first, then a read-only
tool (`list_emails`, `list_spreadsheets`, `list_calendars`, `list_files`, `list_documents`, `list_projects`).
