# Scripts

## diagnose_managed_airbyte.py (exhaustive diagnostic)

Runs the managed Airbyte flow **step-by-step** and stops at the first failure so you see exactly where it breaks.

**Full flow:** `python scripts/diagnose_managed_airbyte.py`

Steps: 0 Env + tokens → 1 Airbyte token → 2 Destination → 3 Source → 4 Connection → 5 Trigger job → 6 Poll job → 7 Pinecone check.

**Connection create via API:** The script and app try several strategies (and retry up to 3 times on 500): (0) **Airbyte SDK** `create_connection` (minimal body) then PATCH streams/namespace/schedule—install `airbyte-api` to use; (1) full payload; (2) minimal then PATCH; (3) namespaceDefinition `"source"`; (4) no configurations; (5) full_refresh_append; (6) snake_case; (7) empty streams then PATCH; (8) no schedule; (9) top-level `streamConfigurations`. Discovery: GET /streams is called (with sourceId only fallback, 90s timeout) unless **AIRBYTE_SKIP_DISCOVERY=1**. Timeouts: **AIRBYTE_CONNECTION_CREATE_TIMEOUT** (default 90), **AIRBYTE_STREAMS_TIMEOUT** (default 90). If all fail (common for Google connectors in Airbyte Cloud sandbox), use the workaround below.

**If Step 4 (Connection) fails with 500** (connector runs in Airbyte’s sandbox and may fail):

1. In **Airbyte Cloud UI**: create **Source** (Google Sheets, your spreadsheet URL, OAuth), **Destination** (Pinecone, your index/key), and **Connection** (source → destination; set destination namespace to any value, e.g. `verify-test`).
2. Copy the **Connection ID** from the UI.
3. Run:
   ```bash
   MANAGED_AIRBYTE_CONNECTION_ID=<connection_id> MANAGED_AIRBYTE_NAMESPACE=verify-test python scripts/diagnose_managed_airbyte.py
   ```
   The script will only trigger sync and verify Pinecone (no source/connection create).

**Credentials:** Uses `.env` (Airbyte + Pinecone) and `tokens.json` (Google OAuth). For Google Sheets, ensure OAuth has scope `https://www.googleapis.com/auth/spreadsheets.readonly` (or re-auth in Airbyte UI when creating the source).

---

## verify_full_flow.py

Verifies the full pipeline: **tokens.json** + **Drive folder URL** → seed tenant → connect (Airbyte) → trigger sync → Pinecone.

**Requirements:**

- `.env` with Pinecone (`PINECONE_API_KEY`, `PINECONE_INDEX`), Airbyte (`AIRBYTE_API_URL`, `AIRBYTE_WORKSPACE_ID`, `AIRBYTE_CLIENT_ID`, `AIRBYTE_CLIENT_SECRET` or `AIRBYTE_ACCESS_TOKEN`), and `AIRBYTE_USE_API=1`.
- **Airbyte Cloud only**: `AIRBYTE_API_URL=https://api.airbyte.com`. The 500 on connection create is a known sandbox issue with the Google Drive connector (not free vs paid). Create the connection in Cloud UI, then run with `VERIFY_EXISTING_CONNECTION_ID=<connection_id>`.
- `tokens.json` in project root with `client_id` and `google` (token, refresh_token, client_id, client_secret).

**Usage:**

```bash
cd "/path/to/estuary poc"
python scripts/verify_full_flow.py
```

**What it does:**

1. Reads `tokens.json` and extracts client_id + Google OAuth credentials.
2. Uses a fixed Drive folder URL and a **random Pinecone namespace** (`verify-<hex>`).
3. Seeds the in-memory tenant (no running POC server needed) and calls:
   - `ensure_airbyte_connection` (validates Google, creates/updates Airbyte source + connection).
   - `airbyte_trigger_sync` (triggers sync job(s) via Airbyte API).
4. Waits 2+ min, then checks Pinecone for that namespace.

**Note:** If the expected namespace does not appear in Pinecone, check the Airbyte UI (connection’s namespace config and job logs). The flow is still valid if connect and trigger succeed.
