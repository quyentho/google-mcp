# Google Drive MCP Server — self-hosted, read + write

An MCP server that lets an AI agent manage your Google Drive,
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Drive API. Nothing else ever sees your
credentials or your files.

## What it does

| Tool | What it does |
|---|---|
| `list_files(query?, folder_id?, mime_type?, max_results?)` | Find files/folders by name, in a folder, or by type — newest first |
| `get_file(file_id)` | Metadata: name, type, size, owners, modified time, link |
| `read_file(file_id)` | Text content — Docs exported as text, Sheets as CSV, Slides as text; plain-text / JSON / CSV / Markdown downloaded as text (20,000 chars max) |
| `upload_file(local_path, name?, parent_folder_id?, mime_type?)` | Upload a local file (max 100 MB) |
| `create_folder(name, parent_folder_id?)` | Create a folder |
| `copy_file(file_id, name?, parent_folder_id?)` | Copy a file |
| `rename_file(file_id, new_name)` | Rename a file or folder |
| `trash_file(file_id)` | Move to trash — recoverable, not a permanent delete |
| `share_file(file_id, email, role?, notify?)` | Share with a person (reader / commenter / writer) |
| `drive_status()` | Checks config without touching any file or revealing secrets |

A `file_id` is the id from `list_files`, or a full Drive URL — the
server extracts the id from `.../file/d/<id>/...` and
`.../drive/folders/<id>` for you.

## Auth — no App Password for Drive

Like Sheets and Calendar, Google offers **no App Password login for
Drive**. Drive requires OAuth 2.0 or a Service Account. Both are
direct-to-Google and self-hosted here — pick one:

Set `DRIVE_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — Service Account if `service-account.json` exists, otherwise OAuth if `token.json` exists
- `oauth` — your own Google account
- `service_account` — a Google Cloud service account

### Option 1 — OAuth 2.0 (best for your personal Drive)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project (or reuse one)
2. APIs & Services → Library → enable **Google Drive API**
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```bash
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve the `drive` scope.
   That creates `token.json` locally.
7. In `.env`:
   ```
   DRIVE_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

The scope is `drive` — it can read and write your Drive files. That
is the broadest scope here, because listing, uploading, sharing and
trashing all need it. It cannot access Gmail, Calendar, or anything else.

### Option 2 — Service Account (best for automation / shared folders)

1. Google Cloud Console → IAM & Admin → Service Accounts → Create service account
2. Keys → Add key → **JSON** → download, save as `service-account.json` in this folder
3. In Drive, share the folder or file with the service account's email
   (the `client_email` inside that JSON) → Viewer or Editor
4. In `.env`:
   ```
   DRIVE_AUTH_METHOD=service_account
   ```
5. No browser login, no token to refresh. Files it creates live in its
   own Drive unless you move/share them. To revoke: stop sharing, or delete the key in Google Cloud.

## Examples

```python
list_files()                                         # 10 most recently modified
list_files(query="Invoice")
list_files(folder_id="1aBcDe...", mime_type="application/pdf")
get_file("1aBcDe...")

read_file("1aBcDe...")                               # Doc -> text, Sheet -> CSV
upload_file("/home/matthew/report.pdf", parent_folder_id="1aBcDe...")
create_folder("Invoices 2026")
copy_file("1aBcDe...", name="Invoice - copy")
rename_file("1aBcDe...", "Invoice 2026-11.pdf")
share_file("1aBcDe...", "alice@example.com", role="writer")
trash_file("1aBcDe...")                              # recoverable from Drive trash
```

Binary files (images, videos, zips, PDFs with no text export) are
not returned as text by `read_file` — you get their metadata and
link instead.

## Run it

```bash
cd google-mcp/drive-mcp
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
    "google-drive": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/drive-mcp/server.py"],
      "env": {
        "DRIVE_AUTH_METHOD": "auto"
      }
    }
  }
}
```

Prefer the `.env` file over putting secrets in the client config. For OAuth, run `auth_oauth.py` once first.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or `service-account.json` — they are in `.gitignore`.
- The OAuth token is scoped to `drive` only.
- A Service Account only sees files/folders shared with it.
- Errors never include your token or key.
- `upload_file` / `rename_file` / `trash_file` / `share_file` **do**
  modify your Drive — the agent should confirm the file, folder and
  recipient before writing, especially on shared folders. Trashing is
  recoverable; sharing sends an email by default.

## Files

- `server.py` — the MCP server (read + write, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `list_files`, `get_file`, `read_file`, `upload_file`, `create_folder`, `copy_file`, `rename_file`, `trash_file`, `share_file`, `drive_status` registered
- `drive_status()` with no credentials fails gracefully, without crashing

Live read/write was **not** tested — that needs your real Google credentials, which I don't have and didn't ask for in chat. Try `drive_status()` first, then `list_files()`.
