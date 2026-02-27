# Airbyte POC — Implementation Plan for Coding Agent

---

## PROMPT FOR CODING AGENT

**Objective:** Complete the Airbyte POC for a multi-tenant knowledge base. Company-xyz has 3 clients (A, B, C). Each client authenticates with our app via Google OAuth. We store their tokens and call the Airbyte API to create a Google Drive Source and a Connection to Pinecone (one namespace per client). We do **not** fetch Google Docs or run any connector logic in our backend — Airbyte does all sync, chunking, embedding, and Pinecone writes. Our backend only: (1) client CRUD, (2) OAuth init/callback and token storage, (3) Airbyte API calls to create/update Source and Connection and trigger sync, (4) RAG (query Pinecone by namespace + LLM).

**Instructions:** Work through the "Implementation tasks" section in order. For each task, implement or fix as specified in the codebase. Use the file paths and function/endpoint names given. Mark acceptance criteria as satisfied before moving on. If a task is already implemented, verify behavior and fix any mismatches (e.g. Airbyte API payload shape, dead code). Do not add Google Docs fetching or sync logic to the backend; that is entirely Airbyte’s responsibility.

---

## Current state (reference)

| Component | Location | Status |
|-----------|----------|--------|
| Backend | `app/main.py` | FastAPI app with client CRUD, OAuth, Airbyte helpers, RAG. Some dead code (e.g. `read_doc_text`, `fetch_revision_id`, `split_chunks`, `get_google_services`) — not used for sync. |
| UI | `app/static/index.html`, `app/static/app.js`, `app/static/style.css` | Create client (name, namespace, optional folder), OAuth, Connect to Airbyte, Trigger sync, Register doc, RAG question. |
| Config | `.env`, `README.md` | Env vars documented; no Estuary. |
| Estuary artifact | `estuary/client-flow.template.yaml` | Legacy; not used by this POC. |

**Backend env (relevant):** `GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_REDIRECT_URI`, `PINECONE_*`, `OPENAI_*`, `AIRBYTE_API_URL`, `AIRBYTE_API_KEY`, `AIRBYTE_WORKSPACE_ID`, optional `AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE`, `AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE`.

---

## Architecture (brief)

- **Your app:** Client CRUD, Google OAuth (your consent screen), token storage, Airbyte API (create/update Source + Connection, trigger job), RAG (Pinecone + LLM). No doc fetching, no connector logic.
- **Airbyte:** One Source (Google Drive) per client (credentials from you), one shared Pinecone Destination, one Connection per client with namespace = client’s `pinecone_namespace`. Airbyte polls Drive, chunks, embeds, writes to Pinecone.
- **Pinecone:** Single index; namespaces `client_a`, `client_b`, `client_c`.

---

## Implementation tasks

Execute in order. Each task has a **Deliverable** and **Done when**.

---

### Task 1: Env and docs

**Deliverable:** `.env.example` (or README) lists all required and optional env vars. README states that Airbyte does all sync/fetch; backend does not fetch Google Docs.

**Done when:**

- [ ] File `.env.example` exists with: `GOOGLE_CLIENT_SECRETS_FILE`, `GOOGLE_REDIRECT_URI`, `PINECONE_API_KEY`, `PINECONE_INDEX`, `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBED_MODEL`, `AIRBYTE_API_URL`, `AIRBYTE_API_KEY`, `AIRBYTE_WORKSPACE_ID`, optional `AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE`, `AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE`. Values can be placeholders.
- [ ] README (or a short "Setup" section) says: "Sync and doc fetching are done by Airbyte; this app does not fetch Google Docs or run connector logic."

**Files:** `README.md`, new `.env.example` (or extend README).

---

### Task 2: Remove or isolate dead code (no Google fetch in backend)

**Deliverable:** Backend must not contain code paths that fetch Google Doc content for sync. Either remove unused helpers or document that they are unused.

**Done when:**

- [ ] One of: (a) Remove `read_doc_text`, `fetch_revision_id`, `get_google_services`, and `split_chunks` from `app/main.py` if nothing calls them, or (b) Keep them but add a single comment at the top of the block: `# Not used for sync; Airbyte handles all doc fetch/chunk. Kept for optional future use.`
- [ ] No route or background job in `app/main.py` calls Google Docs/Drive APIs to read doc body for the purpose of syncing to Pinecone.

**Files:** `app/main.py`.

---

### Task 3: Client model and store

**Deliverable:** Client has: `client_id`, `name`, `pinecone_namespace`, optional `drive_folder_id`, `credentials` (after OAuth), `airbyte_source_id`, `airbyte_connection_id`, `registered_docs` (optional), `created_at`.

**Done when:**

- [ ] `ClientCreateRequest` in `app/main.py` has `name`, `pinecone_namespace`, optional `drive_folder_id` (no Estuary fields).
- [ ] `create_client` writes to store with: `name`, `pinecone_namespace`, `drive_folder_id`, `registered_docs: {}`, `airbyte_source_id: None`, `airbyte_connection_id: None`, `created_at`.
- [ ] `get_client` returns `pinecone_namespace`, `drive_folder_id`, `airbyte_connection_id`, `has_auth`; no `estuary_*`.
- [ ] `update_client_config` (PATCH) can update `pinecone_namespace` and `drive_folder_id`.

**Files:** `app/main.py`.

---

### Task 4: OAuth flow

**Deliverable:** Init returns Google auth URL; callback (UI redirect) exchanges code, stores tokens, optionally calls Airbyte connect.

**Done when:**

- [ ] `POST /oauth/init` with `{ "client_id": "..." }` returns `{ "authorization_url": "...", "state": "..." }`. State is stored and mapped to `client_id`.
- [ ] `GET /oauth/callback-ui` (with query from Google) parses state, looks up client_id, exchanges code for tokens, stores in tenant `credentials` (token, refresh_token, client_id, client_secret, token_uri, scopes, expiry). If `AIRBYTE_API_KEY` and `AIRBYTE_WORKSPACE_ID` are set, call `ensure_airbyte_connection(client_id)` (no crash if it fails). Return HTML that posts message to opener and closes.
- [ ] Google scopes include at least Drive read (e.g. `https://www.googleapis.com/auth/drive.readonly` or docs + metadata). No scope that implies "we fetch docs for sync" in backend — we only need tokens to pass to Airbyte.

**Files:** `app/main.py`.

---

### Task 5: Airbyte API helpers

**Deliverable:** Functions that call Airbyte REST API: get-or-create destination, create/update source (Google Drive with client credentials), create/update connection (namespace), trigger sync job.

**Done when:**

- [ ] `_airbyte_request(method, path, json_body=None)` sends request to `AIRBYTE_API_URL/v1{path}` with `Authorization: Bearer AIRBYTE_API_KEY`, `Content-Type: application/json`. If `AIRBYTE_API_KEY` or `AIRBYTE_WORKSPACE_ID` missing, raise 503 with message "Airbyte not configured".
- [ ] `_airbyte_get_or_create_destination()`: GET destinations for workspace; if a destination named `pinecone-knowledge-base` exists, return its id; else POST create destination (workspaceId, name, destinationDefinitionId from env, configuration with pinecone_api_key, index, embedding). Return destinationId.
- [ ] `_airbyte_create_or_update_source(client_id)`: Read tenant credentials; build connectionConfiguration (client_id, client_secret, refresh_token; optional folder from `drive_folder_id`). If tenant has `airbyte_source_id`, PATCH source; else POST create source. Store and return sourceId.
- [ ] `_airbyte_create_or_update_connection(client_id, source_id, destination_id)`: Namespace = tenant `pinecone_namespace` or fallback. If tenant has `airbyte_connection_id`, PATCH connection; else POST create. Store and return connectionId.
- [ ] `ensure_airbyte_connection(client_id)`: Call get-or-create destination, create/update source, create/update connection; return `{ "airbyte_source_id", "airbyte_connection_id" }`.
- [ ] `airbyte_trigger_sync(client_id)`: Get connectionId from tenant; POST /jobs with connectionId and jobType "sync"; return job info. If no connectionId, 400 with message to complete OAuth and connect first.
- [ ] Use Airbyte API v1 semantics (paths and body keys). If your Airbyte version uses different keys (e.g. `configuration` vs `connectionConfiguration`), align the payloads so create/update succeed.

**Files:** `app/main.py`.

---

### Task 6: Airbyte HTTP endpoints

**Deliverable:** Two endpoints: connect (create/update Source + Connection), trigger-sync (create job).

**Done when:**

- [ ] `POST /airbyte/connect` body `{ "client_id": "..." }`. Calls `ensure_airbyte_connection(client_id)`, returns `{ "airbyte_source_id", "airbyte_connection_id" }`. 404 if client not found; 400 if no credentials.
- [ ] `POST /airbyte/trigger-sync` body `{ "client_id": "..." }`. Calls `airbyte_trigger_sync(client_id)`, returns job result. 404 if client not found; 400 if no Airbyte connection.

**Files:** `app/main.py`.

---

### Task 7: Doc registry (optional, UI only)

**Deliverable:** Register doc URL per client for display; no sync logic.

**Done when:**

- [ ] `POST /docs/register` body `{ "client_id", "doc_url" }`. Extract doc_id from URL; store in `tenant["registered_docs"][doc_id]` with url, and optionally last_sync_at/chunk_count (can be null). Return ok and doc_id. Do not call Google API to fetch content; do not push to Airbyte or Pinecone.

**Files:** `app/main.py`.

---

### Task 8: RAG (query + answer)

**Deliverable:** Query Pinecone by client namespace; normalize metadata; call LLM with context.

**Done when:**

- [ ] `POST /query` body `{ "client_id", "query", "top_k" }`: Resolve namespace from tenant; embed query with `OPENAI_EMBED_MODEL`; query Pinecone index with that namespace, top_k, include_metadata; normalize matches (support both `metadata.text` / `metadata.content` and any existing flow_document shape); return matches.
- [ ] `POST /rag/chat` body `{ "client_id", "question", "top_k" }`: Same retrieval; build prompt with retrieved text; call OpenAI (or configured LLM) with system "answer from context only"; return answer and optionally matches.
- [ ] `normalize_query_matches` works for the metadata shape that Airbyte’s Pinecone connector writes (e.g. `content` or `text`). If Airbyte uses different keys, add fallbacks so `text` in the normalized match is never empty when content exists.

**Files:** `app/main.py`.

---

### Task 9: UI alignment

**Deliverable:** UI has no Estuary fields; has Connect to Airbyte and Trigger sync; create client uses only name, namespace, optional folder.

**Done when:**

- [ ] Create client form: inputs for name, Pinecone namespace, optional Drive folder ID. No webhook URL or auth token fields. Submit calls `POST /clients` with `name`, `pinecone_namespace`, `drive_folder_id` (optional).
- [ ] Workspace shows "Connect to Airbyte" and "Trigger sync" buttons. Connect calls `POST /airbyte/connect`; Trigger sync calls `POST /airbyte/trigger-sync`. Status area shows result or error.
- [ ] Client list and detail show `pinecone_namespace`, and whether Airbyte is connected (e.g. presence of `airbyte_connection_id`). No display of Estuary URLs or tokens.
- [ ] RAG: input + "Ask" calls `POST /rag/chat`; answer and optional snippets displayed.

**Files:** `app/static/index.html`, `app/static/app.js`.

---

### Task 10: Health and run

**Deliverable:** Health endpoint reflects mode; app runs without Estuary.

**Done when:**

- [ ] `GET /health` returns JSON with at least `ok`, `mode: "airbyte"` (or similar), and optional openai/pinecone info. No reference to Estuary.
- [ ] `uvicorn app.main:app --reload` starts the app; no import or runtime dependency on Estuary. Root redirects to `/ui`.

**Files:** `app/main.py`.

---

## Verification (manual)

After implementation:

1. Create client A (name "Client A", namespace `client_a`). Create client B, C with namespaces `client_b`, `client_c`.
2. For client A: click "Authenticate with Google", complete OAuth. Then click "Connect to Airbyte" (or rely on auto-connect after OAuth). Verify in Airbyte UI: one Source (Google Drive), one Connection to Pinecone with namespace `client_a`.
3. In Airbyte, run a sync for that connection (or use "Trigger sync" in the app). Confirm data lands in Pinecone namespace `client_a`.
4. In the app, ask a RAG question for client A; confirm answer uses only that namespace. Repeat for B/C and confirm isolation.
5. Confirm backend never calls Google Docs/Drive API to read document body for sync — only OAuth and token storage.

---

## Env reference

```bash
GOOGLE_CLIENT_SECRETS_FILE=client_secret.json
GOOGLE_REDIRECT_URI=http://localhost:8000/oauth/callback-ui
PINECONE_API_KEY=...
PINECONE_INDEX=knowledge-base
OPENAI_API_KEY=...
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBED_MODEL=text-embedding-3-small
AIRBYTE_API_URL=https://api.airbyte.com
AIRBYTE_API_KEY=...
AIRBYTE_WORKSPACE_ID=...
# Optional: from Airbyte connector registry
AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE=...
AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE=...
```

---

## API summary

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| POST | /clients | name, pinecone_namespace, drive_folder_id? | Create client |
| GET | /clients | — | List clients |
| GET | /clients/{id} | — | Get client |
| PATCH | /clients/{id}/config | pinecone_namespace?, drive_folder_id? | Update config |
| POST | /oauth/init | client_id | Start OAuth |
| GET | /oauth/callback-ui | (query from Google) | OAuth callback |
| POST | /airbyte/connect | client_id | Create/update Source + Connection |
| POST | /airbyte/trigger-sync | client_id | Trigger sync job |
| POST | /docs/register | client_id, doc_url | Register doc (UI only) |
| POST | /query | client_id, query, top_k? | Query Pinecone |
| POST | /rag/chat | client_id, question, top_k? | RAG answer |

---

End of plan. Execute tasks 1–10 in order; verify with the "Verification" section.
