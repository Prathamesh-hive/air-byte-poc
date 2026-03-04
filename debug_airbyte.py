#!/usr/bin/env python3
"""
Debug script: tests every Airbyte API variant using real tokens from tokens.json.
Run AFTER doing OAuth in the app (tokens.json will be auto-created).

Usage:
  python debug_airbyte.py
"""
import json, os, sys, time, subprocess, textwrap
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Load real Google tokens ──────────────────────────────────────────────────
if not os.path.exists("tokens.json"):
    print("ERROR: tokens.json not found. Do OAuth in the app first, then run this.")
    sys.exit(1)

with open("tokens.json") as f:
    tok = json.load(f)

g = tok["google"]
REFRESH_TOKEN  = g["refresh_token"]
ACCESS_TOKEN   = g["token"]
GOOGLE_CLIENT_ID     = g["client_id"]
GOOGLE_CLIENT_SECRET = g["client_secret"]
FOLDER_ID      = tok.get("drive_folder_id") or "1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA"
FOLDER_URL     = f"https://drive.google.com/drive/folders/{FOLDER_ID}"

print(f"Loaded tokens.json  client_id={GOOGLE_CLIENT_ID[:20]}...")
print(f"  refresh_token={REFRESH_TOKEN[:20]}...")
print(f"  folder_url={FOLDER_URL}")
print()

# ── Airbyte token ────────────────────────────────────────────────────────────
AB_CLIENT_ID     = os.getenv("AIRBYTE_CLIENT_ID")
AB_CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET")
WORKSPACE_ID     = os.getenv("AIRBYTE_WORKSPACE_ID")
DEST_ID          = "c696c32b-54ca-47a7-a5b7-86f58770628b"

r = requests.post(
    "https://api.airbyte.com/v1/applications/token",
    json={"client_id": AB_CLIENT_ID, "client_secret": AB_CLIENT_SECRET, "grant-type": "client_credentials"},
    timeout=15,
)
AB_TOKEN = r.json()["access_token"]
AB_HEADERS = {"Authorization": f"Bearer {AB_TOKEN}", "Content-Type": "application/json"}
print(f"Airbyte token obtained: {AB_TOKEN[:30]}...")
print()

# ── Step 1: Verify Google credentials directly ────────────────────────────────
print("=" * 60)
print("STEP 1: Verify Google refresh token works")
print("=" * 60)
r_google = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    },
    timeout=15,
)
print(f"Token refresh status: {r_google.status_code}")
if r_google.status_code == 200:
    new_access = r_google.json().get("access_token", "")
    print(f"  ✓ New access_token: {new_access[:30]}...")
    ACCESS_TOKEN = new_access  # use freshest token
else:
    print(f"  ✗ FAILED: {r_google.text[:300]}")
    print("  Cannot continue — fix Google credentials first.")
    sys.exit(1)
print()

# ── Step 2: Verify Drive folder access ───────────────────────────────────────
print("=" * 60)
print("STEP 2: Verify Drive folder access")
print("=" * 60)
r_drive = requests.get(
    f"https://www.googleapis.com/drive/v3/files?q='{FOLDER_ID}'+in+parents&fields=files(id,name,mimeType)&pageSize=5",
    headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    timeout=15,
)
print(f"Drive list status: {r_drive.status_code}")
if r_drive.status_code == 200:
    files = r_drive.json().get("files", [])
    print(f"  ✓ Found {len(files)} file(s) in folder:")
    for f in files:
        print(f"    - {f['name']} ({f['mimeType']})")
else:
    print(f"  ✗ Drive access failed: {r_drive.text[:300]}")
print()

# ── Step 3: Clean up old sources ─────────────────────────────────────────────
print("=" * 60)
print("STEP 3: Clean workspace (delete all sources)")
print("=" * 60)
sources = requests.get(f"https://api.airbyte.com/v1/sources?workspaceIds={WORKSPACE_ID}", headers=AB_HEADERS, timeout=15).json()
print(f"  Found {len(sources['data'])} sources")
for s in sources["data"]:
    rd = requests.delete(f"https://api.airbyte.com/v1/sources/{s['sourceId']}", headers=AB_HEADERS, timeout=15)
    print(f"  DELETE {s['name']} -> {rd.status_code}")
print()

# ── Step 4: Try source variants ───────────────────────────────────────────────
print("=" * 60)
print("STEP 4: Try source creation variants")
print("=" * 60)

BASE_CREDS = {
    "auth_type": "Client",
    "client_id": GOOGLE_CLIENT_ID,
    "client_secret": GOOGLE_CLIENT_SECRET,
    "refresh_token": REFRESH_TOKEN,
}

VARIANTS = {
    "unstructured": {
        "folder_url": FOLDER_URL,
        "credentials": BASE_CREDS,
        "streams": [{"name": "documents", "globs": ["**"], "validation_policy": "Emit Record",
                     "days_to_sync_if_history_is_full": 3, "format": {"filetype": "unstructured"}}],
    },
    "jsonl": {
        "folder_url": FOLDER_URL,
        "credentials": BASE_CREDS,
        "streams": [{"name": "documents", "globs": ["**"], "validation_policy": "Emit Record",
                     "days_to_sync_if_history_is_full": 3, "format": {"filetype": "jsonl"}}],
    },
    "csv": {
        "folder_url": FOLDER_URL,
        "credentials": BASE_CREDS,
        "streams": [{"name": "documents", "globs": ["**/*.gdoc", "**/*.docx"], "validation_policy": "Emit Record",
                     "days_to_sync_if_history_is_full": 3,
                     "format": {"filetype": "csv", "header_definition": {"header_definition_type": "From CSV"}}}],
    },
    "minimal_no_format": {
        "folder_url": FOLDER_URL,
        "credentials": BASE_CREDS,
        "streams": [{"name": "documents", "globs": ["**"]}],
    },
}

source_ids = {}
for vname, config in VARIANTS.items():
    body = {
        "workspaceId": WORKSPACE_ID,
        "name": f"debug-{vname}",
        "definitionId": "9f8dda77-1048-4368-815b-269bf54ee9b8",
        "configuration": config,
    }
    r_src = requests.post("https://api.airbyte.com/v1/sources", headers=AB_HEADERS, json=body, timeout=15)
    if r_src.status_code in (200, 201):
        sid = r_src.json().get("sourceId", "")
        source_ids[vname] = sid
        print(f"  ✓ Created source [{vname}]: {sid[:8]}...")
    else:
        print(f"  ✗ Create failed [{vname}]: {r_src.status_code} {r_src.text[:200]}")
print()

# ── Step 5: Test /streams for each source ────────────────────────────────────
print("=" * 60)
print("STEP 5: Test catalog discovery (/streams) for each variant")
print("=" * 60)
working_source = None
for vname, sid in source_ids.items():
    print(f"  Testing [{vname}] {sid[:8]}...")
    t0 = time.time()
    r_streams = requests.get(
        f"https://api.airbyte.com/v1/streams?sourceId={sid}&destinationId={DEST_ID}",
        headers=AB_HEADERS, timeout=90,
    )
    elapsed = time.time() - t0
    if r_streams.status_code == 200:
        streams_data = r_streams.json()
        print(f"    ✓ SUCCESS in {elapsed:.1f}s — streams: {json.dumps(streams_data)[:200]}")
        working_source = (vname, sid, streams_data)
    else:
        print(f"    ✗ FAILED {r_streams.status_code} in {elapsed:.1f}s: {r_streams.text[:300]}")
print()

# ── Step 6: Create connection with working source ────────────────────────────
print("=" * 60)
print("STEP 6: Create connection")
print("=" * 60)
if working_source:
    vname, sid, streams_data = working_source
    print(f"  Using [{vname}] source {sid[:8]}...")

    # Build stream configs from discovered streams
    if isinstance(streams_data, list):
        stream_list = streams_data
    else:
        stream_list = streams_data.get("streams", streams_data.get("data", []))
    stream_configs = [
        {"name": s.get("streamName") or s.get("name"), "syncMode": "full_refresh_overwrite"}
        for s in stream_list if isinstance(s, dict) and (s.get("streamName") or s.get("name"))
    ] or [{"name": "documents", "syncMode": "full_refresh_overwrite"}]

    payload = {
        "sourceId": sid,
        "destinationId": DEST_ID,
        "name": "debug-connection",
        "namespaceDefinition": "destination",
        "configurations": {"streams": stream_configs},
        "schedule": {"scheduleType": "manual"},
    }
    print(f"  Payload: {json.dumps(payload, indent=2)}")
    r_conn = requests.post("https://api.airbyte.com/v1/connections", headers=AB_HEADERS, json=payload, timeout=120)
    print(f"  CONNECTION: {r_conn.status_code} {r_conn.text[:500]}")
else:
    print("  No working source found. Trying connection creation directly with each source anyway...")
    for vname, sid in source_ids.items():
        print(f"\n  Trying [{vname}] {sid[:8]}...")
        payload = {
            "sourceId": sid,
            "destinationId": DEST_ID,
            "name": f"debug-conn-{vname}",
            "namespaceDefinition": "destination",
            "configurations": {"streams": [{"name": "documents", "syncMode": "full_refresh_overwrite"}]},
            "schedule": {"scheduleType": "manual"},
        }
        t0 = time.time()
        r_conn = requests.post("https://api.airbyte.com/v1/connections", headers=AB_HEADERS, json=payload, timeout=120)
        print(f"  -> {r_conn.status_code} in {time.time()-t0:.1f}s: {r_conn.text[:400]}")

print()

# ── Step 7: Print curl equivalents ───────────────────────────────────────────
print("=" * 60)
print("STEP 7: Equivalent curl commands (for manual testing)")
print("=" * 60)
print(textwrap.dedent(f"""
# Refresh Google token:
curl -s -X POST https://oauth2.googleapis.com/token \\
  -d client_id="{GOOGLE_CLIENT_ID}" \\
  -d client_secret="{GOOGLE_CLIENT_SECRET}" \\
  -d refresh_token="{REFRESH_TOKEN}" \\
  -d grant_type=refresh_token | python3 -m json.tool

# List Drive files in folder:
curl -s "https://www.googleapis.com/drive/v3/files?q=%27{FOLDER_ID}%27+in+parents&fields=files(id,name,mimeType)" \\
  -H "Authorization: Bearer {ACCESS_TOKEN}" | python3 -m json.tool

# Create Airbyte source (unstructured):
curl -s -X POST https://api.airbyte.com/v1/sources \\
  -H "Authorization: Bearer {AB_TOKEN[:30]}..." \\
  -H "Content-Type: application/json" \\
  -d '{{
    "workspaceId": "{WORKSPACE_ID}",
    "name": "test-source",
    "definitionId": "9f8dda77-1048-4368-815b-269bf54ee9b8",
    "configuration": {{
      "folder_url": "{FOLDER_URL}",
      "credentials": {{
        "auth_type": "Client",
        "client_id": "{GOOGLE_CLIENT_ID}",
        "client_secret": "{GOOGLE_CLIENT_SECRET}",
        "refresh_token": "{REFRESH_TOKEN}"
      }},
      "streams": [{{"name": "documents", "globs": ["**"],
        "validation_policy": "Emit Record", "days_to_sync_if_history_is_full": 3,
        "format": {{"filetype": "unstructured"}}
      }}]
    }}
  }}' | python3 -m json.tool
"""))

print("Done. Check output above for what worked.")
