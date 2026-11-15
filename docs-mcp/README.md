# Google Docs MCP Server — self-hosted, read + write

An MCP server that lets an AI agent read and write your Google Docs,
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Docs API (and the Drive API only to
list/find documents by name). Nothing else ever sees your credentials
or your documents.

## What it does

| Tool | What it does |
|---|---|
| `list_documents(query?, max_results?)` | Find documents by name — most recently modified first |
| `get_document(document_id)` | Title, revision and full plain-text content (headings marked with `#`) |
| `create_document(title, text?)` | Create a document, optionally with initial text |
| `append_text(document_id, text)` | Add text to the end of a document |
| `replace_text(document_id, find, replace, match_case?)` | Find-and-replace, every occurrence |
| `docs_status()` | Checks config without touching any document or revealing secrets |

A `document_id` is the id from `list_documents`, or a full Docs URL —
the server extracts the id from `https://docs.google.com/document/d/<id>/...` for you.

For file operations (move, rename, trash, share, upload) use the
Drive MCP server (`drive-mcp/`) — Docs here is for editing content.

## Auth — no App Password for Docs

Like Sheets, Calendar and Drive, Google offers **no App Password
login for Docs**. Docs requires OAuth 2.0 or a Service Account. Both
are direct-to-Google and self-hosted here — pick one:

Set `DOCS_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — Service Account if `service-account.json` exists, otherwise OAuth if `token.json` exists
- `oauth` — your own Google account
- `service_account` — a Google Cloud service account

### Option 1 — OAuth 2.0 (best for your personal documents)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project (or reuse one)
2. APIs & Services → Library → enable **Google Docs API** and **Google Drive API** (Drive is only used to list/find documents by name)
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```bash
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve the `documents` + `drive.readonly` scopes.
   That creates `token.json` locally.
7. In `.env`:
   ```
   DOCS_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

Scopes are `documents` (read/write your documents) + `drive.readonly`
(list/find documents). The token cannot access Gmail, Calendar, or anything else.

### Option 2 — Service Account (best for automation / shared documents)

1. Google Cloud Console → IAM & Admin → Service Accounts → Create service account
2. Keys → Add key → **JSON** → download, save as `service-account.json` in this folder
3. Open each document you want it to touch → Share → paste the service
   account's email (the `client_email` inside that JSON) → Viewer or Editor
4. In `.env`:
   ```
   DOCS_AUTH_METHOD=service_account
   ```
5. No browser login, no token to refresh. To revoke: stop sharing the document, or delete the key in Google Cloud.

> A service account can **only** see documents explicitly shared with it (or
> documents it creates itself). If a tool returns 404 / permission denied,
> share that document with the service account email first.

## Examples

```python
list_documents()                              # 10 most recently modified
list_documents(query="Meeting notes")
get_document("1aBcDeFgHiJkLmNoPqRsTuVwXyZ")   # or pass the full Docs URL

create_document("Meeting notes 2026-11-14", text="Attendees: Alice, Bob\n\n")
append_text("1aBcDe...", "Decision: ship on Friday.")
replace_text("1aBcDe...", find="Friday", replace="Monday")
replace_text("1aBcDe...", find="draft", replace="final", match_case=True)
```

`get_document` returns plain text (20,000 chars max). Formatting,
images and tables are not preserved as text — tables are flattened
into lines, headings are marked with `#`.

## Run it

```bash
cd google-mcp/docs-mcp
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
    "google-docs": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/docs-mcp/server.py"],
      "env": {
        "DOCS_AUTH_METHOD": "auto"
      }
    }
  }
}
```

Prefer the `.env` file over putting secrets in the client config. For OAuth, run `auth_oauth.py` once first.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or `service-account.json` — they are in `.gitignore`.
- The OAuth token is scoped to `documents` + `drive.readonly`.
- A Service Account only sees documents shared with it.
- Errors never include your token or key.
- `create_document` / `append_text` / `replace_text` **do** modify documents — the agent should confirm the document id and text before writing, especially on shared documents.

## Files

- `server.py` — the MCP server (read + write, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `list_documents`, `get_document`, `create_document`, `append_text`, `replace_text`, `docs_status` registered
- `docs_status()` with no credentials fails gracefully, without crashing

Live read/write was **not** tested — that needs your real Google credentials, which I don't have and didn't ask for in chat. Try `docs_status()` first, then `list_documents()`.
