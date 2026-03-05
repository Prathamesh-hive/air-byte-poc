#!/usr/bin/env python3
"""
Debug script: tests every Airbyte API variant using real tokens from tokens.json.
Run AFTER doing OAuth in the app (tokens.json will be auto-created).

Usage:
  python debug_airbyte.py
  VERIFY_PINECONE_API_KEY=pcsk_... PINECONE_INDEX=pm-test python debug_airbyte.py
"""
import json, os, sys, time, subprocess, textwrap
import requests
from dotenv import load_dotenv

load_dotenv()

# Pinecone: use VERIFY_PINECONE_API_KEY for script-only override, index default knowledge-base
PINECONE_KEY = os.getenv("VERIFY_PINECONE_API_KEY") or os.getenv("PINECONE_API_KEY")
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "pm-test")
if PINECONE_KEY:
    os.environ["PINECONE_API_KEY"] = PINECONE_KEY
if not PINECONE_KEY:
    print("WARNING: PINECONE_API_KEY / VERIFY_PINECONE_API_KEY not set.")
print(f"Using PINECONE_INDEX={PINECONE_INDEX}")

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

r = requests.post(
    "https://api.airbyte.com/v1/applications/token",
    json={"client_id": AB_CLIENT_ID, "client_secret": AB_CLIENT_SECRET, "grant-type": "client_credentials"},
    timeout=15,
)
AB_TOKEN = r.json()["access_token"]
AB_HEADERS = {"Authorization": f"Bearer {AB_TOKEN}", "Content-Type": "application/json"}
print(f"Airbyte token obtained: {AB_TOKEN[:30]}...")
print()

# ── Get or create Pinecone destination (uses PINECONE_API_KEY, PINECONE_INDEX) ──
print("=" * 60)
print("STEP 0: Get or create Pinecone destination (pm-test)")
print("=" * 60)
DEST_ID = os.getenv("AIRBYTE_DESTINATION_ID", "").strip()
if DEST_ID:
    print(f"  Using AIRBYTE_DESTINATION_ID={DEST_ID[:8]}...")
else:
    openai_key = os.getenv("OPENAI_API_KEY", "")
    embed_dim = int(os.getenv("OPENAI_EMBED_DIMENSIONS", "1536"))
    dest_config = {
        "destinationType": "pinecone",
        "embedding": {"mode": "openai", "openai_key": openai_key, "dimensions": embed_dim},
        "indexing": {
            "index": PINECONE_INDEX,
            "pinecone_key": PINECONE_KEY,
            "pinecone_environment": os.getenv("PINECONE_ENV", "us-east-1"),
        },
        "processing": {
            "chunk_size": 997,
            "chunk_overlap": 20,
            "text_fields": ["content"],
            "metadata_fields": [],
            "text_splitter": {"mode": "separator", "separators": ["\n\n", "\n"], "keep_separator": False},
        },
        "omit_raw_text": False,
    }
    dest_def_id = os.getenv("AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE", "3d2b6f84-7f0d-4e3f-a5e5-7c7d4b50eabd")
    r_dests = requests.get(f"https://api.airbyte.com/v1/destinations?workspaceIds={WORKSPACE_ID}", headers=AB_HEADERS, timeout=15)
    dests = (r_dests.json().get("destinations") or r_dests.json().get("data") or [])
    for d in dests:
        if d.get("name") == f"pinecone-{PINECONE_INDEX}":
            DEST_ID = d.get("destinationId") or d.get("id", "")
            r_patch = requests.patch(f"https://api.airbyte.com/v1/destinations/{DEST_ID}", headers=AB_HEADERS, json={"configuration": dest_config}, timeout=30)
            print(f"  PATCH existing destination -> {r_patch.status_code}")
            break
    if not DEST_ID:
        r_create = requests.post("https://api.airbyte.com/v1/destinations", headers=AB_HEADERS, json={
            "workspaceId": WORKSPACE_ID,
            "name": f"pinecone-{PINECONE_INDEX}",
            "definitionId": dest_def_id,
            "configuration": dest_config,
        }, timeout=30)
        if r_create.status_code in (200, 201):
            DEST_ID = r_create.json().get("destinationId") or r_create.json().get("id", "")
            print(f"  Created destination: {DEST_ID[:8]}...")
        else:
            print(f"  CREATE destination failed: {r_create.status_code} {r_create.text[:300]}")
            sys.exit(1)
print(f"  DEST_ID={DEST_ID[:8] if DEST_ID else 'MISSING'}...")
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
