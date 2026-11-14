# Google Sheets MCP Server — self-hosted, read + write

An MCP server that lets an AI agent read and write your Google Sheets,
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Sheets API (and the Drive API only to
list/find sheets by name). Nothing else ever sees your credentials
or your sheets.

## What it does

| Tool | What it does |
|---|---|
| `list_spreadsheets(query?, max_results?)` | Find sheets by name — most recently modified first |
| `get_spreadsheet(spreadsheet_id)` | Metadata + tabs: titles, row/column counts |
| `read_range(spreadsheet_id, range, as_objects?)` | Read values from a range, optionally as a list of dicts (first row = headers) |
| `update_range(spreadsheet_id, range, values, value_input_option?)` | Overwrite a range |
| `append_rows(spreadsheet_id, range, values, value_input_option?)` | Append rows to the end of a table |
| `clear_range(spreadsheet_id, range)` | Clear values in a range (not formatting) |
| `create_spreadsheet(title, sheet_titles?)` | Create a new spreadsheet |
| `add_sheet(spreadsheet_id, title, rows?, columns?)` | Add a tab to a spreadsheet |
| `sheets_status()` | Checks config without touching any sheet or revealing secrets |

A `spreadsheet_id` is the id from `list_spreadsheets`, or a full
Google Sheets URL — the server extracts the id from
`https://docs.google.com/spreadsheets/d/<id>/...` for you.

## Auth — no App Password for Sheets

Unlike Gmail, Google offers **no App Password / SMTP-style login
for Sheets**. Sheets requires OAuth 2.0 or a Service Account.
Both are direct-to-Google and self-hosted here — pick one:

Set `SHEETS_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — Service Account if `service-account.json` exists, otherwise OAuth if `token.json` exists
- `oauth` — your own Google account
- `service_account` — a Google Cloud service account

### Option 1 — OAuth 2.0 (best for your personal sheets)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project (or reuse one)
2. APIs & Services → Library → enable **Google Sheets API** and **Google Drive API** (Drive is only used to list/find sheets by name)
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```bash
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve `spreadsheets` + `drive.readonly`.
   That creates `token.json` locally.
7. In `.env`:
   ```
   SHEETS_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

Scopes are `spreadsheets` (read/write your sheets) + `drive.readonly`
(list/find sheets). The token cannot access anything else.

### Option 2 — Service Account (best for automation / shared sheets)

1. Google Cloud Console → IAM & Admin → Service Accounts → Create service account
2. Keys → Add key → **JSON** → download, save as `service-account.json` in this folder
3. Open each sheet you want it to touch → Share → paste the service
   account's email (the `client_email` inside that JSON, it looks like
   `...@....iam.gserviceaccount.com`) → Viewer or Editor
4. In `.env`:
   ```
   SHEETS_AUTH_METHOD=service_account
   ```
5. No browser login, no token to refresh. To revoke: stop sharing the sheet, or delete the key in Google Cloud.

> A service account can **only** see sheets explicitly shared with it (or
> sheets it creates itself). If a tool returns 404 / permission denied,
> share that sheet with the service account email first.

## Examples

```python
list_spreadsheets()                              # 10 most recently modified
list_spreadsheets(query="Budget")                # find by name
get_spreadsheet("1aBcDeFgHiJkLmNoPqRsTuVwXyZ")   # tabs + metadata
# or pass the full URL instead of the id

read_range("1aBcDe...", "Sheet1!A1:D10")
read_range("1aBcDe...", "Sheet1!A1:D10", as_objects=True)
# as_objects=True turns the first row into keys:
# [{"Name": "Alice", "Age": "30"}, ...]

update_range("1aBcDe...", "Sheet1!A1:B2", [["Name", "Age"], ["Alice", 30]])
append_rows("1aBcDe...", "Sheet1!A:B", [["Bob", 25], ["Carol", 31]])
clear_range("1aBcDe...", "Sheet1!A1:D10")

create_spreadsheet("Monthly Budget", sheet_titles=["2026", "Summary"])
add_sheet("1aBcDe...", "February", rows=500, columns=10)
```

`value_input_option` (for `update_range` / `append_rows`):
- `USER_ENTERED` (default) — parses numbers, dates and formulas like typing in Sheets
- `RAW` — stores values exactly as given, no parsing

## Run it

```bash
cd google-mcp/sheets-mcp
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
    "googlesheet": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/sheets-mcp/server.py"],
      "env": {
        "SHEETS_AUTH_METHOD": "auto"
      }
    }
  }
}
```

Prefer the `.env` file over putting secrets in the client config. For OAuth, run `auth_oauth.py` once first. For a Service Account, put `service-account.json` in this folder and share your sheets with its email.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or `service-account.json` — they are in `.gitignore`.
- The OAuth token is scoped to `spreadsheets` + `drive.readonly` — it cannot read your Gmail, Drive files, or anything else.
- A Service Account only sees sheets shared with it.
- Errors never include your token or key.
- `update_range` / `append_rows` / `clear_range` **do** modify sheets — the agent should confirm the spreadsheet id and range before writing, especially on shared sheets.

## Files

- `server.py` — the MCP server (read + write, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `list_spreadsheets`, `get_spreadsheet`, `read_range`, `update_range`, `append_rows`, `clear_range`, `create_spreadsheet`, `add_sheet`, `sheets_status` registered
- `sheets_status()` with no credentials fails gracefully, without crashing

Live read/write was **not** tested — that needs your real Google credentials, which I don't have and didn't ask for in chat. Try `sheets_status()` first, then `list_spreadsheets()`.
