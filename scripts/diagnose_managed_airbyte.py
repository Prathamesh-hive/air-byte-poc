#!/usr/bin/env python3
"""
Exhaustive diagnostic for managed Airbyte: run each step and report exactly where it breaks.
Uses .env and tokens.json. No OSS/PyAirbyte.

Modes:
  1. Full flow: token → destination → source → connection → job → Pinecone check.
  2. Connection-only: set MANAGED_AIRBYTE_CONNECTION_ID + MANAGED_AIRBYTE_NAMESPACE;
     script only triggers sync for that connection and verifies Pinecone (use after creating
     the connection in Airbyte Cloud UI).

Run: python scripts/diagnose_managed_airbyte.py
"""
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("AIRBYTE_REQUEST_TIMEOUT", "120")

GOOGLE_SHEETS_URL = os.getenv("MANAGED_AIRBYTE_SHEETS_URL", "https://docs.google.com/spreadsheets/d/1cym08LrNKq8kqoxk3HTPq_2NJjGNGAv7f4ZvXiaCUh8/edit?gid=0#gid=0")
TOKENS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tokens.json")


def step(name: str, fn, *args, **kwargs):
    """Run one step; print name and OK or FAIL with message."""
    print(f"  [{name}] ...", end=" ", flush=True)
    try:
        out = fn(*args, **kwargs)
        print("OK", flush=True)
        return out
    except Exception as e:
        print("FAIL", flush=True)
        print(f"      Error: {e}", flush=True)
        raise


def extract_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError(f"Invalid Google Sheets URL: {url}")
    return m.group(1)


def main():
    use_existing = os.getenv("MANAGED_AIRBYTE_CONNECTION_ID", "").strip()
    namespace = os.getenv("MANAGED_AIRBYTE_NAMESPACE", "").strip() or f"verify-{uuid.uuid4().hex[:12]}"
    print("=== Managed Airbyte diagnostic (step-by-step) ===", flush=True)
    print(f"  MANAGED_AIRBYTE_CONNECTION_ID: {use_existing or '(not set)'}", flush=True)
    print(f"  Namespace: {namespace}", flush=True)
    print(flush=True)

    # --- Step 0: Env and files ---
    print("Step 0: Env and tokens.json", flush=True)
    def check_env():
        for k in ["PINECONE_API_KEY", "PINECONE_INDEX", "AIRBYTE_API_URL", "AIRBYTE_WORKSPACE_ID"]:
            if not os.getenv(k):
                raise RuntimeError(f"Missing env: {k}")
        if not os.getenv("AIRBYTE_API_KEY") and not os.getenv("AIRBYTE_ACCESS_TOKEN") and not (os.getenv("AIRBYTE_CLIENT_ID") and os.getenv("AIRBYTE_CLIENT_SECRET")):
            raise RuntimeError("Set AIRBYTE_API_KEY, or AIRBYTE_ACCESS_TOKEN, or AIRBYTE_CLIENT_ID + AIRBYTE_CLIENT_SECRET")
        if not os.path.exists(TOKENS_PATH):
            raise FileNotFoundError(TOKENS_PATH)
        with open(TOKENS_PATH) as f:
            d = json.load(f)
        if not d.get("client_id") or not (d.get("google") or {}).get("refresh_token"):
            raise ValueError("tokens.json must have client_id and google.refresh_token")
        return d
    data = step("Env + tokens.json", check_env)
    client_id = data["client_id"]
    google = data.get("google") or {}
    print("  Optional: Google token refresh (validates tokens.json for Google API)...", end=" ", flush=True)
    try:
        r = __import__("requests").post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": google.get("client_id"),
                "client_secret": google.get("client_secret"),
                "refresh_token": google.get("refresh_token"),
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if r.status_code == 200:
            print("OK (token valid)", flush=True)
        else:
            print(f"HTTP {r.status_code} (may need re-auth or add spreadsheets.readonly scope)", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
    print(flush=True)

    if use_existing:
        # --- Connection-only path: trigger job and verify Pinecone ---
        print("Using existing connection (skip source/connection create).", flush=True)
        from app.main import _airbyte_bearer_token, _airbyte_request, index
        step("Get Airbyte token", _airbyte_bearer_token)
        print("  Trigger job...", end=" ", flush=True)
        job = _airbyte_request("POST", "/jobs", {"connectionId": use_existing, "jobType": "sync"})
        job_id = job.get("jobId") or job.get("id")
        print(f"OK jobId={job_id}", flush=True)
        # Poll
        for _ in range(60):
            time.sleep(5)
            j = _airbyte_request("GET", f"/jobs/{job_id}")
            status = j.get("status", "")
            print(f"  Job status: {status}", flush=True)
            if status in ("succeeded", "failed", "cancelled"):
                break
        if status != "succeeded":
            print(f"  Job ended with status: {status}. Check Airbyte UI.", flush=True)
            sys.exit(1)
        print("  Listing all Pinecone namespaces...", flush=True)
        stats = index.describe_index_stats()
        namespaces = stats.get("namespaces") or {}
        total = stats.get("total_vector_count", 0)
        print(f"  Index total_vector_count: {total}", flush=True)
        if not namespaces and total == 0:
            print("  No namespaces/vectors in index. Check: (1) Airbyte connection has a stream enabled (e.g. Sheet1).", flush=True)
            print("  (2) Job logs in Airbyte UI show records synced. (3) Destination uses same index (e.g. pm-test) and API key as .env.", flush=True)
        elif not namespaces:
            print("  (Namespaces dict empty but total_vector_count > 0)", flush=True)
        else:
            for ns_name, ns_data in sorted(namespaces.items()):
                vc = ns_data.get("vector_count", 0)
                label = "(default)" if ns_name == "" else ""
                print(f"    namespace {repr(ns_name)}: vector_count={vc} {label}", flush=True)
            # Check default namespace (Airbyte may write here if no namespace set in connection)
            default_vc = (namespaces.get("") or {}).get("vector_count", 0)
            if default_vc > 0:
                print(f"  -> Vectors are in the default namespace (empty string). Use MANAGED_AIRBYTE_NAMESPACE= to query.", flush=True)
        print("\n=== Done (existing-connection path) ===", flush=True)
        return

    # --- Full flow: token → destination → source → connection → job → Pinecone ---
    spreadsheet_id = extract_spreadsheet_id(GOOGLE_SHEETS_URL)
    credentials = {
        "token": google.get("token"),
        "refresh_token": google["refresh_token"],
        "token_uri": google.get("token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": google["client_id"],
        "client_secret": google["client_secret"],
        "scopes": google.get("scopes", ["openid", "https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.readonly"]),
        "expiry": google.get("expiry"),
    }
    tenant = {"credentials": credentials, "spreadsheet_id": spreadsheet_id, "pinecone_namespace": namespace}
    integration = {"integration_type": "google_sheets", "config": {"spreadsheet_id": spreadsheet_id}, "name": f"sheets-{client_id[:8]}"}

    print("Step 1: Airbyte API token", flush=True)
    from app.main import (
        _airbyte_bearer_token,
        _airbyte_request,
        _airbyte_request_raw,
        _airbyte_entity_id,
        _airbyte_get_or_create_destination,
        _airbyte_get_streams,
        _airbyte_try_connection_create,
        _build_google_sheets_source_config,
        CONNECTOR_DEFINITION_IDS,
        AIRBYTE_WORKSPACE_ID,
    )
    step("Bearer token", _airbyte_bearer_token)
    print(flush=True)

    print("Step 2: Destination (get or create Pinecone)", flush=True)
    dest_id = step("Get/create destination", _airbyte_get_or_create_destination)
    print(f"      destination_id: {dest_id}", flush=True)
    print(flush=True)

    print("Step 3: Source (create Google Sheets source)", flush=True)
    config = _build_google_sheets_source_config(tenant, integration)
    def_id = CONNECTOR_DEFINITION_IDS["google_sheets"]
    name = integration["name"]
    status_s, body_s = _airbyte_request_raw("POST", "/sources", {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": name,
        "definitionId": def_id,
        "configuration": config,
    }, timeout=60)
    if status_s >= 400:
        print(f"  [Create source] FAIL", flush=True)
        print(f"      HTTP {status_s}: {body_s[:600]}", flush=True)
        print("\n  If 403: add scope https://www.googleapis.com/auth/spreadsheets.readonly and re-auth.", flush=True)
        sys.exit(1)
    src_res = json.loads(body_s) if body_s else {}
    source_id = _airbyte_entity_id(src_res, "sourceId")
    print(f"  [Create source] OK source_id={source_id}", flush=True)
    print(flush=True)

    print("Step 4: Connection (source → destination) — trying multiple payload variants", flush=True)
    if os.getenv("AIRBYTE_SKIP_DISCOVERY", "").strip().lower() in ("1", "true", "yes"):
        streams_config = [{"name": "Sheet1", "syncMode": "full_refresh_overwrite"}]
        print("  (AIRBYTE_SKIP_DISCOVERY=1: using hardcoded Sheet1, no GET /streams)", flush=True)
    else:
        try:
            streams_config = _airbyte_get_streams(source_id, dest_id)
        except Exception:
            streams_config = None
        streams_config = streams_config or [{"name": "Sheet1", "syncMode": "full_refresh_overwrite"}]
    print(f"  Using {len(streams_config)} stream(s) for connection", flush=True)
    status_c, body_c = 500, ""
    conn_timeout = int(os.getenv("AIRBYTE_CONNECTION_CREATE_TIMEOUT", "90"))
    for attempt in range(3):
        status_c, body_c = _airbyte_try_connection_create(
            source_id, dest_id, f"poc-{client_id[:8]}-conn", namespace, streams_config, timeout=conn_timeout
        )
        if status_c < 400:
            break
        if status_c != 500:
            break
        if attempt < 2:
            print(f"  [Create connection] All variants returned 500 (attempt {attempt + 1}/3), retrying in 10s...", flush=True)
            time.sleep(10)
    if status_c >= 400:
        print(f"  [Create connection] FAIL", flush=True)
        print(f"      HTTP {status_c}: {body_c[:800]}", flush=True)
        print("\n  Tried: full payload, minimal then PATCH, namespace=source, no config, full_refresh_append, snake_case, empty streams then PATCH, no schedule.", flush=True)
        print("  Workaround: create the connection in Airbyte UI (source and destination already exist):", flush=True)
        print("    1. Connections → New connection → pick the source and your Pinecone destination (e.g. pinecone-pm-test).", flush=True)
        print("    2. Set destination namespace to:", namespace, flush=True)
        print("    3. Run: MANAGED_AIRBYTE_CONNECTION_ID=<connection_id> MANAGED_AIRBYTE_NAMESPACE=" + namespace + " python scripts/diagnose_managed_airbyte.py", flush=True)
        sys.exit(1)
    conn_res = json.loads(body_c) if body_c else {}
    connection_id = _airbyte_entity_id(conn_res, "connectionId")
    print(f"  [Create connection] OK connection_id={connection_id}", flush=True)
    print(flush=True)

    print("Step 5: Trigger sync job", flush=True)
    job = _airbyte_request("POST", "/jobs", {"connectionId": connection_id, "jobType": "sync"})
    job_id = job.get("jobId") or job.get("id")
    print(f"  [Trigger job] OK job_id={job_id}", flush=True)
    print(flush=True)

    print("Step 6: Poll job until done", flush=True)
    for i in range(60):
        time.sleep(5)
        j = _airbyte_request("GET", f"/jobs/{job_id}")
        status = j.get("status", "")
        print(f"  Poll {i+1}: status={status}", flush=True)
        if status == "succeeded":
            break
        if status in ("failed", "cancelled"):
            print(f"  Job {status}. Check Airbyte UI for logs.", flush=True)
            sys.exit(1)
    else:
        print("  Timeout waiting for job.", flush=True)
        sys.exit(1)
    print(flush=True)

    print("Step 7: Pinecone namespace check", flush=True)
    stats = index.describe_index_stats()
    ns_stats = (stats.get("namespaces") or {}).get(namespace)
    if ns_stats and (ns_stats.get("vector_count") or 0) > 0:
        print(f"  OK namespace '{namespace}' vector_count={ns_stats['vector_count']}", flush=True)
    else:
        print(f"  Namespace '{namespace}' missing or 0 vectors. Check connection namespace in Airbyte.", flush=True)
    print("\n=== All steps OK ===", flush=True)


if __name__ == "__main__":
    main()
