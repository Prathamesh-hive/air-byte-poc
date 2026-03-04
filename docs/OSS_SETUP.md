# Airbyte OSS setup (step by step)

Run these in order. Allow **10–15+ minutes** for the first install (Docker images + Helm).

---

## Step 1: Prerequisites

- **Docker Desktop** installed and running.
- **Port 8000 free** (stop any app using it, e.g. your POC backend).

---

## Step 2: Install abctl

```bash
# Homebrew (recommended)
brew tap airbytehq/tap
brew install abctl

# Or official script
curl -LsfS https://get.airbyte.com | bash -
```

Verify: `abctl version`

---

## Step 3: Install and start Airbyte OSS

**Ensure nothing is using port 8000** (e.g. run your POC on 8001 later).

```bash
abctl local install
```

- First run: creates a Kind cluster, pulls images (~5–10 min), installs Helm chart (~2–5 min).
- When it finishes, the Airbyte UI is at **http://localhost:8000**.

---

## Step 4: Get API credentials (OSS)

1. Open **http://localhost:8000** in a browser.
2. Sign up / log in (local OSS may allow any email or have a default).
3. Go to **User Settings** (profile or gear) → **Applications** → **Create an application**.
4. Copy the **Client ID** and **Client Secret**.

---

## Step 5: Get workspace ID

With the token from the application:

```bash
# Get a token first (replace CLIENT_ID and CLIENT_SECRET)
curl -s -X POST http://localhost:8000/api/v1/applications/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"YOUR_CLIENT_ID","client_secret":"YOUR_CLIENT_SECRET","grant_type":"client_credentials"}' \
  | jq -r '.access_token'

# Then list workspaces (use the token from above)
curl -s http://localhost:8000/api/public/v1/workspaces \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  | jq '.data[0].workspaceId'
```

Or read the workspace ID from the Airbyte UI (e.g. URL or workspace settings).

---

## Step 6: Configure .env for OSS

Add or set in `.env`:

```env
# Airbyte OSS (local)
AIRBYTE_API_URL=http://localhost:8000
AIRBYTE_API_PATH_AUTH=/api/v1
AIRBYTE_API_PATH_PUBLIC=/api/public/v1
AIRBYTE_USE_API=1
AIRBYTE_WORKSPACE_ID=<workspace_id_from_step_5>
AIRBYTE_CLIENT_ID=<from_step_4>
AIRBYTE_CLIENT_SECRET=<from_step_4>

# POC runs on 8001 when OSS uses 8000
GOOGLE_REDIRECT_URI=http://localhost:8001/oauth/callback-ui
```

Optional: `OPENAI_EMBED_DIMENSIONS=1024` to match your Pinecone index.

---

## Step 7: Run the POC backend on 8001

```bash
cd "/Users/prathameshmanjare/Documents/estuary poc"
uvicorn app.main:app --port 8001
```

- **Airbyte UI:** http://localhost:8000  
- **POC API:** http://localhost:8001  

---

## Step 8: Verify

- **Health:** `curl http://localhost:8001/health`  
  - Should include `"airbyte_use_api": true` and `"airbyte_ui_url": "http://localhost:8000"`.
- **Connect:** Create a client, complete OAuth, set `drive_folder_id`, then `POST /airbyte/connect` (creates default Drive integration in Airbyte).
- **Sync:** `POST /airbyte/trigger-sync` triggers jobs via Airbyte API (no PyAirbyte).

---

## Useful abctl commands

| Command | Description |
|--------|-------------|
| `abctl local install` | Install/start Airbyte (idempotent) |
| `abctl local start` | Start existing install |
| `abctl local stop` | Stop Airbyte |
| `abctl local uninstall` | Remove cluster and data |
| `abctl local credentials` | Print credentials / URL hints |

---

## If port 8000 was in use

- Stop the process on 8000 (e.g. `lsof -i :8000` then `kill <PID>`).
- Run `abctl local install` again.
- Start your POC on 8001.
