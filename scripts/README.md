# Scripts

## verify_full_flow.py

Verifies the full pipeline: **tokens.json** + **Drive folder URL** → seed tenant → connect (Airbyte) → trigger sync → Pinecone.

**Requirements:**

- `.env` configured for Airbyte OSS (`AIRBYTE_USE_API=1`, `AIRBYTE_API_URL`, workspace, client id/secret).
- Airbyte OSS running at `AIRBYTE_API_URL` (e.g. http://localhost:8000).
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
