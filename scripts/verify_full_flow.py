#!/usr/bin/env python3
"""
Verify full flow: tokens.json + Drive URL → seed tenant → connect → trigger sync → Pinecone populated.
Uses a random Pinecone namespace. Run from repo root with .env loaded.
Requires: POC backend not running (we import app and use in-process) OR run against API (see USE_API_MODE).
"""
import json
import os
import re
import sys
import time
import uuid

# Add project root for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()
# Airbyte connection/create can be slow; allow 3 min
os.environ.setdefault("AIRBYTE_REQUEST_TIMEOUT", "180")

DRIVE_URL = "https://drive.google.com/drive/folders/1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA"
TOKENS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tokens.json")


def extract_folder_id(url: str) -> str:
    m = re.search(r"/folders/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError(f"Invalid Drive folder URL: {url}")
    return m.group(1)


def main():
    print("=== Full flow verification ===", flush=True)
    print(flush=True)

    # 1. Load tokens.json
    if not os.path.exists(TOKENS_PATH):
        print(f"ERROR: {TOKENS_PATH} not found")
        sys.exit(1)
    with open(TOKENS_PATH) as f:
        data = json.load(f)
    client_id = data.get("client_id")
    google = data.get("google") or {}
    if not client_id or not google.get("refresh_token"):
        print("ERROR: tokens.json must have client_id and google.refresh_token")
        sys.exit(1)

    folder_id = extract_folder_id(DRIVE_URL)
    namespace = f"verify-{uuid.uuid4().hex[:12]}"
    print(f"Client ID:    {client_id}", flush=True)
    print(f"Drive folder: {folder_id}", flush=True)
    print(f"Pinecone ns:  {namespace}", flush=True)
    print(flush=True)

    # 2. Import app and seed tenant (in-process)
    from app.main import (
        TENANT_STORE,
        ensure_airbyte_connection,
        airbyte_trigger_sync,
        index,
        now_iso,
    )

    tenant = {
        "name": "verify-script",
        "pinecone_namespace": namespace,
        "drive_folder_id": folder_id,
        "registered_docs": {},
        "integrations": [],
        "airbyte_source_id": None,
        "airbyte_connection_id": None,
        "created_at": now_iso(),
        "credentials": {
            "token": google.get("token"),
            "refresh_token": google["refresh_token"],
            "token_uri": google.get("token_uri", "https://oauth2.googleapis.com/token"),
            "client_id": google["client_id"],
            "client_secret": google["client_secret"],
            "scopes": google.get("scopes", ["openid", "https://www.googleapis.com/auth/drive.readonly"]),
            "expiry": google.get("expiry"),
        },
    }
    TENANT_STORE[client_id] = tenant
    print("Seeded tenant with credentials and folder_id.", flush=True)
    print(flush=True)

    # 3. Connect (creates source + connection in Airbyte when API mode)
    print("Step 1: ensure_airbyte_connection (validate creds + create integration in Airbyte)...", flush=True)
    try:
        out = ensure_airbyte_connection(client_id)
        print(f"  -> {out}", flush=True)
        print(flush=True)
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 4. Trigger sync (POST /jobs in API mode)
    print("Step 2: airbyte_trigger_sync (trigger Airbyte jobs)...", flush=True)
    try:
        out = airbyte_trigger_sync(client_id)
        print(f"  -> {out}", flush=True)
        print(flush=True)
    except Exception as e:
        print(f"  FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 5. Wait for job(s) to complete then verify Pinecone
    jobs = out.get("jobs", [])
    if jobs:
        print("Step 3: Waiting 2 min for Airbyte sync to write to Pinecone...", flush=True)
        time.sleep(120)
        print("  Wait done.", flush=True)
    else:
        print("Step 3: No jobs in response; waiting 45s...", flush=True)
        time.sleep(45)

    print("Step 4: Checking Pinecone namespace...", flush=True)
    try:
        stats = index.describe_index_stats()
        namespaces = stats.get("namespaces") or {}
        ns_stats = namespaces.get(namespace)
        if ns_stats is None:
            all_ns = list(namespaces.keys())
            print(f"  Namespace '{namespace}' not in index. Existing: {all_ns}", flush=True)
            print("  Re-checking in 30s...", flush=True)
            time.sleep(30)
            stats2 = index.describe_index_stats()
            namespaces2 = stats2.get("namespaces") or {}
            ns_stats = namespaces2.get(namespace)
            if ns_stats is None:
                # Show any namespace with vectors (in case Airbyte uses a different key)
                with_vectors = {k: v.get("vector_count", 0) for k, v in namespaces2.items() if v.get("vector_count", 0) > 0}
                print(f"  Still not found. Namespaces with vectors: {with_vectors}", flush=True)
                if with_vectors:
                    print("  Flow reached Pinecone; namespace key may differ. Check Airbyte connection config.", flush=True)
                else:
                    print("  Check Airbyte UI: connection namespaceFormat and job logs.", flush=True)
            else:
                vc = ns_stats.get("vector_count", 0)
                print(f"  Namespace '{namespace}': vector_count = {vc}", flush=True)
                if vc > 0:
                    print("\n*** SUCCESS: Pinecone is populated for this namespace. ***", flush=True)
        else:
            vc = ns_stats.get("vector_count", 0)
            print(f"  Namespace '{namespace}': vector_count = {vc}", flush=True)
            if vc > 0:
                print("\n*** SUCCESS: Pinecone is populated for this namespace. ***", flush=True)
            else:
                print("\n  WARNING: vector_count is 0. Sync may still be in progress.", flush=True)
    except Exception as e:
        print(f"  Pinecone check failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n=== Done ===", flush=True)


if __name__ == "__main__":
    main()
