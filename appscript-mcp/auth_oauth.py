#!/usr/bin/env python3
"""
One-time OAuth setup for the Google Apps Script MCP server.

Run once on your own machine:
    python auth_oauth.py

It opens a browser, you sign in with the Google account whose script
projects you want to use, approve the script.projects +
script.deployments + drive.readonly scopes, and it saves token.json
next to server.py. That file holds a refresh token — keep it private,
like a password. Never commit it.

Prerequisites:
  1. Google Cloud Console > create a project (or reuse one)
  2. APIs & Services > Library > enable Apps Script API
     AND Google Drive API (Drive is used only to list/find projects by name)
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
CREDENTIALS_FILE = Path(os.environ.get("APPSCRIPT_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("APPSCRIPT_TOKEN_FILE", BASE_DIR / "token.json"))
SCOPES = [
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/drive.readonly",
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
    print("Scopes granted: script.projects + script.deployments + drive.readonly (list/find projects).")
    print("You can now use APPSCRIPT_AUTH_METHOD=oauth (or auto) in server.py.")
    print(f"To revoke later: Google Account > Security > Third-party access, or delete {TOKEN_FILE}.")


if __name__ == "__main__":
    main()
