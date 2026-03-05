#!/usr/bin/env python3
"""
Test: Google Drive source / OAuth (cause #3 in AIRBYTE_CONNECTION_CREATE_500.md).

Verifies tokens.json has valid OAuth and can list files in the Drive folder.
Invalid/expired OAuth or no access → source check fails → 500.

Usage:
  python scripts/test_3_google_drive_source.py
  GOOGLE_DRIVE_FOLDER_ID=xxx python scripts/test_3_google_drive_source.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

TOKENS_PATH = ROOT / "tokens.json"
FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA")


def main():
    print("Test 3: Google Drive source (OAuth, folder access)")
    print("  Doc: AIRBYTE_CONNECTION_CREATE_500.md §3 — invalid OAuth / scope → 500")
    print()

    if not TOKENS_PATH.exists():
        print("SKIP: tokens.json not found. Run OAuth in the app first.")
        sys.exit(2)

    with open(TOKENS_PATH) as f:
        data = json.load(f)
    g = data.get("google")
    if not g:
        print("FAIL: tokens.json has no 'google' key")
        sys.exit(1)
    for k in ("client_id", "client_secret", "refresh_token"):
        if not g.get(k):
            print(f"FAIL: google.{k} missing in tokens.json")
            sys.exit(1)
    if g.get("auth_type") is None and "Client" not in str(g.get("auth_type", "")):
        print("  Note: auth_type not set; Airbyte may require auth_type: 'Client' in source config.")

    # Try to refresh token and list Drive files (same scope Airbyte uses)
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as e:
        print(f"  WARN: Google libs not available: {e}. Skipping live Drive list.")
        print("  OAuth keys present in tokens.json. Cause #3 possible if token expired or scope wrong.")
        print()
        print("VERDICT: UNKNOWN — Validate in Airbyte UI (source check) or ensure drive.readonly scope.")
        sys.exit(0)

    creds = Credentials(
        token=None,
        refresh_token=g["refresh_token"],
        token_uri=g.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=g["client_id"],
        client_secret=g["client_secret"],
        scopes=g.get("scopes", ["https://www.googleapis.com/auth/drive.readonly"]),
    )
    try:
        creds.refresh(Request())
    except Exception as e:
        print(f"FAIL: Token refresh failed — {e}")
        print("  Fix: Re-run OAuth; ensure client_id/secret and refresh_token are correct.")
        sys.exit(1)
    print("  Token refresh: OK")

    try:
        service = build("drive", "v3", credentials=creds)
        # List files in folder (max 10); folder must be shared with the OAuth account
        result = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            pageSize=10,
            fields="nextPageToken, files(id, name, mimeType)",
        ).execute()
        files = result.get("files", [])
        print(f"  Folder {FOLDER_ID}: {len(files)} file(s) listed")
        if files:
            for f in files[:5]:
                print(f"    - {f.get('name', '?')} ({f.get('mimeType', '?')})")
        else:
            print("  WARN: Folder is empty or not shared with this account. Can cause 'no streams' → 500.")
    except Exception as e:
        print(f"FAIL: Drive list failed — {e}")
        print("  Fix: Share the folder with the Google account that authorized the app; scope drive.readonly.")
        sys.exit(1)

    print()
    print("VERDICT: PASS — OAuth valid and folder accessible. Cause #3 is unlikely.")
    sys.exit(0)


if __name__ == "__main__":
    main()
