#!/usr/bin/env python3
"""
One-time OAuth setup for the Gmail MCP server.

Run once on your own machine:
    python auth_oauth.py

It opens a browser, you sign in with the Gmail account you want to
send from / read, approve the gmail.send + gmail.readonly scopes,
and it saves token.json next to server.py. That file holds a refresh
token — keep it private, like a password. Never commit it.

If you already have a token.json from the send-only version, delete
it first — the old token only has gmail.send and cannot read mail.

Prerequisites:
  1. Google Cloud Console > create a project (or reuse one)
  2. APIs & Services > Library > enable Gmail API
  3. APIs & Services > Credentials > Create Credentials > OAuth client ID
     > Application type: Desktop app
  4. Download the JSON and save it as credentials.json in this folder
  5. OAuth consent screen: add yourself as a Test user if the app is
     in Testing mode (it is by default)
  6. pip install -r requirements.txt
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN_FILE", BASE_DIR / "token.json"))
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"Missing {CREDENTIALS_FILE}.\n"
            "Create a Desktop OAuth client in Google Cloud, download its JSON, "
            "and save it as credentials.json in this folder."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    # localhost redirect, no server needed beyond this one-time flow
    creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json())
    print(f"Saved OAuth token to {TOKEN_FILE}")
    print("Scopes granted: gmail.send + gmail.readonly — can send and read mail, cannot modify/delete it.")
    print("You can now use GMAIL_AUTH_METHOD=oauth (or auto) in server.py.")
    print(f"To revoke later: Google Account > Security > Third-party access, or delete {TOKEN_FILE}.")


if __name__ == "__main__":
    main()
