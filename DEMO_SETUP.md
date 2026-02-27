# Demo setup — step by step

Do these in order. After each step you can run the app to verify (Step 6).

---

## Step 1: Python environment and dependencies

Use **Python 3.10–3.12** (3.14 is not yet supported by pydantic). From the project root:

```bash
cd "/Users/prathameshmanjare/Documents/estuary poc"

# If you have pyenv: pyenv local 3.12.12  (then use python -m venv .venv)
# Or use Python 3.12 explicitly:
python3.12 -m venv .venv312
source .venv312/bin/activate   # Windows: .venv312\Scripts\activate
pip install -r requirements.txt
```

**Check:** `pip list` shows `fastapi`, `uvicorn`, `python-dotenv`, `openai`, `pinecone`, etc.

---

## Step 2: Create `.env` from template

```bash
cp .env.example .env
```

Edit `.env` and leave placeholders for now if you don’t have keys yet. The app **will not start** without `OPENAI_API_KEY` and `PINECONE_API_KEY` (Step 4).

---

## Step 3: Google OAuth (for “Authenticate with Google”)

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials.
2. Create OAuth 2.0 Client ID (Desktop or Web application).
3. Set authorized redirect URI: `http://localhost:8000/oauth/callback-ui`.
4. Download JSON and save as `client_secret.json` in the project root (same folder as `.env`).

**Check:** File exists: `client_secret.json`.

---

## Step 4: Pinecone and OpenAI keys in `.env`

1. [Pinecone](https://www.pinecone.io/): create an index (e.g. dimension `1536` for `text-embedding-3-small`), copy API key and index name.
2. [OpenAI](https://platform.openai.com/): copy API key.

In `.env` set:

- `PINECONE_API_KEY=<your-key>`
- `PINECONE_INDEX=<your-index-name>`
- `OPENAI_API_KEY=<your-key>`

**Check:** No quotes needed; no spaces around `=`.

---

## Step 5 (optional): Airbyte for sync

To use “Connect to Airbyte” and “Trigger sync”:

1. Use [Airbyte Cloud](https://cloud.airbyte.com/) or self-hosted with API.
2. Get API key and workspace ID.
3. In `.env` set: `AIRBYTE_API_URL`, `AIRBYTE_API_KEY`, `AIRBYTE_WORKSPACE_ID`.

If these are missing, the app still runs; Airbyte buttons will return 503.

---

## Step 6: Run the app

From the project root, with the venv you used in Step 1 (e.g. `.venv312`):

```bash
cd "/Users/prathameshmanjare/Documents/estuary poc"
source .venv312/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Check:**

- Terminal: `Uvicorn running on http://0.0.0.0:8000`
- Browser: open `http://localhost:8000` → redirects to `http://localhost:8000/ui/`
- You see: “Knowledge Base” sidebar, “Create Client”, and steps 1–4 (OAuth, Paste link, Airbyte, RAG)

If the app exits with **`OPENAI_API_KEY is required`** or **`PINECONE_API_KEY`** / **`Invalid API Key`**: complete Step 4 and put real keys in `.env`.

---

## Quick demo flow (after app is running)

1. Create client: name e.g. “Demo”, namespace e.g. `client-a` → Create Client.
2. Select the client → **1. OAuth** → Authenticate with Google (complete consent in popup).
3. **2. Paste link** → paste a Drive **folder** URL → Add (so Airbyte knows what to sync). Optionally add a Doc URL → Add.
4. **3. Airbyte** → Connect to Airbyte → Trigger Sync (requires Step 5).
5. **4. RAG** → type a question → Ask (uses Pinecone namespace for selected client).

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `python3` is 3.14 and `pip install` fails on pydantic-core | Use Python 3.12: `~/.pyenv/versions/3.12.12/bin/python -m venv .venv312` then `source .venv312/bin/activate` |
| `Client.__init__() got an unexpected keyword argument 'proxies'` | Already handled: `requirements.txt` pins `httpx<0.28`. If you see this, run `pip install 'httpx<0.28'` |
| App exits with `Invalid API Key` (Pinecone) or OpenAI error | Put valid `PINECONE_API_KEY` and `OPENAI_API_KEY` in `.env` (Step 4) |
