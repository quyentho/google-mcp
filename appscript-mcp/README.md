# Google Apps Script MCP Server — self-hosted, read + write

An MCP server that lets an AI agent manage your Google Apps Script
projects — create them, read and rewrite their code, and deploy them —
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — via the Apps Script API (and the Drive API only
to list/find projects by name). Nothing else ever sees your
credentials or your scripts.

## What it does

| Tool | What it does |
|---|---|
| `list_projects(query?, max_results?)` | Find script projects by name — most recently modified first |
| `get_project(script_id)` | Metadata: title, creator, created/updated time, parent folder |
| `get_project_content(script_id)` | All files: code, HTML and manifest, with source |
| `create_project(title, parent_folder_id?)` | Create a script project (starts with a default `Code.gs` + `appsscript.json`) |
| `update_project_content(script_id, files)` | Replace a project's files — send the complete file list |
| `create_deployment(script_id, version_number?, description?)` | Deploy a version of the project |
| `list_deployments(script_id)` | Deployments of a project |
| `appscript_status()` | Checks config without touching any project or revealing secrets |

A `script_id` is the id from `list_projects`, or a full Apps Script URL —
the server extracts the id from `https://script.google.com/d/<id>/edit`
and `https://script.google.com/home/projects/<id>` for you.

This server manages project code and deployments. It does **not**
execute a script function (`scripts.run`) — that needs a separate
execution setup with a deployed executable / web app.

For file operations (move, rename, trash, share) use the Drive MCP
server (`drive-mcp/`) — Apps Script here is for editing project code.

## Auth — no App Password for Apps Script

Like Sheets, Calendar, Drive and Docs, Google offers **no App Password
login for Apps Script**. Apps Script requires OAuth 2.0 or a Service
Account. Both are direct-to-Google and self-hosted here — pick one:

Set `APPSCRIPT_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — Service Account if `service-account.json` exists, otherwise OAuth if `token.json` exists
- `oauth` — your own Google account
- `service_account` — a Google Cloud service account

### Option 1 — OAuth 2.0 (best for your personal projects)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project (or reuse one)
2. APIs & Services → Library → enable **Apps Script API** and **Google Drive API** (Drive is only used to list/find projects by name)
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```bash
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve the `script.projects` +
   `script.deployments` + `drive.readonly` scopes.
   That creates `token.json` locally.
7. In `.env`:
   ```
   APPSCRIPT_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

### Option 2 — Service Account (best for automation / shared projects)

1. Google Cloud Console → IAM & Admin → Service Accounts → Create service account
2. Keys → Add key → **JSON** → download, save as `service-account.json` in this folder
3. Open each script project you want it to touch → Share → paste the service
   account's email (the `client_email` inside that JSON) → Editor
4. In `.env`:
   ```
   APPSCRIPT_AUTH_METHOD=service_account
   ```
5. No browser login, no token to refresh. To revoke: stop sharing the project, or delete the key in Google Cloud.

> A service account can **only** see script projects explicitly shared with it (or
> projects it creates itself). If a tool returns 404 / permission denied,
> share that project with the service account email first.

## Examples

```python
list_projects()                              # 10 most recently modified
list_projects(query="Invoice mailer")
get_project("1aBcDeFgHiJkLmNoPqRsTuVwXyZ")   # or pass the full script.google.com URL
get_project_content("1aBcDe...")

create_project("Invoice mailer")

# update_project_content replaces the WHOLE project — read first, edit, send all back:
update_project_content("1aBcDe...", files=[
  {"name": "Code", "type": "SERVER_JS", "source": "function myFunction() {\n  Logger.log('hi');\n}\n"},
  {"name": "appsscript", "type": "JSON", "source": '{"timeZone": "Asia/Saigon", "exceptionLogging": "STACKDRIVER", "runtimeVersion": "V8"}'},
])

create_deployment("1aBcDe...", description="First deploy")
list_deployments("1aBcDe...")
```

File names have **no extension** in the API: `Code` (not `Code.gs`),
`Index` for an HTML file, `appsscript` for the manifest. Types:
`SERVER_JS`, `HTML`, `JSON`.

A deployment freezes that version — later code edits need a new
deployment to take effect for the deployed entry points.

## Run it

```bash
cd google-mcp/appscript-mcp
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
    "google-apps-script": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/appscript-mcp/server.py"],
      "env": {
        "APPSCRIPT_AUTH_METHOD": "auto"
      }
    }
  }
}
```

Prefer the `.env` file over putting secrets in the client config. For OAuth, run `auth_oauth.py` once first. For a Service Account, put `service-account.json` in this folder and share your script projects with its email.

## Security notes

- Never commit `.env`, `token.json`, `credentials.json`, or `service-account.json` — they are in `.gitignore`.
- The OAuth token is scoped to `script.projects` + `script.deployments` + `drive.readonly`.
- A Service Account only sees script projects shared with it.
- Errors never include your token or key.
- `update_project_content` **replaces the whole project** and `create_deployment` publishes a version — the agent should read the current content first and confirm the script id and files before writing, especially on shared projects. Apps Script code runs with your Google account's access when deployed, so review code before deploying it.

## Files

- `server.py` — the MCP server (read + write, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `list_projects`, `get_project`, `get_project_content`, `create_project`, `update_project_content`, `create_deployment`, `list_deployments`, `appscript_status` registered
- `appscript_status()` with no credentials fails gracefully, without crashing

Live read/write was **not** tested — that needs your real Google credentials, which I don't have and didn't ask for in chat. Try `appscript_status()` first, then `list_projects()`.
