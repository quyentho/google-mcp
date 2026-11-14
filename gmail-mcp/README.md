# Gmail MCP Server — self-hosted, send + read

An MCP server that lets an AI agent send **and read** email from your Gmail,
with **no third-party service in the middle**.

Your agent talks to this local server over stdio. This server talks
directly to Google — either via SMTP/IMAP or via the Gmail API. Nothing
else ever sees your credentials or your mail.

## What it does

| Tool | What it does |
|---|---|
| `send_email(to, subject, body, cc?, bcc?, is_html?, reply_to?)` | Sends one email from your Gmail |
| `list_emails(query?, max_results?, unread_only?, folder?, include_body?)` | Lists / searches mail — newest first, headers + snippet. Read-only. |
| `get_email(message_id, folder?)` | Reads one full message — headers + plain-text body (truncated at 20,000 chars) |
| `gmail_status()` | Checks config without sending/reading anything or revealing secrets |

Read is **read-only**: it never deletes, archives, labels, or marks mail as read.

## Choose an auth method

Set `GMAIL_AUTH_METHOD` in `.env` (or in your MCP client's `env`):

- `auto` (default) — App Password if `GMAIL_APP_PASSWORD` is set, otherwise OAuth if `token.json` exists
- `app_password` — SMTP for sending, IMAP for reading
- `oauth` — Gmail API for both

### Option 1 — App Password (simplest, no Google Cloud project)

1. Google Account → Security → turn on **2-Step Verification**
2. Security → **App passwords** → App: Mail → generate a 16-character password
3. Gmail → Settings → Forwarding and POP/IMAP → **Enable IMAP**
4. Copy `env.example` to `.env` and fill in:
   ```
   GMAIL_USER=you@gmail.com
   GMAIL_AUTH_METHOD=app_password
   GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
   ```
5. To revoke later: Google Account → Security → App passwords → revoke.

### Option 2 — OAuth 2.0 (no password stored)

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project
2. APIs & Services → Library → enable **Gmail API**
3. APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
4. Download the JSON, save it as `credentials.json` in this folder
5. OAuth consent screen → add yourself as a Test user (apps start in Testing mode)
6. Install deps and run the one-time login:
   ```
   pip install -r requirements.txt
   python auth_oauth.py
   ```
   A browser opens, you sign in and approve `gmail.send` + `gmail.readonly`.
   That creates `token.json` locally.
   > If you already have a `token.json` from the send-only version, delete it first — it only has `gmail.send` and cannot read mail.
7. In `.env`:
   ```
   GMAIL_USER=you@gmail.com
   GMAIL_AUTH_METHOD=oauth
   ```
8. To revoke later: Google Account → Security → Third-party access → remove it, or just delete `token.json`.

## Reading mail

```
list_emails()                                    # 10 most recent in INBOX
list_emails(query="from:alice@example.com")      # search by sender
list_emails(query="subject:invoice", unread_only=True)
list_emails(query="is:unread", max_results=20)
get_email(message_id="imap:INBOX:123")           # IMAP id from list_emails
get_email(message_id="18f2a1b2c3d4e5f6")         # Gmail API id from list_emails
```

Folders: `INBOX` by default. For IMAP sent mail use `"[Gmail]/Sent Mail"`.

## Run it

```bash
cd google-mcp/gmail-mcp
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
    "gmail-smtp": {
      "command": "python",
      "args": ["/absolute/path/to/google-mcp/gmail-mcp/server.py"],
      "env": {
        "GMAIL_USER": "you@gmail.com",
        "GMAIL_AUTH_METHOD": "auto",
        "GMAIL_APP_PASSWORD": "your-16-char-app-password"
      }
    }
  }
}
```

Prefer the `.env` file over putting the password in the client config — then omit `env` entirely. For OAuth, omit `GMAIL_APP_PASSWORD` and run `auth_oauth.py` once first.

## Security notes

- Never commit `.env`, `token.json`, or `credentials.json` — they are in `.gitignore`.
- The App Password only works for SMTP/IMAP, cannot sign in to your Google Account on the web, and can be revoked separately.
- The OAuth token is scoped to `gmail.send` + `gmail.readonly` — it cannot modify or delete mail.
- Errors never include your password or token.
- BCC addresses are never echoed back in the tool result.
- Bodies are truncated at 20,000 characters per message.

## Files

- `server.py` — the MCP server (send + read, both auth methods)
- `auth_oauth.py` — one-time OAuth login, creates `token.json`
- `requirements.txt` — `mcp`, `python-dotenv`, plus Google libs for OAuth
- `env.example` — copy to `.env`
- `mcp.json` — example client config
- `.gitignore` — keeps secrets out of git

## Tested

- `python -m py_compile server.py auth_oauth.py` — OK
- Import + tool registration with `mcp` 1.x — tools `send_email`, `list_emails`, `get_email`, `gmail_status` registered
- Invalid recipient rejected cleanly, no secret leaked
- Read paths fail gracefully with no credentials (IMAP login error / OAuth “run `python auth_oauth.py`”), without crashing

Live send/read was **not** tested — that needs your real Gmail credentials, which I don't have and didn't ask for in chat. Try `gmail_status()` first, then `list_emails()` to yourself.
