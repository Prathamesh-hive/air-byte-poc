#!/usr/bin/env python3
"""
Airbyte Cloud setup: one Google Drive source → two Pinecone destinations, with connections
syncing every 2 hours.

Source: Google Drive folder (includes docs/sheets in that folder).
  Folder: https://drive.google.com/drive/folders/1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA

Destination: one Pinecone index (pm-test, 1536 dim, text-embedding-3-small, us-east-1).

Prerequisites:
  - tokens.json with Google OAuth (client_id, client_secret, refresh_token under "google")
  - .env: AIRBYTE_*, PINECONE_API_KEY, OPENAI_API_KEY

If connection create returns 500, see docs/AIRBYTE_CONNECTION_CREATE_500.md.

Usage:
  python scripts/airbyte_setup.py
  AIRBYTE_DISCOVERY_WAIT_AFTER_SOURCE=30 AIRBYTE_ALLOW_DOCUMENTS_FALLBACK=1 python scripts/airbyte_setup.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

AIRBYTE_CLIENT_ID = os.getenv("AIRBYTE_CLIENT_ID")
AIRBYTE_CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")
AIRBYTE_WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINECONE_ENV = os.getenv("PINECONE_ENV", os.getenv("PINECONE_ENVIRONMENT", "us-east-1"))

# Google Drive folder to sync (ID from URL)
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA")
FOLDER_URL = f"https://drive.google.com/drive/folders/{GOOGLE_DRIVE_FOLDER_ID}"

# Single Pinecone index (pm-test, us-east-1, text-embedding-3-small)
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "pm-test")
PINECONE_INDEXES = [(PINECONE_INDEX, 1536)]

GOOGLE_DRIVE_SOURCE_DEFINITION_ID = os.getenv(
    "AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE", "9f8dda77-1048-4368-815b-269bf54ee9b8"
)
PINECONE_DEST_DEFINITION_ID = os.getenv(
    "AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE", "3d2b6f84-7f0d-4e3f-a5e5-7c7d4b50eabd"
)

# Sync every 2 hours. Try cron first; API may expect scheduleType "scheduled" + units.
SCHEDULE_EVERY_2_HOURS_CRON = {"scheduleType": "cron", "cronExpression": "0 0 */2 * * ?"}
SCHEDULE_EVERY_2_HOURS_SCHEDULED = {"scheduleType": "scheduled", "units": 2, "timeUnit": "hours"}

BASE_URL = "https://api.airbyte.com/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
TOKENS_PATH = SCRIPT_DIR.parent / "tokens.json"


def load_google_creds() -> dict:
    if not TOKENS_PATH.exists():
        print("ERROR: tokens.json not found. Run OAuth in the app first.")
        sys.exit(1)
    with open(TOKENS_PATH) as f:
        data = json.load(f)
    g = data.get("google")
    if not g or not all(g.get(k) for k in ("client_id", "client_secret", "refresh_token")):
        print("ERROR: tokens.json must contain google.client_id, client_secret, refresh_token")
        sys.exit(1)
    return g


def get_airbyte_token() -> str:
    r = requests.post(
        f"{BASE_URL}/applications/token",
        json={
            "client_id": AIRBYTE_CLIENT_ID,
            "client_secret": AIRBYTE_CLIENT_SECRET,
            "grant-type": "client_credentials",
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"ERROR: Airbyte token failed: {r.status_code} {r.text[:400]}")
        sys.exit(1)
    return r.json()["access_token"]


def ab(method: str, path: str, payload: dict | None = None, token: str | None = None, timeout: int = 60) -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, json=payload, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}
    return r.status_code, body


def main() -> None:
    # Validate env
    if not all([AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET, AIRBYTE_WORKSPACE_ID]):
        print("ERROR: Set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET, AIRBYTE_WORKSPACE_ID in .env")
        sys.exit(1)
    if not PINECONE_API_KEY:
        print("ERROR: Set PINECONE_API_KEY in .env")
        sys.exit(1)
    if not OPENAI_API_KEY:
        print("ERROR: Set OPENAI_API_KEY in .env (required for Pinecone embeddings)")
        sys.exit(1)

    google = load_google_creds()
    print("Config: Drive folder", FOLDER_URL, "→ Pinecone index:", PINECONE_INDEX, "(1536 dim, us-east-1)")
    print()

    token = get_airbyte_token()
    print("Airbyte token obtained.\n")

    # -------------------------------------------------------------------------
    # 1. Create Google Drive source
    # -------------------------------------------------------------------------
    print("STEP 1: Create Google Drive source")
    source_payload = {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": "gdrive-poc",
        "definitionId": GOOGLE_DRIVE_SOURCE_DEFINITION_ID,
        "configuration": {
            "folder_url": FOLDER_URL,
            "delivery_method": {"delivery_type": "use_records_transfer"},
            "credentials": {
                "auth_type": "Client",
                "client_id": google["client_id"],
                "client_secret": google["client_secret"],
                "refresh_token": google["refresh_token"],
            },
            "streams": [
                {
                    "name": "documents",
                    "globs": ["**"],
                    "validation_policy": "Emit Record",
                    "days_to_sync_if_history_is_full": 3,
                    "format": {"filetype": "unstructured"},
                }
            ],
        },
    }
    status, body = ab("POST", "/sources", source_payload, token, timeout=30)
    if status not in (200, 201):
        print(f"  FAILED {status}: {json.dumps(body, indent=2)[:800]}")
        sys.exit(1)
    source_id = body.get("sourceId") or body.get("id")
    print(f"  Source ID: {source_id}\n")

    # -------------------------------------------------------------------------
    # 2. Create two Pinecone destinations
    # -------------------------------------------------------------------------
    dest_ids: list[tuple[str, str]] = []  # (index_name, destination_id)
    for index_name, dim in PINECONE_INDEXES:
        print(f"STEP 2: Create Pinecone destination '{index_name}' (dim={dim})")
        dest_payload = {
            "workspaceId": AIRBYTE_WORKSPACE_ID,
            "name": f"pinecone-{index_name}",
            "definitionId": PINECONE_DEST_DEFINITION_ID,
            "configuration": {
                "destinationType": "pinecone",
                "embedding": {"mode": "openai", "openai_key": OPENAI_API_KEY, "dimensions": dim},
                "indexing": {
                    "index": index_name,
                    "pinecone_key": PINECONE_API_KEY,
                    "pinecone_environment": PINECONE_ENV,
                },
                "processing": {
                    "chunk_size": 997,
                    "chunk_overlap": 20,
                    "text_fields": ["content"],
                    "metadata_fields": [],
                    "text_splitter": {"mode": "separator", "separators": ["\n\n", "\n"], "keep_separator": False},
                },
                "omit_raw_text": False,
            },
        }
        status, body = ab("POST", "/destinations", dest_payload, token, timeout=30)
        if status not in (200, 201):
            print(f"  FAILED {status}: {json.dumps(body, indent=2)[:800]}")
            sys.exit(1)
        dest_id = body.get("destinationId") or body.get("id")
        dest_ids.append((index_name, dest_id))
        print(f"  Destination ID: {dest_id}")
    print()

    # -------------------------------------------------------------------------
    # 3. Discover streams (required for connection create to succeed)
    # -------------------------------------------------------------------------
    # Give the source a moment to be ready; empty discovery often causes 500 on connection create.
    wait_s = int(os.getenv("AIRBYTE_DISCOVERY_WAIT_AFTER_SOURCE", "20"))
    if wait_s > 0:
        print(f"STEP 3: Wait {wait_s}s for source to be ready, then discover streams...")
        time.sleep(wait_s)
    else:
        print("STEP 3: Discover streams (30–90s)...")
    _, first_dest_id = dest_ids[0]
    stream_names: list[str] = []
    for attempt in range(1, 5):
        for path in (
            f"/streams?sourceId={source_id}&destinationId={first_dest_id}",
            f"/streams?sourceId={source_id}",
        ):
            status, body = ab("GET", path, token=token, timeout=150)
            if status != 200:
                continue
            data = body.get("data", body) if isinstance(body, dict) else body
            if isinstance(data, list):
                stream_names = [
                    s.get("streamName") or s.get("name")
                    for s in data
                    if isinstance(s, dict) and (s.get("streamName") or s.get("name"))
                ]
            if stream_names:
                break
        if stream_names:
            break
        if attempt < 4:
            time.sleep(20)
    if not stream_names:
        allow_fallback = os.getenv("AIRBYTE_ALLOW_DOCUMENTS_FALLBACK", "").strip().lower() in ("1", "true", "yes")
        if allow_fallback:
            stream_names = ["documents"]
            print("  WARNING: Discovery returned no streams; using 'documents' (connection create may 500).")
        else:
            print("  ERROR: Stream discovery returned no streams. Connection create would likely fail with 500.")
            print("  Ensure: (1) Drive folder has at least one file, (2) OAuth has drive.readonly, (3) folder is shared.")
            print("  See docs/AIRBYTE_CONNECTION_CREATE_500.md. Set AIRBYTE_ALLOW_DOCUMENTS_FALLBACK=1 to try anyway.")
            sys.exit(1)
    else:
        print(f"  Streams: {stream_names}")
    print()

    # -------------------------------------------------------------------------
    # 4. Create two connections (source → each destination), schedule every 2h
    # -------------------------------------------------------------------------
    stream_configs = [{"name": n, "syncMode": "full_refresh_overwrite"} for n in stream_names]
    connections_created: list[tuple[str, str]] = []  # (index_name, connection_id)

    for index_name, dest_id in dest_ids:
        print(f"STEP 4: Create connection source → {index_name}")
        # Create with manual schedule first (most compatible); set 2h schedule after if needed
        conn_payload = {
            "sourceId": source_id,
            "destinationId": dest_id,
            "name": f"conn-gdrive-to-{index_name}",
            "namespaceDefinition": "custom_format",
            "namespaceFormat": f"ns_{index_name}",
            "schedule": {"scheduleType": "manual"},
            "configurations": {"streams": stream_configs},
        }
        status, body = ab("POST", "/connections", conn_payload, token, timeout=120)
        if status not in (200, 201):
            print(f"  FAILED {status}: {json.dumps(body, indent=2)[:600]}")
            sys.exit(1)
        conn_id = body.get("connectionId") or body.get("id")
        connections_created.append((index_name, conn_id))
        # Try to set schedule to every 2 hours via PATCH
        for sched in (SCHEDULE_EVERY_2_HOURS_SCHEDULED, SCHEDULE_EVERY_2_HOURS_CRON):
            patch_status, _ = ab("PATCH", f"/connections/{conn_id}", {"schedule": sched}, token, timeout=30)
            if patch_status in (200, 204):
                print(f"  Connection ID: {conn_id} (schedule: every 2h)")
                break
        else:
            print(f"  Connection ID: {conn_id} (manual schedule — set 'Every 2 hours' in UI)")
    print()

    # -------------------------------------------------------------------------
    # 5. Trigger first sync for each connection
    # -------------------------------------------------------------------------
    print("STEP 5: Trigger first sync for each connection")
    for index_name, conn_id in connections_created:
        status, body = ab("POST", "/jobs", {"connectionId": conn_id, "jobType": "sync"}, token, timeout=30)
        if status in (200, 201):
            job_id = body.get("jobId", "?")
            print(f"  {index_name}: job {job_id}")
        else:
            print(f"  {index_name}: trigger failed {status}")
    print()

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("DONE. Save these IDs:")
    print(f"  SOURCE_ID       : {source_id}")
    for index_name, dest_id in dest_ids:
        print(f"  DEST_ID ({index_name:18}): {dest_id}")
    for index_name, conn_id in connections_created:
        print(f"  CONNECTION_ID ({index_name:14}): {conn_id}")
    print()
    print("Schedule: every 2 hours (cron or scheduled). If UI shows manual, set in connection settings.")


if __name__ == "__main__":
    main()
