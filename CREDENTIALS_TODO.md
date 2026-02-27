# TODO: Where to put your credentials (POC)

**No database:** Auth and client state are stored in memory and reset on restart.

## 1. Copy env template

```bash
cp .env.example .env
```

Edit `.env` and fill in the values below.

## 2. Values to fill in `.env`

| Variable | Where to get it |
|----------|-----------------|
| `GOOGLE_CLIENT_SECRETS_FILE` | Keep as `client_secret.json` (or path to your file). |
| `GOOGLE_REDIRECT_URI` | Use `http://localhost:8000/oauth/callback-ui` for local dev. |
| `PINECONE_API_KEY` | [Pinecone](https://www.pinecone.io/) → API Keys. |
| `PINECONE_INDEX` | Pinecone index name (e.g. `knowledge-base`). |
| `OPENAI_API_KEY` | [OpenAI](https://platform.openai.com/) → API keys. |
| `OPENAI_CHAT_MODEL` | e.g. `gpt-4o-mini`. |
| `OPENAI_EMBED_MODEL` | Must match Airbyte Pinecone connector (e.g. `text-embedding-3-small`). |
| `AIRBYTE_API_URL` | Cloud: `https://api.airbyte.com`; self-hosted: your base URL. |
| `AIRBYTE_API_KEY` | Airbyte workspace → Settings → API key. |
| `AIRBYTE_WORKSPACE_ID` | From Airbyte UI URL or workspace settings. |
| `AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE` | Optional; from Airbyte connector registry. |
| `AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE` | Optional; from Airbyte connector registry. |

## 3. Google OAuth client secret

- Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials.
- Create OAuth 2.0 Client ID (Web application).
- Add redirect URI: `http://localhost:8000/oauth/callback-ui`.
- Download JSON and save as **`client_secret.json`** in the project root (or set `GOOGLE_CLIENT_SECRETS_FILE` in `.env` to its path).

Do not commit `.env` or `client_secret.json` (they are in `.gitignore`).

---

## Verify before demo

From project root:

```bash
source .venv/bin/activate   # or create venv first
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- Open [http://localhost:8000/health](http://localhost:8000/health) — expect `{"ok":true,"mode":"airbyte",...}`.
- Open [http://localhost:8000/ui](http://localhost:8000/ui) — UI loads; create a client (name + namespace); list shows the client.
- OAuth and Airbyte need `client_secret.json` and (for Airbyte) `AIRBYTE_API_KEY` + `AIRBYTE_WORKSPACE_ID` in `.env`.
