import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from openai import OpenAI
from pinecone import Pinecone
from pydantic import BaseModel

load_dotenv()

TENANT_STORE: Dict[str, dict] = {}
OAUTH_STATE_TO_CLIENT: Dict[str, str] = {}


class ClientCreateRequest(BaseModel):
    name: str
    pinecone_namespace: str
    drive_folder_id: Optional[str] = None  # optional; Airbyte Google Drive folder to sync


class ClientUpdateConfigRequest(BaseModel):
    pinecone_namespace: Optional[str] = None
    drive_folder_id: Optional[str] = None


class OAuthInitRequest(BaseModel):
    client_id: str


class OAuthCallbackRequest(BaseModel):
    client_id: str
    authorization_response_url: str


class RegisterDocRequest(BaseModel):
    client_id: str
    doc_url: str


class AddLinkRequest(BaseModel):
    """Paste Drive folder link (for Airbyte sync) or Doc link (for registry)."""
    client_id: str
    url: str


class SyncDocRequest(BaseModel):
    client_id: str
    doc_id: str


class SyncAllRequest(BaseModel):
    client_id: str


class QueryRequest(BaseModel):
    client_id: str
    query: str
    top_k: int = 5


class RagChatRequest(BaseModel):
    client_id: str
    question: str
    top_k: int = 5


GOOGLE_CLIENT_SECRETS_FILE = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth/callback-ui")
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/drive.readonly",
]

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "knowledge-base")

AIRBYTE_API_URL = os.getenv("AIRBYTE_API_URL", "http://localhost:8000").rstrip("/")
AIRBYTE_API_KEY = os.getenv("AIRBYTE_API_KEY", "")
AIRBYTE_WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

app = FastAPI(title="Multi-Tenant Google Docs Knowledge Base (Airbyte + Pinecone)")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

if not PINECONE_API_KEY:
    raise RuntimeError("PINECONE_API_KEY is required")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
pc = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index(PINECONE_INDEX)

# Airbyte: optional. If set, we create/update source+connection on OAuth and expose trigger-sync.
AIRBYTE_HEADERS = {}
if AIRBYTE_API_KEY:
    AIRBYTE_HEADERS["Authorization"] = f"Bearer {AIRBYTE_API_KEY}"
    AIRBYTE_HEADERS["Content-Type"] = "application/json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_doc_id(doc_url: str) -> str:
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", doc_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Google Doc URL")
    return match.group(1)


def extract_folder_id(url: str) -> str:
    """Extract Drive folder ID from URLs like drive.google.com/.../folders/ID."""
    match = re.search(r"/folders/([a-zA-Z0-9-_]+)", url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Google Drive folder URL")
    return match.group(1)


def is_doc_url(url: str) -> bool:
    return "/document/d/" in url


def is_folder_url(url: str) -> bool:
    return "/folders/" in url


def credentials_from_store(client_id: str) -> Credentials:
    tenant = TENANT_STORE.get(client_id)
    if not tenant or "credentials" not in tenant:
        raise HTTPException(status_code=404, detail="Client credentials not found")

    creds = Credentials.from_authorized_user_info(tenant["credentials"], GOOGLE_SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        tenant["credentials"] = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }

    return creds


def parse_state_from_redirect_url(authorization_response_url: str) -> Optional[str]:
    parsed = urlparse(authorization_response_url)
    params = parse_qs(parsed.query)
    state_values = params.get("state", [])
    return state_values[0] if state_values else None


def _airbyte_request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    if not AIRBYTE_API_KEY or not AIRBYTE_WORKSPACE_ID:
        raise HTTPException(status_code=503, detail="Airbyte not configured (AIRBYTE_API_KEY, AIRBYTE_WORKSPACE_ID)")
    url = f"{AIRBYTE_API_URL}/v1{path}"
    r = requests.request(method, url, headers=AIRBYTE_HEADERS, json=json_body, timeout=30)
    if r.status_code >= 400:
        raise HTTPException(status_code=min(r.status_code, 502), detail=r.text[:500])
    return r.json() if r.text else {}


def _airbyte_get_or_create_destination() -> str:
    """Return existing Pinecone destination id or create one."""
    list_res = _airbyte_request("GET", f"/destinations?workspaceIds={AIRBYTE_WORKSPACE_ID}")
    for d in list_res.get("destinations", []):
        if d.get("name") == "pinecone-knowledge-base":
            return d["destinationId"]
    create = _airbyte_request("POST", "/destinations", {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": "pinecone-knowledge-base",
        "destinationDefinitionId": os.getenv("AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE", "00000000-0000-0000-0000-000000000000"),
        "configuration": {
            "pinecone_api_key": PINECONE_API_KEY,
            "index": PINECONE_INDEX,
            "embedding": {"mode": "openai", "openai_key": OPENAI_API_KEY},
        },
    })
    return create["destinationId"]


def _airbyte_create_or_update_source(client_id: str) -> str:
    tenant = TENANT_STORE.get(client_id)
    if not tenant or "credentials" not in tenant:
        raise HTTPException(status_code=400, detail="Client has no Google credentials")
    credentials_from_store(client_id)
    c = tenant["credentials"]
    name = f"google-drive-{tenant.get('name', client_id)}".replace(" ", "-")[:64]
    config = {
        "credentials": {
            "auth_type": "Client",
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "refresh_token": c["refresh_token"],
        },
        "folder_url": f"https://drive.google.com/drive/folders/{tenant.get('drive_folder_id') or 'root'}" if tenant.get("drive_folder_id") else None,
    }
    source_id = tenant.get("airbyte_source_id")
    if source_id:
        _airbyte_request("PATCH", f"/sources/{source_id}", {"connectionConfiguration": config, "name": name})
        return source_id
    res = _airbyte_request("POST", "/sources", {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": name,
        "sourceDefinitionId": os.getenv("AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE", "30f72781-4b8d-4e43-b8e2-1b4c2a3d5e6f"),
        "connectionConfiguration": config,
    })
    sid = res["sourceId"]
    tenant["airbyte_source_id"] = sid
    return sid


def _airbyte_create_or_update_connection(client_id: str, source_id: str, destination_id: str) -> str:
    tenant = TENANT_STORE.get(client_id)
    namespace = (tenant or {}).get("pinecone_namespace") or f"client-{client_id[:8]}"
    conn_id = (tenant or {}).get("airbyte_connection_id")
    payload = {
        "sourceId": source_id,
        "destinationId": destination_id,
        "namespaceDefinition": "customformat",
        "namespaceFormat": namespace,
        "syncCatalog": {"streams": []},
    }
    if conn_id:
        _airbyte_request("PATCH", f"/connections/{conn_id}", payload)
        return conn_id
    payload["name"] = f"conn-{client_id[:8]}"
    res = _airbyte_request("POST", "/connections", payload)
    cid = res["connectionId"]
    if tenant:
        tenant["airbyte_connection_id"] = cid
    return cid


def ensure_airbyte_connection(client_id: str) -> dict:
    """After OAuth: create/update Airbyte source and connection. Returns {source_id, connection_id} or raises."""
    dest_id = _airbyte_get_or_create_destination()
    source_id = _airbyte_create_or_update_source(client_id)
    conn_id = _airbyte_create_or_update_connection(client_id, source_id, dest_id)
    return {"airbyte_source_id": source_id, "airbyte_connection_id": conn_id}


def airbyte_trigger_sync(client_id: str) -> dict:
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")
    conn_id = tenant.get("airbyte_connection_id")
    if not conn_id:
        raise HTTPException(status_code=400, detail="No Airbyte connection. Complete OAuth and connect first.")
    res = _airbyte_request("POST", "/jobs", {"connectionId": conn_id, "jobType": "sync"})
    return {"job_id": res.get("job", {}).get("id"), "connection_id": conn_id}


def parse_flow_document(metadata: dict) -> dict:
    flow_document = metadata.get("flow_document")
    if isinstance(flow_document, str):
        try:
            return json.loads(flow_document)
        except json.JSONDecodeError:
            return {}
    if isinstance(flow_document, dict):
        return flow_document
    return {}


def normalize_query_matches(raw_response: Any) -> List[dict]:
    if isinstance(raw_response, dict):
        raw_matches = raw_response.get("matches", [])
    else:
        raw_matches = getattr(raw_response, "matches", [])

    normalized: List[dict] = []
    for match in raw_matches:
        if isinstance(match, dict):
            metadata = match.get("metadata", {}) or {}
            flow_doc = parse_flow_document(metadata)
            text = flow_doc.get("chunk_text") or flow_doc.get("text") or metadata.get("content") or metadata.get("text") or ""
            normalized.append(
                {
                    "id": match.get("id"),
                    "score": match.get("score"),
                    "doc_id": flow_doc.get("doc_id"),
                    "chunk_index": flow_doc.get("chunk_index"),
                    "text": text,
                }
            )
        else:
            metadata = getattr(match, "metadata", {}) or {}
            flow_doc = parse_flow_document(metadata)
            text = flow_doc.get("chunk_text") or flow_doc.get("text") or metadata.get("content") or metadata.get("text") or ""
            normalized.append(
                {
                    "id": getattr(match, "id", None),
                    "score": getattr(match, "score", None),
                    "doc_id": flow_doc.get("doc_id"),
                    "chunk_index": flow_doc.get("chunk_index"),
                    "text": text,
                }
            )

    return normalized


def retrieve_matches(client_id: str, query: str, top_k: int) -> List[dict]:
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    namespace = tenant.get("pinecone_namespace")
    if not namespace:
        raise HTTPException(status_code=400, detail="Pinecone namespace is not configured")

    embed = openai_client.embeddings.create(model=OPENAI_EMBED_MODEL, input=query)
    q_vec = embed.data[0].embedding

    response = index.query(
        namespace=namespace,
        vector=q_vec,
        top_k=top_k,
        include_metadata=True,
    )

    return normalize_query_matches(response)


def answer_with_rag(question: str, matches: List[dict]) -> str:
    if not matches:
        return "No relevant context found in this client knowledge base yet."

    context = []
    for i, match in enumerate(matches, start=1):
        context.append(
            f"[Snippet {i}] doc_id={match.get('doc_id')} chunk={match.get('chunk_index')}\n{match.get('text', '')}"
        )

    prompt = (
        "Use only the provided snippets to answer. "
        "If info is missing, say context is insufficient. Cite snippet numbers.\n\n"
        f"Question:\n{question}\n\n"
        f"Context:\n{'\n\n'.join(context)}"
    )

    completion = openai_client.responses.create(
        model=OPENAI_CHAT_MODEL,
        input=[
            {"role": "system", "content": "Answer concisely from context only."},
            {"role": "user", "content": prompt},
        ],
    )
    return (completion.output_text or "").strip()


@app.get("/")
def root():
    return RedirectResponse(url="/ui")


@app.get("/health")
def health():
    return {
        "ok": True,
        "time": now_iso(),
        "openai_chat_model": OPENAI_CHAT_MODEL,
        "openai_embed_model": OPENAI_EMBED_MODEL,
        "pinecone_index": PINECONE_INDEX,
        "mode": "airbyte",
    }


@app.post("/clients")
def create_client(req: ClientCreateRequest):
    client_id = str(uuid.uuid4())
    TENANT_STORE[client_id] = {
        "name": req.name,
        "pinecone_namespace": req.pinecone_namespace,
        "drive_folder_id": req.drive_folder_id,
        "registered_docs": {},
        "airbyte_source_id": None,
        "airbyte_connection_id": None,
        "created_at": now_iso(),
    }
    return {"client_id": client_id, "name": req.name}


@app.get("/clients")
def list_clients():
    return [
        {
            "client_id": cid,
            "name": data.get("name"),
            "doc_count": len(data.get("registered_docs", {})),
            "has_auth": "credentials" in data,
            "pinecone_namespace": data.get("pinecone_namespace"),
        }
        for cid, data in TENANT_STORE.items()
    ]


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    docs = []
    for doc_id, info in tenant.get("registered_docs", {}).items():
        docs.append(
            {
                "doc_id": doc_id,
                "url": info.get("url"),
                "last_revision": info.get("last_revision"),
                "last_sync_at": info.get("last_sync_at"),
                "chunk_count": info.get("chunk_count", 0),
            }
        )

    return {
        "client_id": client_id,
        "name": tenant.get("name"),
        "has_auth": "credentials" in tenant,
        "doc_count": len(docs),
        "docs": docs,
        "pinecone_namespace": tenant.get("pinecone_namespace"),
        "drive_folder_id": tenant.get("drive_folder_id"),
        "airbyte_connection_id": tenant.get("airbyte_connection_id"),
    }


@app.patch("/clients/{client_id}/config")
def update_client_config(client_id: str, req: ClientUpdateConfigRequest):
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    if req.pinecone_namespace is not None:
        tenant["pinecone_namespace"] = req.pinecone_namespace
    if req.drive_folder_id is not None:
        tenant["drive_folder_id"] = req.drive_folder_id

    return {"ok": True, "client_id": client_id}


@app.post("/oauth/init")
def oauth_init(req: OAuthInitRequest):
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")

    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    TENANT_STORE[req.client_id]["oauth_state"] = state
    OAUTH_STATE_TO_CLIENT[state] = req.client_id
    return {"authorization_url": auth_url, "state": state}


@app.post("/oauth/callback")
def oauth_callback(req: OAuthCallbackRequest):
    tenant = TENANT_STORE.get(req.client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=GOOGLE_SCOPES,
        state=tenant.get("oauth_state"),
        redirect_uri=GOOGLE_REDIRECT_URI,
    )

    flow.fetch_token(authorization_response=req.authorization_response_url)
    creds = flow.credentials

    tenant["credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    if AIRBYTE_API_KEY and AIRBYTE_WORKSPACE_ID:
        try:
            ensure_airbyte_connection(req.client_id)
        except Exception:
            pass
    return {"ok": True, "client_id": req.client_id}


@app.get("/oauth/callback-ui", response_class=HTMLResponse)
def oauth_callback_ui(request: Request):
    full_url = str(request.url)
    state = parse_state_from_redirect_url(full_url)
    if not state or state not in OAUTH_STATE_TO_CLIENT:
        raise HTTPException(status_code=400, detail="Invalid or unknown OAuth state")

    client_id = OAUTH_STATE_TO_CLIENT[state]
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    flow = Flow.from_client_secrets_file(
        GOOGLE_CLIENT_SECRETS_FILE,
        scopes=GOOGLE_SCOPES,
        state=state,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )
    flow.fetch_token(authorization_response=full_url)
    creds = flow.credentials

    tenant["credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    if AIRBYTE_API_KEY and AIRBYTE_WORKSPACE_ID:
        try:
            ensure_airbyte_connection(client_id)
        except Exception:
            pass
    return f"""
    <html>
      <body style=\"font-family: sans-serif; padding: 24px;\">
        <h3>OAuth success</h3>
        <p>Account linked for client <code>{client_id}</code>. You can close this tab.</p>
        <script>
          if (window.opener) {{
            window.opener.postMessage({{ type: 'oauth-success', clientId: '{client_id}' }}, '*');
          }}
          setTimeout(() => window.close(), 1200);
        </script>
      </body>
    </html>
    """


@app.post("/docs/register")
def register_doc(req: RegisterDocRequest):
    tenant = TENANT_STORE.get(req.client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    doc_id = extract_doc_id(req.doc_url)
    tenant["registered_docs"][doc_id] = {
        "url": req.doc_url,
        "last_revision": None,
        "last_sync_at": None,
        "chunk_count": 0,
    }
    return {"ok": True, "client_id": req.client_id, "doc_id": doc_id}


@app.post("/links/add")
def add_link(req: AddLinkRequest):
    """Add a Drive folder link (sets sync source for Airbyte) or Doc link (registers doc)."""
    tenant = TENANT_STORE.get(req.client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")

    if is_folder_url(url):
        folder_id = extract_folder_id(url)
        tenant["drive_folder_id"] = folder_id
        return {"ok": True, "type": "folder", "folder_id": folder_id}
    if is_doc_url(url):
        doc_id = extract_doc_id(url)
        tenant["registered_docs"][doc_id] = {
            "url": url,
            "last_revision": None,
            "last_sync_at": None,
            "chunk_count": 0,
        }
        return {"ok": True, "type": "doc", "doc_id": doc_id}
    raise HTTPException(
        status_code=400,
        detail="Provide a Google Drive folder link (drive.google.com/.../folders/...) or Doc link (docs.google.com/document/d/...)",
    )


@app.post("/airbyte/connect")
def airbyte_connect(req: OAuthInitRequest):
    """Create or update Airbyte source + connection for this client (requires OAuth done)."""
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")
    return ensure_airbyte_connection(req.client_id)


@app.post("/airbyte/trigger-sync")
def airbyte_trigger_sync_endpoint(req: SyncAllRequest):
    """Trigger an Airbyte sync for the client's connection. Airbyte will fetch updates and push to Pinecone."""
    return airbyte_trigger_sync(req.client_id)


@app.post("/query")
def query_docs(req: QueryRequest):
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")

    matches = retrieve_matches(req.client_id, req.query, req.top_k)
    return {"client_id": req.client_id, "matches": matches}


@app.post("/rag/chat")
def rag_chat(req: RagChatRequest):
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")

    matches = retrieve_matches(req.client_id, req.question, req.top_k)
    answer = answer_with_rag(req.question, matches)

    return {
        "client_id": req.client_id,
        "question": req.question,
        "answer": answer,
        "matches": matches,
        "model": OPENAI_CHAT_MODEL,
    }


app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
