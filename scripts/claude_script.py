#!/usr/bin/env python3
"""
Simple Airbyte Cloud setup: Source (Google Sheets) + Destination (Pinecone) + Connection.
Minimal config: spreadsheet link + OAuth only.

Prerequisites:
  - tokens.json with Google OAuth (run OAuth in the app first)
  - .env with Airbyte and Pinecone credentials

Usage:
  python scripts/claude_script.py
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

AIRBYTE_CLIENT_ID     = os.getenv("AIRBYTE_CLIENT_ID")
AIRBYTE_CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")
AIRBYTE_WORKSPACE_ID  = "7c49afde-bda6-4b42-a343-18323d48e119"

PINECONE_API_KEY      = os.getenv("VERIFY_PINECONE_API_KEY") or os.getenv("PINECONE_API_KEY")
PINECONE_INDEX        = os.getenv("PINECONE_INDEX", "pm-test")
PINECONE_ENVIRONMENT  = os.getenv("PINECONE_ENV", os.getenv("PINECONE_ENVIRONMENT", "us-east-1"))
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY")

# Single client
CLIENT_NAMESPACE = "client-123123"

# Google Sheets: minimal — just the spreadsheet URL (like the UI)
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1cym08LrNKq8kqoxk3HTPq_2NJjGNGAv7f4ZvXiaCUh8/edit?gid=0#gid=0"

# Connector definition ID for source (Airbyte Cloud)
GOOGLE_SHEETS_DEFINITION_ID = "71607ba1-c0ac-4799-8049-7f4b90dd50f7"

# Use existing destination (already created in workspace)
EXISTING_DESTINATION_ID = "3d2b6f84-7f0d-4e3f-a5e5-7c7d4b50eabd"

# ─────────────────────────────────────────────
# LOAD GOOGLE TOKENS
# ─────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKENS_PATH = os.path.join(os.path.dirname(_SCRIPT_DIR), "tokens.json")

if not os.path.exists(TOKENS_PATH):
    print("ERROR: tokens.json not found. Run OAuth in the app first.")
    sys.exit(1)

with open(TOKENS_PATH) as f:
    tok = json.load(f)

g = tok["google"]
GOOGLE_CLIENT_ID     = g["client_id"]
GOOGLE_CLIENT_SECRET = g["client_secret"]
REFRESH_TOKEN        = g["refresh_token"]

if not all([AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET, AIRBYTE_WORKSPACE_ID]):
    print("ERROR: Set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET, AIRBYTE_WORKSPACE_ID in .env")
    sys.exit(1)
if not PINECONE_API_KEY:
    print("ERROR: Set PINECONE_API_KEY or VERIFY_PINECONE_API_KEY in .env")
    sys.exit(1)
if not OPENAI_API_KEY:
    print("ERROR: Set OPENAI_API_KEY in .env (required for Pinecone embeddings)")
    sys.exit(1)

print("=" * 55)
print("CONFIG")
print("=" * 55)
print(f"  Workspace ID   : {AIRBYTE_WORKSPACE_ID}")
print(f"  Spreadsheet    : {SPREADSHEET_URL[:60]}...")
print(f"  Pinecone index : {PINECONE_INDEX}")
print(f"  Namespace      : {CLIENT_NAMESPACE}")
print()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

BASE_URL = "https://api.airbyte.com/v1"


def get_airbyte_token():
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
        print(f"ERROR: Could not get Airbyte token: {r.status_code} {r.text}")
        sys.exit(1)
    return r.json()["access_token"]


def ab(method, path, payload=None, token=None, timeout=60):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{BASE_URL}{path}"
    r = requests.request(method, url, headers=headers, json=payload, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    return r.status_code, body


# ─────────────────────────────────────────────
# STEP 0: Token
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 0: Get Airbyte bearer token")
print("=" * 55)
TOKEN = get_airbyte_token()
print(f"  ✓ Token: {TOKEN[:30]}...")
print()

# ─────────────────────────────────────────────
# STEP 1: Create Google Sheets source (minimal)
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 1: Create Google Sheets source")
print("=" * 55)

source_payload = {
    "workspaceId": AIRBYTE_WORKSPACE_ID,
    "name": f"sheets-{CLIENT_NAMESPACE}",
    "definitionId": GOOGLE_SHEETS_DEFINITION_ID,
    "configuration": {
        "spreadsheet_id": SPREADSHEET_URL,
        "credentials": {
            "auth_type": "Client",
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
        },
    },
}

status, body = ab("POST", "/sources", source_payload, TOKEN)
if status not in (200, 201):
    print(f"  ✗ FAILED {status}: {json.dumps(body, indent=2)}")
    sys.exit(1)

SOURCE_ID = body["sourceId"]
print(f"  ✓ Source created: {SOURCE_ID}")
print()

# ─────────────────────────────────────────────
# STEP 2: Use existing destination
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 2: Use existing destination")
print("=" * 55)
DEST_ID = EXISTING_DESTINATION_ID
print(f"  ✓ Destination ID: {DEST_ID}")
print()

# ─────────────────────────────────────────────
# STEP 3: Discover streams
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 3: Discover streams")
print("=" * 55)

STREAM_NAMES = []
for attempt in range(1, 4):
    print(f"  Attempt {attempt}/3...")
    status, body = ab(
        "GET",
        f"/streams?sourceId={SOURCE_ID}&destinationId={DEST_ID}",
        token=TOKEN,
        timeout=120,
    )
    if status == 200:
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list):
            STREAM_NAMES = [
                s.get("streamName") or s.get("name")
                for s in data
                if isinstance(s, dict) and (s.get("streamName") or s.get("name"))
            ]
        if STREAM_NAMES:
            print(f"  ✓ Discovered: {STREAM_NAMES}")
            break
        print(f"  ⚠ 200 but no stream names: {str(body)[:150]}")
    else:
        print(f"  ✗ {status}: {str(body)[:200]}")

    if attempt < 3:
        time.sleep(15)

if not STREAM_NAMES:
    STREAM_NAMES = ["Sheet1"]
    print(f"  ⚠ Fallback to: {STREAM_NAMES}")
print()

# ─────────────────────────────────────────────
# STEP 4: Create connection
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 4: Create connection")
print("=" * 55)

stream_configs = [{"name": n, "syncMode": "full_refresh_overwrite"} for n in STREAM_NAMES]
connection_payload = {
    "sourceId": SOURCE_ID,
    "destinationId": DEST_ID,
    "name": f"conn-{CLIENT_NAMESPACE}",
    "namespaceDefinition": "custom_format",
    "namespaceFormat": CLIENT_NAMESPACE,
    "schedule": {"scheduleType": "manual"},
    "configurations": {"streams": stream_configs},
}

status, body = ab("POST", "/connections", connection_payload, TOKEN, timeout=120)
if status not in (200, 201):
    print(f"  ✗ FAILED {status}: {json.dumps(body, indent=2)}")
    sys.exit(1)

CONNECTION_ID = body["connectionId"]
print(f"  ✓ Connection created: {CONNECTION_ID}")
print()

# ─────────────────────────────────────────────
# STEP 5: Trigger sync
# ─────────────────────────────────────────────

print("=" * 55)
print("STEP 5: Trigger first sync")
print("=" * 55)

status, body = ab("POST", "/jobs", {"connectionId": CONNECTION_ID, "jobType": "sync"}, TOKEN, timeout=30)
if status not in (200, 201):
    print(f"  ✗ Sync trigger failed {status}: {body}")
else:
    JOB_ID = body.get("jobId", "unknown")
    print(f"  ✓ Sync job: {JOB_ID}")
    print(f"  https://cloud.airbyte.com/workspaces/{AIRBYTE_WORKSPACE_ID}/connections/{CONNECTION_ID}/job-history")

print()
print("=" * 55)
print("DONE")
print("=" * 55)
print(f"  SOURCE_ID     : {SOURCE_ID}")
print(f"  DEST_ID       : {DEST_ID}")
print(f"  CONNECTION_ID : {CONNECTION_ID}")
print(f"  NAMESPACE     : {CLIENT_NAMESPACE}")
