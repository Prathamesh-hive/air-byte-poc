# Multi-Client Knowledge Base: Google Docs → Airbyte → Pinecone

Multi-tenant knowledge base: clients authenticate with Google (OAuth), you register their docs or Drive folder; **Airbyte** fetches updates, chunking, and embeddings, and writes to **Pinecone** (one namespace per client). Your app does OAuth, client/doc registry, and RAG query only.

**Sync and doc fetching are done by Airbyte; this app does not fetch Google Docs or run connector logic.** Auth is stored in memory (POC; resets on restart).

## Architecture

- **Your app**: Google OAuth (per client), client + doc link registry, Airbyte API (create source/connection, trigger sync), RAG (query Pinecone by namespace + LLM).
- **Airbyte**: Google Drive source (per client, using tokens you pass), scheduled or on-demand sync, chunking + embedding (OpenAI/Cohere), Pinecone destination with namespace per connection.
- **Pinecone**: One index; namespaces `client-a`, `client-b`, etc. Query uses active client’s namespace.

Clients authenticate **with you** (your Google OAuth app). You store tokens and call Airbyte’s API to create/update the source and connection; clients never see Airbyte.

### Is “we hold tokens, Airbyte uses them” supported?

Yes. This is a supported pattern:

- **Airbyte Google Drive source** ([docs](https://docs.airbyte.com/integrations/sources/google-drive)): For OAuth, you can “enter your Google application’s **client ID, client secret, and refresh token**” in the connector. So the connector accepts credentials you provide (including a per-user refresh token).
- **Your app** runs the OAuth flow (your consent screen), stores each client’s `refresh_token`, and when creating a source via the Airbyte API you pass `connectionConfiguration.credentials` with your app’s `client_id`, `client_secret`, and that client’s `refresh_token`. Airbyte stores this config and uses it to obtain access tokens and call Google Drive on behalf of that user.
- **Per-client sources**: Each Airbyte source has its own configuration; so one source per client, each with that client’s credentials. Airbyte then fetches from each client’s Drive and writes to your Pinecone (e.g. via one shared Pinecone destination, with namespace per connection).

So: you hold all authentication tokens for your clients; you act as the Airbyte API client; you pass each client’s credentials into Airbyte when creating/updating that client’s source; Airbyte performs the actual sync and populates your Pinecone. No “bring your own” limitation—this is the standard way to use OAuth sources when the OAuth flow is done in your app.

## End-to-end flow (UI)

1. **Create client** — name + Pinecone namespace (e.g. `client-a`).
2. **OAuth** — Authenticate with Google (Drive read). Client never sees Airbyte.
3. **Paste link** — Add a **Drive folder** URL (for Airbyte to sync) and/or **Doc** URLs (for registry). Use “Add” for each.
4. **Connect to Airbyte** — Creates/updates source + connection (folder → Pinecone namespace).
5. **Trigger sync** — Airbyte syncs folder → chunking + embedding → Pinecone.
6. **RAG** — Ask a question; retrieval uses the active client’s namespace.

## Requirements

- Python 3.10+
- Google OAuth client (`client_secret.json`) — your app, your consent screen
- Pinecone index
- OpenAI API key (for RAG query embedding; Airbyte can use same or Cohere for sync)
- **Airbyte** Cloud or self-hosted with API access

## Where to put credentials (TODO)

See **[CREDENTIALS_TODO.md](CREDENTIALS_TODO.md)** for a checklist: copy `.env.example` to `.env`, fill in Pinecone/OpenAI/Airbyte keys, and add Google OAuth `client_secret.json`. Do not commit `.env` or `client_secret.json`.

## Environment

Create `.env` (see `.env.example`):

```bash
# Google OAuth (your app)
GOOGLE_CLIENT_SECRETS_FILE=client_secret.json
GOOGLE_REDIRECT_URI=http://localhost:8000/oauth/callback-ui

# Pinecone (same index for all clients; namespace per client)
PINECONE_API_KEY=...
PINECONE_INDEX=knowledge-base

# OpenAI (RAG and, if used, same as Airbyte embedding)
OPENAI_API_KEY=...
OPENAI_CHAT_MODEL=gpt-4o-mini
OPENAI_EMBED_MODEL=text-embedding-3-small

# Airbyte (optional; if set, OAuth callback will create/update source + connection)
AIRBYTE_API_URL=https://api.airbyte.com
AIRBYTE_API_KEY=...
AIRBYTE_WORKSPACE_ID=...
# Optional: override definition IDs from Airbyte registry
# AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE=...
# AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE=...
```

If `AIRBYTE_API_KEY` and `AIRBYTE_WORKSPACE_ID` are not set, the app still runs; “Connect to Airbyte” and “Trigger sync” will return 503.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open UI: [http://localhost:8000/ui](http://localhost:8000/ui)

## Flow

1. **Create client**: name + Pinecone namespace (e.g. `client-a`). Optionally set Drive folder ID (Airbyte will sync that folder).
2. **OAuth**: Client clicks “Authenticate with Google” → your consent screen → you store tokens. If Airbyte is configured, the app will create/update the Airbyte source and connection for this client.
3. **Connect to Airbyte** (if not done after OAuth): Creates or updates the Google Drive source and Connection to Pinecone with this client’s namespace.
4. **Trigger sync**: Calls Airbyte to run a sync for this client’s connection (Airbyte pulls from Drive, chunking/embedding, writes to Pinecone).
5. **Register doc**: Optional; store doc links for your UI. Airbyte sync is driven by the source config (e.g. folder), not by this list.
6. **Ask**: RAG over the client’s Pinecone namespace.

## API Endpoints

- `POST /clients` — create client (name, pinecone_namespace, optional drive_folder_id)
- `GET /clients`, `GET /clients/{id}`, `PATCH /clients/{id}/config`
- `POST /oauth/init`, `GET /oauth/callback-ui`
- `POST /airbyte/connect` — create/update Airbyte source + connection for client
- `POST /airbyte/trigger-sync` — trigger sync for client’s connection
- `POST /docs/register`
- `POST /query`, `POST /rag/chat`

## Airbyte setup

1. In Airbyte, create a **Pinecone destination** (or let the app create one via API with `AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE`). Use the same index and embedding model as your app’s RAG (e.g. `text-embedding-3-small`).
2. The app creates a **Google Drive source** per client (using stored OAuth) and a **Connection** from that source to the Pinecone destination with the client’s namespace.
3. Sync schedule is set in Airbyte; you can also trigger sync via `POST /airbyte/trigger-sync`.

## Demo checklist (verify POC is demoable)

Before demoing:

1. **Credentials:** Complete [CREDENTIALS_TODO.md](CREDENTIALS_TODO.md) — `.env` and `client_secret.json` in place.
2. **Run:** `uvicorn app.main:app --reload`; open [http://localhost:8000](http://localhost:8000) (redirects to `/ui`).
3. **Health:** [http://localhost:8000/health](http://localhost:8000/health) returns `{"ok": true, "mode": "airbyte", ...}`.
4. **Create clients:** In UI, create Client A (namespace `client_a`), Client B (`client_b`), Client C (`client_c`).
5. **OAuth:** Select Client A → “Authenticate with Google” → complete consent; badge shows “Authenticated”.
6. **Airbyte:** Click “Connect to Airbyte” (or rely on auto-connect after OAuth if env is set). Then “Trigger sync”. In Airbyte UI, confirm Source + Connection exist and sync runs.
7. **RAG:** Select a client that has synced data → type a question → “Ask”. Answer and snippets appear; switching client changes namespace (isolation).
8. **Without Airbyte:** If `AIRBYTE_API_KEY` / `AIRBYTE_WORKSPACE_ID` are empty, app still runs; Create client, OAuth, Register doc, and RAG (over existing Pinecone data) work; “Connect to Airbyte” and “Trigger sync” return 503.

## POC scope

- In-memory tenant/credentials/doc state (resets on restart).
- No security hardening. Embedding model and Pinecone metadata shape should match what Airbyte’s Pinecone connector writes for RAG to work.
