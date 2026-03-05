#!/usr/bin/env python3
"""
Test: Connection create (cause #4 + overall).

Attempts POST /connections with current source/dest and discovered (or default) streams.
Captures exact status and body so you can see if it's 500 and the real error.

Usage:
  python scripts/test_4_connection_create.py
  SOURCE_ID=xxx DEST_ID=yyy python scripts/test_4_connection_create.py
  DRY_RUN=1  # only print payload, do not POST
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests

BASE = "https://api.airbyte.com/v1"
SOURCE_ID = os.getenv("SOURCE_ID", os.getenv("AIRBYTE_SOURCE_ID"))
DEST_ID = os.getenv("DEST_ID", os.getenv("AIRBYTE_DEST_ID"))
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def get_token():
    r = requests.post(
        f"{BASE}/applications/token",
        json={
            "client_id": os.getenv("AIRBYTE_CLIENT_ID"),
            "client_secret": os.getenv("AIRBYTE_CLIENT_SECRET"),
            "grant-type": "client_credentials",
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"FAIL: Airbyte token: {r.status_code}")
        sys.exit(1)
    return r.json()["access_token"]


def get_streams(token):
    if not SOURCE_ID:
        return []
    paths = [f"/streams?sourceId={SOURCE_ID}&destinationId={DEST_ID}"] if DEST_ID else []
    paths.append(f"/streams?sourceId={SOURCE_ID}")
    for path in paths:
        try:
            r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=120)
            if r.status_code != 200:
                continue
            data = r.json() if r.text else {}
            raw = data.get("data", data.get("streams", []))
            if isinstance(raw, list):
                names = [s.get("streamName") or s.get("name") for s in raw if isinstance(s, dict) and (s.get("streamName") or s.get("name"))]
                if names:
                    return names
        except Exception:
            pass
    return ["documents"]


def main():
    print("Test 4: Connection create (POST /connections)")
    print("  Doc: AIRBYTE_CONNECTION_CREATE_500.md §4 — payload/timeout/schedule")
    print()

    if not all([os.getenv("AIRBYTE_CLIENT_ID"), os.getenv("AIRBYTE_CLIENT_SECRET")]):
        print("SKIP: Set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET")
        sys.exit(2)
    if not SOURCE_ID or not DEST_ID:
        print("SKIP: Set SOURCE_ID and DEST_ID (e.g. from airbyte_setup.py)")
        sys.exit(2)

    token = get_token()
    stream_configs = [{"name": n, "syncMode": "full_refresh_overwrite"} for n in get_streams(token)]
    print(f"  Streams to use: {[c['name'] for c in stream_configs]}")

    payload = {
        "sourceId": SOURCE_ID,
        "destinationId": DEST_ID,
        "name": "test-conn-diagnose",
        "namespaceDefinition": "custom_format",
        "namespaceFormat": "test_ns",
        "schedule": {"scheduleType": "manual"},
        "configurations": {"streams": stream_configs},
    }
    print(f"  Payload (excerpt): name={payload['name']}, streams={len(stream_configs)}")
    if DRY_RUN:
        print()
        print("DRY_RUN=1: not sending POST. Remove DRY_RUN to run.")
        print(json.dumps(payload, indent=2))
        sys.exit(0)

    print("  POST /connections (timeout=120s)...")
    try:
        r = requests.post(
            f"{BASE}/connections",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
    except requests.exceptions.Timeout:
        print("  TIMEOUT — Cause #4 (timeout). Try longer or retry.")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    body = r.json() if r.text else {}
    print(f"  HTTP {r.status_code}")
    print()
    if r.status_code in (200, 201):
        cid = body.get("connectionId") or body.get("id")
        print(f"VERDICT: PASS — Connection created: {cid}")
        print("  You may delete this test connection in Airbyte UI.")
        sys.exit(0)
    else:
        print("VERDICT: FAIL — Connection create failed.")
        print(json.dumps(body, indent=2)[:1200])
        if r.status_code == 500:
            print()
            print("  500 = backend connector error. Run test_1 (streams), test_2 (Pinecone), test_3 (Drive) to find cause.")
        sys.exit(1)


if __name__ == "__main__":
    main()
