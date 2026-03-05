#!/usr/bin/env python3
"""
Create a new connection for given source + destination and trigger a sync.
Uses existing source/destination IDs; payload shape matches working UI-created connection.
"""
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.airbyte.com/v1"
SOURCE_ID = "53a71919-e602-48f2-ba0b-66a224f2c7a6"
DESTINATION_ID = "c553a544-34a6-4034-8760-d54cfb374e20"
AIRBYTE_CLIENT_ID = os.getenv("AIRBYTE_CLIENT_ID")
AIRBYTE_CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")

if not AIRBYTE_CLIENT_ID or not AIRBYTE_CLIENT_SECRET:
    print("ERROR: Set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET in .env")
    sys.exit(1)


def get_token():
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
        print(f"ERROR: Token failed {r.status_code} {r.text}")
        sys.exit(1)
    return r.json()["access_token"]


def api(method, path, payload=None, token=None, timeout=60):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, f"{BASE_URL}{path}", headers=headers, json=payload, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def main():
    print("Get token...")
    token = get_token()
    print("  ✓ Token OK\n")

    # Use custom_format + unique namespace so it doesn't conflict with existing connection
    namespace = f"verify-api-{int(time.time())}"
    connection_payload = {
        "sourceId": SOURCE_ID,
        "destinationId": DESTINATION_ID,
        "name": "verify-conn-api",
        "namespaceDefinition": "custom_format",
        "namespaceFormat": namespace,
        "schedule": {"scheduleType": "manual"},
        "configurations": {
            "streams": [
                {"name": "Sheet1", "syncMode": "full_refresh_overwrite"}
            ],
        },
    }

    print("Create connection...")
    status, body = api("POST", "/connections", connection_payload, token, timeout=120)
    if status not in (200, 201):
        print(f"  ✗ FAILED {status}: {json.dumps(body, indent=2)}")
        sys.exit(1)

    connection_id = body.get("connectionId") or body.get("connection_id")
    print(f"  ✓ Connection created: {connection_id}\n")

    print("Trigger sync...")
    status, body = api("POST", "/jobs", {"connectionId": connection_id, "jobType": "sync"}, token, timeout=30)
    if status not in (200, 201):
        print(f"  ✗ Sync trigger failed {status}: {body}")
        sys.exit(1)

    job_id = body.get("jobId") or body.get("job_id", "unknown")
    print(f"  ✓ Sync job triggered: {job_id}")
    print(f"\n  Connection: {connection_id}")
    print(f"  https://cloud.airbyte.com/workspaces/7c49afde-bda6-4b42-a343-18323d48e119/connections/{connection_id}/job-history")


if __name__ == "__main__":
    main()
