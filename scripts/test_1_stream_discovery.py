#!/usr/bin/env python3
"""
Test: Stream discovery (cause #1 in AIRBYTE_CONNECTION_CREATE_500.md).

Calls GET /streams with sourceId and destinationId. If this returns no streams,
connection create often fails with 500.

Usage:
  python scripts/test_1_stream_discovery.py
  SOURCE_ID=xxx DEST_ID=yyy python scripts/test_1_stream_discovery.py
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
        print(f"FAIL: Could not get Airbyte token: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    return r.json()["access_token"]


def main():
    print("Test 1: Stream discovery (GET /streams)")
    print("  Doc: AIRBYTE_CONNECTION_CREATE_500.md §1 — empty discovery → 500 on connection create")
    print()

    if not all([os.getenv("AIRBYTE_CLIENT_ID"), os.getenv("AIRBYTE_CLIENT_SECRET")]):
        print("SKIP: Set AIRBYTE_CLIENT_ID, AIRBYTE_CLIENT_SECRET in .env")
        sys.exit(2)
    if not SOURCE_ID:
        print("SKIP: Set SOURCE_ID or AIRBYTE_SOURCE_ID (e.g. from airbyte_setup.py output)")
        sys.exit(2)

    token = get_token()
    timeout = 150
    paths = [
        f"/streams?sourceId={SOURCE_ID}&destinationId={DEST_ID}" if DEST_ID else None,
        f"/streams?sourceId={SOURCE_ID}",
    ]
    paths = [p for p in paths if p]

    streams_found = []
    for path in paths:
        print(f"  GET {path} (timeout={timeout}s)...")
        try:
            r = requests.get(
                f"{BASE}{path}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            print(f"  TIMEOUT after {timeout}s — discovery can be slow; may still cause 500 if backend times out.")
            print()
            print("VERDICT: Likely cause #1 (discovery timeout). Increase timeout or retry later.")
            sys.exit(1)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:400]}")
            continue

        data = r.json() if r.text else {}
        raw = data.get("data", data.get("streams", data))
        if isinstance(raw, list):
            for s in raw:
                if isinstance(s, dict):
                    n = s.get("streamName") or s.get("name")
                    if n:
                        streams_found.append(n)
        if streams_found:
            break

    print()
    if streams_found:
        print(f"  Streams discovered: {streams_found}")
        print()
        print("VERDICT: PASS — Stream discovery returns data. Cause #1 is unlikely.")
        sys.exit(0)
    else:
        print("  Streams discovered: (none)")
        print()
        print("VERDICT: FAIL — Empty stream discovery. This is a common cause of connection create 500.")
        print("  Fix: Ensure Drive folder has files, OAuth has drive.readonly, folder shared with account.")
        sys.exit(1)


if __name__ == "__main__":
    main()
