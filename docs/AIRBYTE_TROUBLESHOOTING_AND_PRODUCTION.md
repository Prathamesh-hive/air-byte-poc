# Airbyte POC: Troubleshooting Summary & Production Readiness

This document summarizes everything we tried to get Google Drive → Pinecone sync working, what worked, what didn’t, how we fixed it, and how to productionalize the system **without** the backend running connector logic or fetching.

---

## 1. What We Tried: Full Chronology

### 1.1 Initial Failure: “Connect to Airbyte” Not Working

| What we tried | Result | Why |
|---------------|--------|-----|
| Call Airbyte `/v1/applications/token` with `client_id` and `client_secret` only | **Failed** (e.g. HTTP 502) | Airbyte Cloud token endpoint expects `grant-type: "client_credentials"` in the JSON body. Without it, token exchange fails. |

**Fix:** In `_airbyte_bearer_token()`, add `"grant-type": "client_credentials"` to the POST body. Use `expires_in` from the response for token cache TTL instead of a hardcoded 120s.

---

### 1.2 Connection Create: HTTP 500 “Unexpected problem”

| What we tried | Result | Why |
|---------------|--------|-----|
| Create connection after creating source + destination | **Failed** (500) | Multiple causes: (1) `drive_folder_id` was null or `"root"` → invalid `folder_url` for the Google Drive connector. (2) On server restart, `airbyte_source_id` was lost → new source created every time → many duplicate sources. (3) Code called `GET /v1/streams` to discover catalog before creating the connection; that call runs the connector in Airbyte Cloud’s sandbox and was failing (see 1.5). |

**Fixes:**

- Require a valid `drive_folder_id` before creating/updating source; reject with 400 if missing.
- Look up existing source by name (`_airbyte_lookup_source_by_name`) and PATCH it instead of always POSTing a new one.
- Remove the dependency on `GET /streams` for connection create; send a fixed stream config `[{"name": "documents", "syncMode": "full_refresh_overwrite"}]` in the connection payload.
- Add `DELETE /airbyte/cleanup-sources` to delete duplicate sources when needed.

---

### 1.3 Connection Create: HTTP 400 “StreamConfiguration parameter name null”

| What we tried | Result | Why |
|---------------|--------|-----|
| Send stream config with field `streamName` | **Failed** (400) | Airbyte Public API expects the field **`name`**, not `streamName`, in each stream configuration object. |

**Fix:** In the connection create/update payload, use `"name": "documents"` (and `"syncMode": "full_refresh_overwrite"`) instead of `"streamName"`.  

---

### 1.4 Google Drive API 403 When Testing

| What we tried | Result | Why |
|---------------|--------|-----|
| Use `tokens.json` (from OAuth callback) in `debug_airbyte.py` to call Drive API and Airbyte | **Failed** (403 on Drive list) | Google Drive API was **not enabled** in the GCP project. The project had OAuth and client credentials but the Drive API was disabled. |

**Fix:** Enable “Google Drive API” in Google Cloud Console for the project (`https://console.developers.google.com/apis/api/drive.googleapis.com/overview`). After enabling, direct Drive API calls (and token refresh) returned 200.

---

### 1.5 Airbyte Cloud: Connector Still Fails (500) After Fixes

| What we tried | Result | Why |
|---------------|--------|-----|
| Create source (with valid folder_url + credentials) and then call `GET /v1/streams` or `POST /v1/connections` | **Failed** (500) every time | Airbyte Cloud runs the Google Drive connector **inside its sandbox**. Catalog discovery and connection creation trigger the connector there. We confirmed the **same config and credentials** work when the connector runs locally in Docker. So the failure is in **Airbyte Cloud’s environment** (e.g. network restrictions, sandbox isolation, or blocked outbound calls to `oauth2.googleapis.com` or Drive). |
| Try different source config variants (unstructured, jsonl, csv, minimal streams) | **Failed** (500) for all | Same sandbox/connector execution issue. |
| Create connection without calling `/streams` (with fixed stream config) | **Failed** (500) | Creating the connection still triggers connector validation/catalog discovery on Airbyte’s side. |

**Conclusion:** Credentials and connector config are correct. The blocker is **Airbyte Cloud’s execution environment** for this connector, not our code or Google setup.

---

### 1.6 Local Connector: Docker and PyAirbyte

| What we tried | Result | Why |
|---------------|--------|-----|
| Run `airbyte/source-google-drive` via Docker (`check`, `discover`) with same config as Cloud | **Worked** | Connector has full network access; token refresh and Drive API calls succeed. |
| Run PyAirbyte in-process (no Docker) | **Failed** in some envs | `ModuleNotFoundError: No module named '_lzma'` (Python build without lzma). Not a connector bug. |
| Run PyAirbyte with `docker_image=airbyte/source-google-drive:latest` | **Worked** | PyAirbyte runs the connector in Docker; same as manual Docker run. Check, discover, and read all succeed. |
| Implement sync in backend: PyAirbyte read → chunk → OpenAI embed → Pinecone upsert | **Worked** | End-to-end flow works: one Drive doc → one vector → query returns correct match. |

**Additional fixes during local testing:**

- **OpenAI client error:** `TypeError: Client.__init__() got an unexpected keyword argument 'proxies'` — driven by `httpx` 0.28+ and older `openai`. **Fix:** Upgrade `openai` (e.g. `>=1.58`).
- **Pinecone 400:** “Vector dimension 1536 does not match the dimension of the index 1024”. **Fix:** Use `dimensions=1024` (or `OPENAI_EMBED_DIMENSIONS`) in all `embeddings.create()` calls so they match the Pinecone index.
- **PyAirbyte incremental vs full refresh:** Use `source.read(cache=cache, force_full_refresh=True)` so the first sync is a full read.

---

## 2. What Works Today (POC)

- **Backend (current state):**
  - OAuth, token storage, `/airbyte/connect` (validates credentials + folder via Google token refresh).
  - `/airbyte/trigger-sync`: runs **PyAirbyte in-process**: starts Docker connector, reads “documents” stream into DuckDB cache, chunks text, calls OpenAI Embeddings, upserts to Pinecone.
- **Implication:** The **backend is doing connector execution and fetching** (via PyAirbyte + Docker). That contradicts the requirement: *“Our backend won’t be doing connector logic and fetching.”*

---

## 3. Production: Constraint and Options

**Constraint:** The **backend (API server) must not** run connector logic or fetch data from Google Drive. Sync and fetching should be done by Airbyte (Cloud) or by a **separate sync worker**, not by the main app process.

So we have two high-level options:

1. **Airbyte Cloud** — Backend only calls Airbyte API (create source/connection, trigger job). No connector runs on your infra.
2. **Local Airbyte (PyAirbyte) via a separate worker** — A **dedicated sync worker service** (not the API server) runs PyAirbyte + Docker and performs sync; the backend only triggers that worker (e.g. via HTTP or queue) and does not run any connector code.

Below we spell out what each option implies for the server, limitations, and what to put where.

---

## 4. Option A: Airbyte Cloud (Preferred If It Works)

### Why choose Airbyte Cloud?

| Reason | Detail |
|--------|--------|
| **No connector infra on your side** | You don’t run Docker, connector images, or PyAirbyte. No sync worker to deploy, scale, or secure. Backend stays thin: OAuth + API calls + RAG. |
| **Managed runs and reliability** | Airbyte runs, monitors, and retries syncs. You don’t own scheduling, container resource limits, or connector crashes. |
| **Connector updates and security** | New connector versions and security fixes are applied by Airbyte. With a local worker you must pull new images and redeploy. |
| **Unified UI and observability** | Connection config, sync history, and logs live in Airbyte’s UI. With a local worker you build and maintain your own dashboards and alerting. |
| **Less operational surface** | No credential storage for the worker, no queue (SQS/Celery) for sync jobs, no “worker down” or “Docker socket” incidents on your infra. |
| **Fits “backend doesn’t fetch”** | Backend only calls the Airbyte API to create/update sources and connections and to trigger syncs. No connector logic or Drive fetching in your codebase. |

**Trade-off:** You depend on Airbyte Cloud’s availability and on their sandbox working for the Google Drive connector (we saw 500 in POC; needs re-test or support). If that’s resolved, Cloud is the simpler and more maintainable option.

---

### 4.1 What You Want

- Backend: OAuth, token storage, Airbyte API calls only (create/update source, create/update connection, trigger sync).
- Airbyte Cloud: runs the Google Drive connector, syncs to Pinecone (with Airbyte’s Pinecone destination and embedding if configured there), or at least runs the source; you may still do embedding/upsert elsewhere if needed.
- **Nothing** on your server: no Docker for connectors, no PyAirbyte.

### 4.2 What We Hit in POC

- Creating a connection (or calling `/streams`) in Airbyte Cloud always led to **500** (“Something went wrong in the connector”) even with valid credentials and Drive API enabled.
- Same config works in Docker locally → issue is **Airbyte Cloud’s connector execution environment**, not your config.

### 4.3 What to Do for Production

1. **Re-test Airbyte Cloud** (new workspace or after Airbyte changes):
   - Use the same source config (folder_url, credentials, streams with `unstructured`).
   - If it still 500s, **open a ticket with Airbyte** and share: connector = Google Drive, action = connection create or catalog discovery, error = 500, and that the same config works in self-hosted/Docker.
2. **Use Airbyte’s OAuth flow if available** so Airbyte holds and refreshes tokens in their environment; sometimes that avoids sandbox/network issues.
3. **Backend responsibilities:** Only call Airbyte API; no connector execution, no Drive fetching.

### 4.4 What Must Be on the Server (Airbyte Cloud)

| Component | On your server? | Notes |
|----------|-----------------|--------|
| Backend (FastAPI) | Yes | OAuth, token store, Airbyte API client, RAG (Pinecone + LLM). |
| Docker | No | Not needed for Airbyte Cloud. |
| PyAirbyte | No | Not needed. |
| Airbyte connector | No | Runs in Airbyte Cloud. |

**Limitations:** Depends on Airbyte Cloud’s reliability and sandbox; we currently cannot create a working connection from the API for this connector until Airbyte fixes or explains the 500.

---

## 5. Option B: Local Airbyte (PyAirbyte) — Sync Worker Architecture

If you stay with “connector runs locally” (PyAirbyte + Docker), the **backend must not** run it. You need a **separate sync worker** that does the fetch and (optionally) embed/upsert.

### 5.1 Architecture

- **API server (backend):**  
  - Client CRUD, OAuth, token storage.  
  - “Connect” = validate credentials + folder (e.g. token refresh).  
  - “Trigger sync” = enqueue a job (e.g. POST to sync worker or push to SQS/Celery).  
  - **No** PyAirbyte, **no** Docker, **no** connector logic, **no** Drive fetch.

- **Sync worker (separate process/service):**  
  - Consumes jobs (e.g. “sync client_id X”).  
  - Loads credentials + folder_id from your store (DB or secure storage).  
  - Runs PyAirbyte (Google Drive source in Docker) → reads documents → chunks → OpenAI embed → Pinecone upsert.  
  - Reports status back (DB or callback URL) so the API can show “last sync” etc.

So: **connector logic and fetching live only in the sync worker**, not in the backend.

### 5.2 What Must Be on the Server (Local Airbyte)

| Component | Where | Notes |
|-----------|--------|------|
| Backend (FastAPI) | API server | No Docker, no PyAirbyte, no connector. |
| Sync worker | **Separate** host/container/process | Must have Docker + PyAirbyte; runs connector, optional chunk/embed/upsert. |
| Docker + Docker socket | **Sync worker only** | Required for PyAirbyte to run `source-google-drive`. |
| Python + PyAirbyte + deps | **Sync worker only** | Same as current POC sync path, but in a dedicated service. |
| Credential store | Shared (DB or secrets manager) | Worker reads tokens + folder_id for each job. |

### 5.3 What the Sync Worker Needs

- **Runtime:** Docker, Python 3.10+, PyAirbyte, `openai`, `pinecone`, `requests`.
- **Config:** Same env as today for OpenAI, Pinecone, and (for the worker) read-only access to tenant credentials and `drive_folder_id`, `pinecone_namespace`.
- **Trigger:** HTTP endpoint (e.g. `POST /sync` with `client_id`) or a queue consumer (SQS, Redis, Celery).
- **Idempotency / concurrency:** Decide whether one sync per client at a time; use locks or a single-worker queue if needed.

**Who triggers sync (local)?** The Docker container runs only when a sync is **explicitly started** (e.g. backend calls trigger-sync → worker runs PyAirbyte → PyAirbyte starts the connector container, reads, then the container exits). There is no built-in scheduler in the connector or PyAirbyte. So in local you **must** trigger sync manually (UI/API) or add your own scheduler (cron, Celery Beat, EventBridge, etc.) that periodically calls the worker or enqueues sync jobs.

**Is there an Airbyte UI for the local (PyAirbyte) setup?** No. PyAirbyte only runs the connector in Docker on-demand; it does not run Airbyte’s server or web app. The “UI” in this setup is **your app’s UI** (e.g. trigger sync, view client, RAG). If you want the official Airbyte UI locally, you must run **Airbyte OSS** (self-hosted full platform); see section 10 below.

### 5.4 Limitations of Local Airbyte (PyAirbyte)

| Limitation | Detail |
|------------|--------|
| **Docker dependency** | Sync worker must run where Docker (and optionally Docker Compose) is available. Not ideal for pure serverless (Lambda) unless you use a Docker-based Lambda or run worker on EC2/EKS. |
| **Resource usage** | Each sync runs a container (CPU/memory). Many concurrent syncs = many containers. Need to size the worker host and/or limit concurrency. |
| **Maintenance** | You upgrade connector images (e.g. `airbyte/source-google-drive`) and PyAirbyte. Airbyte Cloud would handle connector updates. |
| **Scaling** | Horizontal scaling = multiple workers + queue; need to avoid duplicate syncs and handle failures/retries. |
| **Chunking/embedding** | In the POC this is in the same process as the connector. In production it stays in the **sync worker**; backend never does it. |
| **No Airbyte UI** | You don’t get Airbyte’s UI for connection config or logs; you build your own status/logging. |

### 5.5 Summary Table: Who Does What

| Responsibility | Airbyte Cloud (Option A) | Local PyAirbyte + Worker (Option B) |
|----------------|--------------------------|-------------------------------------|
| OAuth + token storage | Backend | Backend |
| Create source/connection | Backend → Airbyte API | N/A (worker uses config from your store) |
| Trigger sync | Backend → Airbyte API | Backend → Worker (HTTP/queue) |
| Run connector / fetch Drive | Airbyte Cloud | **Sync worker only** |
| Chunk + embed + Pinecone | Airbyte (or separate pipeline) | **Sync worker only** |
| Query / RAG | Backend | Backend |
| Docker on API server | No | No |
| Docker on worker | N/A | **Yes** |

---

## 6. Recommendations

1. **Short term:**  
   - Keep the current POC as a **reference** that the connector and flow work when run locally (Docker + PyAirbyte).  
   - Do **not** treat “backend runs PyAirbyte” as the production design; it violates “backend doesn’t do connector logic or fetching.”

2. **Production target (if you’re OK paying for Airbyte Cloud):**  
   - **Prefer Airbyte Cloud.**  
   - Backend: only Airbyte API + OAuth + RAG.  
   - Re-validate connection create in Cloud (new workspace / support ticket). If 500 persists, get Airbyte to fix or document sandbox limits for the Google Drive connector.

3. **If Airbyte Cloud is not viable:**  
   - Introduce a **dedicated sync worker** that runs PyAirbyte + Docker and performs all connector execution and (if desired) chunk/embed/upsert.  
   - Backend: trigger sync via the worker (or queue), never run the connector or fetch Drive.

4. **Regardless of option:**  
   - Store credentials and `drive_folder_id` in a persistent, secure store (DB or secrets manager) so both backend and (if used) sync worker can access them.  
   - Keep embedding dimension aligned with Pinecone index (e.g. `OPENAI_EMBED_DIMENSIONS=1024`).

---

## 7. Files Changed During Troubleshooting (Reference)

| File | Changes |
|------|--------|
| `app/main.py` | Token body fix; source lookup by name; no `/streams`; connection payload `name` not `streamName`; `ensure_airbyte_connection` → credential validation only; `airbyte_trigger_sync` → PyAirbyte + chunk + embed + Pinecone; `OPENAI_EMBED_DIMENSIONS`; `force_full_refresh=True`. |
| `debug_airbyte.py` | Script to test token refresh, Drive list, source variants, `/streams`, connection create using `tokens.json`. |
| `test_pyairbyte.py` | Local PyAirbyte check + stream list. |
| `test_full_pipeline.py` | End-to-end: PyAirbyte read → embed (1024-dim) → Pinecone upsert → query. |
| `tokens.json` | Written by OAuth callback for debugging; contains Google credentials + `drive_folder_id`. |
| `.env` | No structural change; ensure Drive API enabled in GCP. |
| Dependencies | `openai>=1.58` to fix `httpx` 0.28 compatibility. |

---

## 8. Quick Reference: Env and Endpoints

**Backend env (relevant):**  
`GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_REDIRECT_URI`, `PINECONE_API_KEY`, `PINECONE_INDEX`, `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBED_MODEL`, `OPENAI_EMBED_DIMENSIONS` (e.g. 1024), `AIRBYTE_API_URL`, `AIRBYTE_CLIENT_ID`, `AIRBYTE_CLIENT_SECRET`, `AIRBYTE_WORKSPACE_ID`. Optional: `GDRIVE_SOURCE_IMAGE`, `SYNC_CHUNK_SIZE`, `SYNC_CHUNK_OVERLAP`.

**If using sync worker:**  
Worker needs Docker, PyAirbyte, OpenAI, Pinecone, and read access to tenant credentials and config (e.g. from your API or DB).

**Endpoints (current POC):**  
- `POST /airbyte/connect` — Validate credentials + folder (no connector run).  
- `POST /airbyte/trigger-sync` — **Today:** runs PyAirbyte in-process. **Production (Option B):** should call sync worker or enqueue job instead.

---

## 9. Refactor for Production (If You Use Option B — Sync Worker)

To align with “backend does not do connector logic or fetching”:

1. **Move sync implementation out of the API server**  
   - Remove PyAirbyte and Docker-dependent code from `app/main.py` (e.g. `airbyte_trigger_sync`’s current body that runs `ab.get_source`, `source.read`, chunking, embed, upsert).  
   - Keep in the backend: validation, credential refresh, and “trigger” logic only.

2. **Implement a sync worker service**  
   - New service (e.g. `worker/sync_worker.py` or a small FastAPI/Flask app) that:  
     - Exposes something like `POST /sync` with `client_id` (or consumes from a queue).  
     - Loads tenant credentials and `drive_folder_id`, `pinecone_namespace` from your DB or API.  
     - Runs the current PyAirbyte + chunk + embed + Pinecone upsert logic.  
     - Writes `last_sync_at` / status to DB or calls back the API.

3. **Change `POST /airbyte/trigger-sync` in the backend**  
   - Instead of calling PyAirbyte, the backend should:  
     - Enqueue a sync job (e.g. POST to the worker’s `POST /sync` with `client_id`), or  
     - Push a message to SQS/Redis/Celery with `client_id`.  
   - Return immediately with something like `{"status": "queued", "job_id": "..."}`.  
   - Optionally poll or webhook for completion and store `last_sync_at` on the tenant.

4. **Deploy**  
   - API server: no Docker, no PyAirbyte.  
   - Sync worker: runs on a host/container with Docker; has env for OpenAI, Pinecone, and access to tenant store.

---

## 10. Running Airbyte UI Locally (Airbyte OSS — Full Platform)

The **PyAirbyte + single-connector** setup has **no** Airbyte UI; only your app’s UI. To use the **official Airbyte web UI** locally, you run **Airbyte OSS** (self-hosted full platform). That gives you the same kind of UI as Airbyte Cloud (sources, destinations, connections, sync history) on your machine.

### Option A: `abctl` (recommended)

1. **Install abctl** (Airbyte’s CLI):
   ```bash
   curl -LsfS https://get.airbyte.com | bash -
   ```
2. **Install and start Airbyte locally** (uses Docker; may use Kubernetes under the hood):
   ```bash
   abctl local install
   ```
3. **Get credentials** (password for UI login):
   ```bash
   abctl local credentials
   ```
4. **Open the UI:**  
   - URL: **http://localhost:8000** (or the port `abctl` reports).  
   - If your FastAPI app already uses port 8000, either run it on another port (e.g. 8001) or run Airbyte on a different port if `abctl` allows it.  
   - Login: username `airbyte`, password from step 3.

**Requirements:** Docker (Desktop), 4+ CPUs and 8GB RAM recommended.

### Option B: Docker Compose (legacy)

- Clone the Airbyte repo and run:
  ```bash
  git clone https://github.com/airbytehq/airbyte.git && cd airbyte
  docker compose up
  ```
- UI is typically at **http://localhost:8000** (check the repo’s README for current ports).

### How this differs from our POC

| | PyAirbyte (our POC) | Airbyte OSS (full platform + UI) |
|--|---------------------|-----------------------------------|
| What runs | Only the connector container when you call sync | Airbyte server + scheduler + UI + connector containers |
| UI | Your app’s UI only | Official Airbyte UI (sources, connections, logs) |
| API | Your backend talks to Airbyte **Cloud** API (or would talk to OSS API) | Your backend would use the **local** Airbyte API (same API shape as Cloud) |
| Use case | Lightweight: your code triggers sync, no Airbyte server | You want the full Airbyte experience on-prem / locally |

If you run Airbyte OSS, you can point your backend at the local Airbyte API (e.g. `AIRBYTE_API_URL=http://localhost:8000` or the port the OSS server uses) and create sources/connections via the UI or via the same API calls we use for Cloud. The Google Drive connector would then run **inside** this local Airbyte instance (not via PyAirbyte in your process).

---

## 11. What Is Airbyte OSS For? Few Connectors + Need a UI — OSS or PyAirbyte?

### What Airbyte OSS (full self-hosted) is for

Airbyte OSS is the **full platform** you run yourself: web UI, connection management, scheduling, sync history, logs, and **all** Airbyte connectors (Notion, Google Drive, Google Sheets, 300+ others). Use it when:

- You want the **same experience as Airbyte Cloud** but on your own infra (data stays on-prem, compliance).
- You want **one UI** to add sources, configure connections, set schedules, and see logs without building anything.
- You’re OK running and maintaining a larger stack (Airbyte server, DB, temporal/scheduler, connector containers).

### Only a few connectors (Notion, Google Docs, Google Sheets) + need to “visualize and manage”

For production with **only a handful of connectors** and a **need for a UI to visualize/manage**, both approaches can work:

| | **PyAirbyte + your app** | **Airbyte OSS (full platform)** |
|--|---------------------------|----------------------------------|
| **Connectors** | You run only what you need (e.g. Notion, Google Drive, Sheets) via PyAirbyte + Docker. Each connector = one image. | All connectors available in the UI; you use only the ones you need (Notion, Drive, Sheets). |
| **Management UI** | **No** built-in Airbyte UI. You add management/visualization to **your app**: e.g. clients list, “last sync” per client, “Trigger sync” button, sync history table (from your DB or worker logs), optional simple admin page for job status. | **Yes.** Full Airbyte UI: sources, connections, sync history, logs. No UI to build for sync management. |
| **Worth it?** | **Yes** if you’re fine extending your existing app UI (sync status, history, trigger). Lighter: no Airbyte server, only worker + connector containers. | **Yes** if you want **zero** custom UI for sync and prefer the official Airbyte UI. You run the full OSS stack. |
| **Production** | Works well: sync worker (PyAirbyte + Docker) + backend + your UI. You own the “management” screens (e.g. in your current app/static or a small admin dashboard). | Works well: point backend at OSS API; users/admins use Airbyte UI for connection setup and monitoring. |

### Recommendation

- **PyAirbyte is sufficient** for production with a few connectors (Notion, Google Docs, Sheets): run only those connector images in your sync worker, keep the backend free of connector logic. **Management and visualization** = extend **your** UI: show per-client sync status, last sync time, trigger button, and (if you persist it) sync history or worker job status. No need to run Airbyte OSS unless you explicitly want its UI.
- **Use Airbyte OSS** when you want the **built-in Airbyte UI** for connection/sync management and are OK running and maintaining the full platform. Then your backend only talks to the OSS API; admins use the Airbyte UI to visualize and manage sources/connections/syncs.
