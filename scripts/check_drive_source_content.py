#!/usr/bin/env python3
"""
Verify Google token and fetch text from the Doc and Sheet.
Doc: 1B9z424_gLeSUDINi05O9H3O6uuzBT7m_NtNTbDOzxls
Sheet: 1cym08LrNKq8kqoxk3HTPq_2NJjGNGAv7f4ZvXiaCUh8 (gid=0)
Folder: 1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS_PATH = ROOT / "tokens.json"

DOC_ID = "1B9z424_gLeSUDINi05O9H3O6uuzBT7m_NtNTbDOzxls"
SHEET_ID = "1cym08LrNKq8kqoxk3HTPq_2NJjGNGAv7f4ZvXiaCUh8"
FOLDER_ID = "1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA"


def main():
    if not TOKENS_PATH.exists():
        print("ERROR: tokens.json not found")
        sys.exit(1)
    with open(TOKENS_PATH) as f:
        data = json.load(f)
    g = data.get("google")
    if not g or not all(g.get(k) for k in ("client_id", "client_secret", "refresh_token")):
        print("ERROR: tokens.json missing google.client_id, client_secret, or refresh_token")
        sys.exit(1)

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    import io

    creds = Credentials(
        token=None,
        refresh_token=g["refresh_token"],
        token_uri=g.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=g["client_id"],
        client_secret=g["client_secret"],
        scopes=g.get("scopes", ["https://www.googleapis.com/auth/drive.readonly"]),
    )
    creds.refresh(Request())
    print("Token: OK (refreshed)\n")

    drive = build("drive", "v3", credentials=creds)

    # 1) List folder
    print(f"--- Folder {FOLDER_ID} ---")
    try:
        r = drive.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            pageSize=20,
            fields="files(id, name, mimeType)",
        ).execute()
        for f in r.get("files", []):
            print(f"  {f['name']}  id={f['id']}  mimeType={f.get('mimeType')}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    # 2) Doc text (export as plain text)
    print("--- Doc text ---")
    try:
        doc = drive.files().export(fileId=DOC_ID, mimeType="text/plain").execute()
        text = doc.decode("utf-8", errors="replace")
        print(text if text.strip() else "(empty)")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    # 3) Sheet (export as CSV for first sheet, or use Sheets API for grid)
    print("--- Sheet content (gid=0) ---")
    try:
        # Export first sheet as CSV
        sheet = drive.files().export(fileId=SHEET_ID, mimeType="text/csv").execute()
        text = sheet.decode("utf-8", errors="replace")
        print(text if text.strip() else "(empty)")
    except Exception as e:
        print(f"  Export failed: {e}")
        try:
            from googleapiclient.discovery import build as build2
            sheets = build2("sheets", "v4", credentials=creds)
            # Need spreadsheets.readonly for this; try anyway
            val = sheets.spreadsheets().values().get(
                spreadsheetId=SHEET_ID, range="Sheet1"
            ).execute()
            rows = val.get("values", [])
            for row in rows:
                print("\t".join(str(c) for c in row))
        except Exception as e2:
            print(f"  Sheets API fallback: {e2}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
