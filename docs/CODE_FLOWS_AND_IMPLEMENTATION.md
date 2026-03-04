# Code Implementation & Flows — How It Works

This document describes how the application is implemented: main functions, flows, conditions, and end-to-end behavior.

---

## 1. Overview

- **Stack:** FastAPI backend, in-memory tenant store, Google OAuth, PyAirbyte (Google Drive connector in Docker), OpenAI embeddings + chat, Pinecone vector store.
- **Entry:** `app/main.py` — single FastAPI app; UI served from `app/static/` at `/ui`.
- **State:** All tenant data lives in `TENANT_STORE` (dict keyed by `client_id`). OAuth state is in `OAUTH_STATE_TO_CLIENT`. No database; restart clears everything except `tokens.json` (debug file).

---

## 2. Global State & Config

| Item | Type | Purpose |
|------|------|--------|
| `TENANT_STORE` | `Dict[str, dict]` | Per-client data: `name`, `pinecone_namespace`, `drive_folder_id`, `credentials`, `registered_docs`, `airbyte_*`, `last_sync_at`, etc. |
| `OAUTH_STATE_TO_CLIENT` | `Dict[str, str]` | Maps OAuth `state` → `client_id` for callback. Cleared on restart. |
| `_AIRBYTE_TOKEN_CACHE` | `Dict` | Cached Airbyte Cloud Bearer token and `expires_at`. |
| Env (main) | `.env` | `GOOGLE_*`, `PINECONE_*`, `OPENAI_*`, `AIRBYTE_*`, `OPENAI_EMBED_DIMENSIONS` (e.g. 1024). |

---

## 3. Main Functions (by Area)

### 3.1 Auth & Credentials

| Function | What it does |
|----------|----------------|
| `credentials_from_store(client_id)` | Loads tenant credentials, builds Google `Credentials`. If expired, refreshes via `refresh_token` and updates `TENANT_STORE["credentials"]`. Raises 404 if no credentials. |
| `parse_state_from_redirect_url(url)` | Parses `state` query param from OAuth redirect URL. |
| `extract_folder_id(url)` | Regex extract of folder ID from `.../folders/ID`. |
| `extract_doc_id(url)` | Regex extract of document ID from `.../document/d/ID`. |
| `is_folder_url(url)` / `is_doc_url(url)` | Detect Drive folder vs Doc URL. |

### 3.2 Airbyte (Token & API)

| Function | What it does |
|----------|----------------|
| `_airbyte_bearer_token()` | Returns Bearer: uses `AIRBYTE_API_KEY` if set, else exchanges `AIRBYTE_CLIENT_ID` + `AIRBYTE_CLIENT_SECRET` with `grant-type: client_credentials`, caches token using `expires_in`. |
| `_airbyte_configured()` | True if `AIRBYTE_WORKSPACE_ID` and (API key or client credentials) are set. |
| `_airbyte_request(method, path, json_body)` | Calls `AIRBYTE_API_URL/v1{path}` with Bearer; on 4xx/5xx parses error and raises `HTTPException`. |
| `_airbyte_request_raw(...)` | Same call but returns `(status_code, response_text)` for debugging (no raise). |
| `_airbyte_pinecone_destination_config()` | Builds Pinecone destination config dict for Airbyte API (embedding, indexing, processing). |
| `_airbyte_get_or_create_destination()` | GET destinations by workspace; if "pinecone-knowledge-base" exists return its id, else POST create one. |

### 3.3 Sync (PyAirbyte + Pinecone)

| Function | What it does |
|----------|----------------|
| `_chunk_text(text, size, overlap)` | Splits text into overlapping chunks (`CHUNK_SIZE`, `CHUNK_OVERLAP`). |
| `ensure_airbyte_connection(client_id)` | **Does not** create Airbyte Cloud sources. Validates: tenant has credentials and `drive_folder_id`; calls Google token refresh. Updates tenant `credentials["token"]`, sets `pyairbyte_ready`, returns `{status, folder_id, mode}`. |
| `airbyte_trigger_sync(client_id)` | Runs sync in-process: builds PyAirbyte source (Google Drive via Docker), `source.read(force_full_refresh=True)`, reads "documents" into DuckDB cache → DataFrame. For each row: chunk content, OpenAI embed (with `OPENAI_EMBED_DIMENSIONS`), build vectors with `flow_document` metadata. Upserts to Pinecone in batches of 100; sets `tenant["last_sync_at"]`. Returns doc count, vector count, namespace. |

### 3.4 RAG & Query

| Function | What it does |
|----------|----------------|
| `parse_flow_document(metadata)` | Gets `flow_document` from Pinecone metadata; if string, JSON-decode; else return dict or `{}`. |
| `normalize_query_matches(raw_response)` | Takes Pinecone query response; normalizes each match to `{id, score, doc_id, chunk_index, text}` using `flow_document.chunk_text` or metadata fallbacks. |
| `retrieve_matches(client_id, query, top_k)` | Loads tenant namespace; embeds `query` with OpenAI; queries Pinecone by vector; returns normalized matches. |
| `answer_with_rag(question, matches)` | If no matches, returns a fixed “no context” message. Else builds context from matches, prompts LLM (OpenAI `responses.create`) to answer from context only, returns trimmed reply. |

### 3.5 Helpers

| Function | What it does |
|----------|----------------|
| `now_iso()` | Current UTC time as ISO string. |
| `_oauth_error_html(title, err_msg, status, tb)` | Returns HTML error page with checks for redirect URI, client_secret, etc. |

---

## 4. API Endpoints Summary

| Method | Path | Purpose |
|--------|------|--------|
| GET | `/` | Redirect to `/ui`. |
| GET | `/health` | Health + model/index info. |
| POST | `/clients` | Create client; body: `name`, `pinecone_namespace`, optional `drive_folder_id`. Returns `client_id`, `name`. |
| GET | `/clients` | List clients (id, name, doc_count, has_auth, namespace). |
| GET | `/clients/{client_id}` | Get one client + registered_docs, namespace, drive_folder_id, airbyte_connection_id. |
| PATCH | `/clients/{client_id}/config` | Update `pinecone_namespace` and/or `drive_folder_id`. |
| POST | `/oauth/init` | Body: `client_id`. Returns Google auth URL + state. Stores state in tenant and `OAUTH_STATE_TO_CLIENT`. |
| POST | `/oauth/callback` | Body: `client_id`, `authorization_response_url`. Exchanges code for tokens, stores in tenant. Optionally calls `ensure_airbyte_connection`. |
| GET | `/oauth/callback-ui` | **Browser redirect target.** Reads `state` from URL, looks up client_id, completes OAuth, stores credentials, writes `tokens.json`, then HTML with postMessage to opener or redirect to `/ui`. |
| POST | `/docs/register` | Body: `client_id`, `doc_url`. Extracts doc_id, adds to `registered_docs`. |
| POST | `/links/add` | Body: `client_id`, `url`. If folder URL → set `drive_folder_id`. If doc URL → add to `registered_docs`. |
| POST | `/airbyte/connect` | Body: `client_id`. Calls `ensure_airbyte_connection` (validate creds + folder). |
| POST | `/airbyte/trigger-sync` | Body: `client_id`. Calls `airbyte_trigger_sync` (PyAirbyte + embed + Pinecone). |
| DELETE | `/airbyte/cleanup-sources` | Deletes all sources in Airbyte workspace (debug/cleanup). |
| POST | `/airbyte/debug-source` | Tries source config variants (minimal/csv/unstructured), returns raw Airbyte responses. |
| POST | `/query` | Body: `client_id`, `query`, `top_k`. Returns Pinecone matches (normalized). |
| POST | `/rag/chat` | Body: `client_id`, `question`, `top_k`. Retrieves matches, then answers with RAG. |

---

## 5. End-to-End Flows

### 5.1 Flow: Create Client → OAuth → Connect → Sync

```
1. Create client
   POST /clients { name, pinecone_namespace, drive_folder_id? }
   → client_id created, TENANT_STORE[client_id] initialized.
   Conditions: none.

2. (Optional) Set Drive folder if not provided at creation
   POST /links/add { client_id, url }  with url = Drive folder link
   → tenant["drive_folder_id"] = extracted folder_id.
   Conditions: url must contain "/folders/"; else 400.

3. Start OAuth
   POST /oauth/init { client_id }
   → Returns authorization_url, state.
   Conditions: client_id must exist (404 else).
   Side effect: TENANT_STORE[client_id]["oauth_state"] = state; OAUTH_STATE_TO_CLIENT[state] = client_id.

4. User visits authorization_url in browser, signs in with Google, is redirected to
   GET /oauth/callback-ui?state=...&code=...
   → State validated; code exchanged for tokens; credentials stored in tenant; tokens.json written; if _airbyte_configured(), ensure_airbyte_connection(client_id) called (may fail silently).
   Conditions: state must be in OAUTH_STATE_TO_CLIENT; client must exist; client_secret.json and redirect URI must be correct.

5. Connect (validate ready for sync)
   POST /airbyte/connect { client_id }
   → ensure_airbyte_connection(client_id): checks credentials + drive_folder_id, refreshes Google token.
   Conditions: tenant must have "credentials" (400 else); must have drive_folder_id (400 else); Google token refresh must succeed (400 else).

6. Trigger sync
   POST /airbyte/trigger-sync { client_id }
   → airbyte_trigger_sync: PyAirbyte reads Drive folder (Docker), chunks, embeds, upserts to Pinecone.
   Conditions: tenant exists (404); has credentials (400); has drive_folder_id (400); has pinecone_namespace (400). Requires Docker and PyAirbyte.
```

### 5.2 Flow: Query / RAG

```
1. Query (vector search only)
   POST /query { client_id, query, top_k }
   → retrieve_matches: get namespace from tenant, embed query, Pinecone query, normalize matches.
   Conditions: client exists (404); tenant has pinecone_namespace (400).

2. RAG chat (search + LLM answer)
   POST /rag/chat { client_id, question, top_k }
   → retrieve_matches then answer_with_rag(question, matches).
   Conditions: same as /query. If no matches, answer is "No relevant context found...".
```

### 5.3 Flow: Add Doc or Folder Link

```
POST /links/add { client_id, url }
- If url contains "/folders/" → extract_folder_id, set tenant["drive_folder_id"], return { type: "folder", folder_id }.
- If url contains "/document/d/" → extract_doc_id, add to registered_docs, return { type: "doc", doc_id }.
- Else → 400 "Provide a Google Drive folder link or Doc link".
Conditions: client exists (404); url non-empty (400).
```

### 5.4 Conditions Summary

| Step | Failure condition | HTTP / behavior |
|------|-------------------|------------------|
| Any endpoint using `client_id` | Client not in TENANT_STORE | 404 |
| OAuth callback UI | state missing or not in OAUTH_STATE_TO_CLIENT | 400 HTML |
| OAuth callback UI | Token exchange fails (wrong redirect, secret, etc.) | 500 HTML |
| ensure_airbyte_connection | No credentials in tenant | 400 |
| ensure_airbyte_connection | No drive_folder_id | 400 |
| ensure_airbyte_connection | Google token refresh fails | 400 |
| airbyte_trigger_sync | No credentials | 400 |
| airbyte_trigger_sync | No drive_folder_id | 400 |
| airbyte_trigger_sync | No pinecone_namespace | 400 |
| retrieve_matches / RAG | No pinecone_namespace | 400 |
| Airbyte API calls | Missing workspace or token | 503 |
| Airbyte API calls | API returns 4xx/5xx | Same status or 502, detail from response |

---

## 6. Data Flow (Sync Path)

```
User clicks "Trigger sync" (or POST /airbyte/trigger-sync)
  → airbyte_trigger_sync(client_id)
  → credentials_from_store(client_id)  [refresh token if expired]
  → ab.get_source("source-google-drive", docker_image=..., config={ folder_url, credentials, streams })
  → source.read(cache, force_full_refresh=True)   [Docker container runs, reads Drive]
  → cache["documents"].to_pandas()  → DataFrame with document_key, content, ...
  → For each row: _chunk_text(content) → chunks
  → For each chunk: openai_client.embeddings.create(..., dimensions=OPENAI_EMBED_DIMENSIONS)
  → Build vector list: id = client_id[:8]-doc_key-i, metadata.flow_document = { doc_id, chunk_index, chunk_text }
  → index.upsert(vectors, namespace=tenant["pinecone_namespace"]) in batches of 100
  → tenant["last_sync_at"] = now_iso()
  → Return { status, documents_synced, vectors_upserted, docs, namespace }
```

---

## 7. Data Flow (Query / RAG)

```
POST /query or /rag/chat
  → retrieve_matches(client_id, query/question, top_k)
  → namespace = tenant["pinecone_namespace"]
  → openai_client.embeddings.create(query, dimensions=OPENAI_EMBED_DIMENSIONS)
  → index.query(namespace=namespace, vector=..., top_k=top_k, include_metadata=True)
  → normalize_query_matches(response)  → list of { id, score, doc_id, chunk_index, text }
  → For /query: return { client_id, matches }
  → For /rag/chat: answer_with_rag(question, matches)
    → Build context from matches; OpenAI responses.create with system "Answer from context only"; return answer + matches
```

---

## 8. Where Things Live

| Concern | Location |
|--------|----------|
| Tenant data | `TENANT_STORE` in memory (no DB). |
| Google credentials | `TENANT_STORE[client_id]["credentials"]` (+ `tokens.json` for debug). |
| Drive folder for sync | `TENANT_STORE[client_id]["drive_folder_id"]`. |
| Pinecone namespace | `TENANT_STORE[client_id]["pinecone_namespace"]`. |
| Vector metadata | Pinecone; each vector has `metadata.flow_document` (JSON: doc_id, chunk_index, chunk_text). |
| Sync trigger | Manual only: UI or POST `/airbyte/trigger-sync`. No cron/scheduler in app. |

---

## 9. UI Entry Points (app/static)

- **`/ui`** — Static app (e.g. `index.html`). Typically: create client, paste folder link, “Authenticate with Google” (opens `/oauth/init` then redirect to Google, then `/oauth/callback-ui`), “Connect” (calls `/airbyte/connect`), “Trigger sync” (calls `/airbyte/trigger-sync`), and RAG/query.
- OAuth success: callback-ui HTML posts a message to opener with `oauth-success` and `clientId`, then closes; the opener (UI) can refresh client state.

This document reflects the implementation in `app/main.py` and the flows described above; env and deployment details are in the README and `AIRBYTE_TROUBLESHOOTING_AND_PRODUCTION.md`.
