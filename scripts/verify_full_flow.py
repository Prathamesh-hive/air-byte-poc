#!/usr/bin/env python3
"""
Verify full flow for managed Airbyte: tokens.json + Google Sheets URL → connect → trigger sync → Pinecone.
Managed Airbyte only (no OSS/PyAirbyte). Uses Google Sheets source connector.
Pinecone index: pm-test (1536 dim, us-east-1). See docs/AIRBYTE_CONNECTION_CREATE_500.md if you get 500.

Run: python scripts/verify_full_flow.py
Optional env:
  PINECONE_INDEX — Pinecone index name (default pm-test).
  VERIFY_PINECONE_API_KEY — Pinecone key for this script only.
  VERIFY_EXISTING_CONNECTION_ID — Skip connection create, use this connection id.
"""
import json
import os
import re
import sys
import time
import uuid

# Add project root for imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
sys.path.insert(0, _root)
os.chdir(_root)

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("AIRBYTE_REQUEST_TIMEOUT", "180")

# Google Sheets URL to sync
GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/1cym08LrNKq8kqoxk3HTPq_2NJjGNGAv7f4ZvXiaCUh8/edit?gid=0#gid=0"
TOKENS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tokens.json")


def extract_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError(f"Invalid Google Sheets URL: {url}")
    return m.group(1)


def main():
    print("=== Full flow verification (Airbyte Cloud + Pinecone) ===", flush=True)
    print(flush=True)

    # 0. Env check. Set PINECONE_INDEX before importing app so destination uses pm-test (1536, us-east-1).
    pinecone_key = os.getenv("VERIFY_PINECONE_API_KEY") or os.getenv("PINECONE_API_KEY")
    pinecone_index = os.getenv("PINECONE_INDEX", "pm-test")
    os.environ["PINECONE_INDEX"] = pinecone_index
    if pinecone_key:
        os.environ["PINECONE_API_KEY"] = pinecone_key
    ab_url = os.getenv("AIRBYTE_API_URL")
    ab_workspace = os.getenv("AIRBYTE_WORKSPACE_ID")
    print(f"PINECONE_INDEX: {pinecone_index}", flush=True)
    print(f"AIRBYTE_API_URL: {ab_url}", flush=True)
    print(f"AIRBYTE_WORKSPACE_ID: {ab_workspace}", flush=True)
    if not pinecone_key:
        print("ERROR: Set PINECONE_API_KEY or VERIFY_PINECONE_API_KEY in .env or env")
        sys.exit(1)
    if not ab_workspace or not ab_url:
        print("ERROR: AIRBYTE_API_URL and AIRBYTE_WORKSPACE_ID required in .env")
        sys.exit(1)
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

    spreadsheet_id = extract_spreadsheet_id(GOOGLE_SHEETS_URL)
    namespace = f"verify-{uuid.uuid4().hex[:12]}"
    print(f"Client ID:      {client_id}", flush=True)
    print(f"Spreadsheet ID: {spreadsheet_id}", flush=True)
    print(f"Pinecone ns:    {namespace}", flush=True)
    print("(If you get 403, ensure OAuth has scope https://www.googleapis.com/auth/spreadsheets.readonly)", flush=True)
    print(flush=True)

    # 2. Import app and seed tenant (in-process)
    try:
        from app.main import (
            TENANT_STORE,
            ensure_airbyte_connection,
            airbyte_trigger_sync,
            index,
            now_iso,
        )
    except ImportError as e:
        print(f"ERROR: Missing dependency: {e}. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    credentials = {
        "token": google.get("token"),
        "refresh_token": google["refresh_token"],
        "token_uri": google.get("token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": google["client_id"],
        "client_secret": google["client_secret"],
        "scopes": google.get("scopes", ["openid", "https://www.googleapis.com/auth/drive.readonly"]),
        "expiry": google.get("expiry"),
    }
    existing_conn_id = os.getenv("VERIFY_EXISTING_CONNECTION_ID", "").strip()
    integrations = []
    if existing_conn_id:
        integrations = [{"integration_type": "google_sheets", "config": {"spreadsheet_id": spreadsheet_id}, "name": f"sheets-{client_id[:8]}", "airbyte_connection_id": existing_conn_id}]
    tenant = {
        "name": "verify-script",
        "pinecone_namespace": namespace,
        "spreadsheet_id": spreadsheet_id,
        "registered_docs": {},
        "integrations": integrations,
        "airbyte_source_id": None,
        "airbyte_connection_id": existing_conn_id or None,
        "created_at": now_iso(),
        "credentials": credentials,
    }
    TENANT_STORE[client_id] = tenant
    print("Seeded tenant with credentials and spreadsheet_id.", flush=True)
    if existing_conn_id:
        print(f"Using existing connection ID (VERIFY_EXISTING_CONNECTION_ID): {existing_conn_id}", flush=True)
    print(flush=True)

    # 3. Connect (creates source + connection in Airbyte when API mode; skip if using existing connection)
    if existing_conn_id:
        print("Step 1: Skipping ensure_airbyte_connection (using existing connection).", flush=True)
        out = {"status": "ready", "mode": "airbyte-api", "integrations": 1}
        print(f"  -> {out}", flush=True)
        print(flush=True)
    else:
        print("Step 1: ensure_airbyte_connection (create source + destination + connection)...", flush=True)
        try:
            out = ensure_airbyte_connection(client_id)
            print(f"  -> {out}", flush=True)
            print(flush=True)
        except Exception as e:
            err_str = str(e)
            print(f"  FAILED: {err_str}", flush=True)
            print("  See docs/AIRBYTE_CONNECTION_CREATE_500.md. Common: GET /streams or POST /connections returns 500.", flush=True)
            if "500" in err_str:
                print("  Diagnosing where it failed (sources, destinations, GET /streams, POST /connections)...", flush=True)
                try:
                    from app.main import _airbyte_request_raw, _airbyte_request, _airbyte_entity_id
                    ws = os.getenv("AIRBYTE_WORKSPACE_ID")
                    list_src = _airbyte_request("GET", f"/sources?workspaceIds={ws}")
                    sources = list_src.get("sources", list_src.get("data", []))
                    name = f"sheets-{client_id[:8]}"
                    src_id = None
                    for s in sources:
                        if s.get("name") == name:
                            src_id = _airbyte_entity_id(s, "sourceId")
                            print(f"  Found source {name} -> {src_id}", flush=True)
                            break
                    if not src_id:
                        print(f"  No source named {name}. Failure was likely during source create.", flush=True)
                    dest_list = _airbyte_request("GET", f"/destinations?workspaceIds={ws}")
                    dests = dest_list.get("destinations", dest_list.get("data", []))
                    dest_name = f"pinecone-{pinecone_index}"
                    dest_id = None
                    for d in dests:
                        if d.get("name") == dest_name:
                            dest_id = _airbyte_entity_id(d, "destinationId")
                            print(f"  Found destination {dest_name} -> {dest_id}", flush=True)
                            break
                    if not dest_id:
                        print(f"  No destination {dest_name}. Have: {[d.get('name') for d in dests]}", flush=True)
                    if src_id and dest_id:
                        print("  GET /streams (timeout 150s)...", flush=True)
                        status_s, body_s = _airbyte_request_raw("GET", f"/streams?sourceId={src_id}&destinationId={dest_id}", timeout=150)
                        print(f"  GET /streams -> {status_s}. (Body len: {len(body_s or '')})", flush=True)
                        if status_s != 200:
                            print(f"  Response: {(body_s or '')[:500]}", flush=True)
                        stream_configs = [{"name": "Sheet1", "syncMode": "full_refresh_overwrite"}]
                        if status_s == 200 and body_s:
                            try:
                                data = json.loads(body_s)
                                raw = data.get("data", data.get("streams", []))
                                if isinstance(raw, list) and raw:
                                    discovered = [{"name": (x.get("streamName") or x.get("name")), "syncMode": "full_refresh_overwrite"} for x in raw if isinstance(x, dict) and (x.get("streamName") or x.get("name"))]
                                    if discovered:
                                        stream_configs = discovered
                                        print(f"  Streams: {[c['name'] for c in stream_configs]}", flush=True)
                            except Exception:
                                pass
                        if status_s == 200:
                            print("  POST /connections (timeout 120s)...", flush=True)
                            payload = {"sourceId": src_id, "destinationId": dest_id, "name": f"poc-{client_id[:8]}-conn", "configurations": {"streams": stream_configs}, "namespaceDefinition": "custom_format", "namespaceFormat": namespace, "schedule": {"scheduleType": "manual"}}
                            status, body = _airbyte_request_raw("POST", "/connections", payload, timeout=120)
                            print(f"  POST /connections -> {status}. Body: {(body or '')[:600]}", flush=True)
                            if status not in (200, 201):
                                print("  Failure at connection create. See docs/AIRBYTE_CONNECTION_CREATE_500.md.", flush=True)
                        else:
                            print("  Failure at GET /streams (connector error in Airbyte). Check Airbyte Cloud logs.", flush=True)
                    print("  Workaround: Create connection in Airbyte UI, then VERIFY_EXISTING_CONNECTION_ID=<id>.", flush=True)
                except Exception as dbg:
                    print(f"  Debug failed: {dbg}", flush=True)
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
