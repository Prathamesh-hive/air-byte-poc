import html
import json
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# Allow http://localhost for OAuth (must be set before importing google_auth_oauthlib)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

import requests
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from openai import OpenAI
from pinecone import Pinecone
from pydantic import BaseModel

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


class AddIntegrationRequest(BaseModel):
    """Add an Airbyte integration (e.g. Google Drive) for a client."""
    client_id: str
    integration_type: str  # "google_drive" in first cut
    config: Optional[Dict[str, Any]] = None  # e.g. {"folder_id": "..."}
    name: Optional[str] = None


GOOGLE_CLIENT_SECRETS_FILE = os.getenv("GOOGLE_CLIENT_SECRETS_FILE", "client_secret.json")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth/callback-ui")
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/drive.readonly",
]

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "knowledge-base")

AIRBYTE_API_URL = os.getenv("AIRBYTE_API_URL", "http://localhost:8000").rstrip("/")
AIRBYTE_API_PATH_AUTH = os.getenv("AIRBYTE_API_PATH_AUTH", "/v1").rstrip("/")  # OSS: /api/v1
AIRBYTE_API_PATH_PUBLIC = os.getenv("AIRBYTE_API_PATH_PUBLIC", "/v1").rstrip("/")  # OSS: /api/public/v1
AIRBYTE_USE_API = os.getenv("AIRBYTE_USE_API", "").strip().lower() in ("1", "true", "yes")
AIRBYTE_API_KEY = os.getenv("AIRBYTE_API_KEY", "")
AIRBYTE_WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID", "")
AIRBYTE_REQUEST_TIMEOUT = int(os.getenv("AIRBYTE_REQUEST_TIMEOUT", "90"))
AIRBYTE_CLIENT_ID = os.getenv("AIRBYTE_CLIENT_ID", "")
AIRBYTE_CLIENT_SECRET = os.getenv("AIRBYTE_CLIENT_SECRET", "")
AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE = os.getenv(
    "AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE", "9f8dda77-1048-4368-815b-269bf54ee9b8"
)
CONNECTOR_DEFINITION_IDS: Dict[str, str] = {"google_drive": AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
OPENAI_EMBED_DIMENSIONS = int(os.getenv("OPENAI_EMBED_DIMENSIONS", "1024"))

app = FastAPI(title="Multi-Tenant Google Docs Knowledge Base (Airbyte + Pinecone)")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

if not PINECONE_API_KEY:
    raise RuntimeError("PINECONE_API_KEY is required")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
pc = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index(PINECONE_INDEX)

# Airbyte: optional. Need either AIRBYTE_API_KEY (static token) or AIRBYTE_CLIENT_ID + AIRBYTE_CLIENT_SECRET (Cloud token exchange).
_AIRBYTE_TOKEN_CACHE: Dict[str, Any] = {}  # { "token": str, "expires_at": float }


def _airbyte_bearer_token() -> str:
    """Return Bearer token: static API key or fresh token from client credentials (Airbyte Cloud)."""
    if AIRBYTE_API_KEY:
        return AIRBYTE_API_KEY
    if AIRBYTE_CLIENT_ID and AIRBYTE_CLIENT_SECRET:
        now = datetime.now(timezone.utc).timestamp()
        if _AIRBYTE_TOKEN_CACHE.get("token") and (_AIRBYTE_TOKEN_CACHE.get("expires_at") or 0) > now + 60:
            return _AIRBYTE_TOKEN_CACHE["token"]
        url = f"{AIRBYTE_API_URL}{AIRBYTE_API_PATH_AUTH}/applications/token"
        r = requests.post(
            url,
            json={
                "client_id": AIRBYTE_CLIENT_ID,
                "client_secret": AIRBYTE_CLIENT_SECRET,
                "grant-type": "client_credentials",
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Airbyte token exchange failed: {r.status_code} {r.text[:300]}",
            )
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise HTTPException(status_code=502, detail="Airbyte token response missing access_token")
        expires_in = data.get("expires_in", 900)
        _AIRBYTE_TOKEN_CACHE["token"] = token
        _AIRBYTE_TOKEN_CACHE["expires_at"] = now + expires_in
        return token
    return ""


def _airbyte_configured() -> bool:
    return bool(AIRBYTE_WORKSPACE_ID and (AIRBYTE_API_KEY or (AIRBYTE_CLIENT_ID and AIRBYTE_CLIENT_SECRET)))


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
    if not AIRBYTE_WORKSPACE_ID:
        raise HTTPException(status_code=503, detail="AIRBYTE_WORKSPACE_ID is required")
    token = _airbyte_bearer_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Airbyte not configured: set AIRBYTE_API_KEY or (AIRBYTE_CLIENT_ID + AIRBYTE_CLIENT_SECRET)",
        )
    url = f"{AIRBYTE_API_URL}{AIRBYTE_API_PATH_PUBLIC}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, url, headers=headers, json=json_body, timeout=AIRBYTE_REQUEST_TIMEOUT)
    if r.status_code >= 400:
        detail = r.text[:1200]
        try:
            err = r.json()
            if isinstance(err, dict):
                if "detail" in err:
                    detail = err.get("detail", detail)
                if "_embedded" in err and isinstance(err["_embedded"], dict):
                    emb = err["_embedded"]
                    if "errors" in emb and isinstance(emb["errors"], list):
                        parts = [err.get("message", "")]
                        for e in emb["errors"][:5]:
                            if isinstance(e, dict) and e.get("message"):
                                parts.append(e.get("message", ""))
                                if e.get("path"):
                                    parts.append(f" (path: {e.get('path')})")
                        detail = " ".join(parts) or detail
                elif "message" in err:
                    detail = err.get("message", detail)
                detail = f"[HTTP {r.status_code}] {detail}"
        except Exception:
            detail = f"[HTTP {r.status_code}] {detail}"
        raise HTTPException(status_code=min(r.status_code, 502), detail=str(detail))
    return r.json() if r.text else {}


def _airbyte_request_raw(method: str, path: str, json_body: Optional[dict] = None, timeout: int = 15) -> tuple[int, str]:
    """Call Airbyte API and return (status_code, response_text) without raising. For debugging."""
    if not AIRBYTE_WORKSPACE_ID or not _airbyte_bearer_token():
        return (503, "Airbyte not configured")
    url = f"{AIRBYTE_API_URL}{AIRBYTE_API_PATH_PUBLIC}{path}"
    headers = {"Authorization": f"Bearer {_airbyte_bearer_token()}", "Content-Type": "application/json"}
    try:
        r = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        return (r.status_code, r.text)
    except requests.exceptions.Timeout:
        return (0, f"Request timed out after {timeout}s")
    except requests.exceptions.RequestException as e:
        return (0, str(e))


def _airbyte_pinecone_destination_config() -> dict:
    """Build Pinecone destination config matching Airbyte API destination-pinecone schema (OpenAPI oneOf)."""
    embed_dim = OPENAI_EMBED_DIMENSIONS
    return {
        "destinationType": "pinecone",
        "embedding": {"mode": "openai", "openai_key": OPENAI_API_KEY, "dimensions": embed_dim},
        "indexing": {
            "index": PINECONE_INDEX,
            "pinecone_key": PINECONE_API_KEY,
            "pinecone_environment": os.getenv("PINECONE_ENV", "us-east-1"),
        },
        "processing": {
            "chunk_size": 1500,
            "chunk_overlap": 20,
            "text_fields": ["content"],
            "metadata_fields": [],
            "text_splitter": {"mode": "separator", "separators": ["\n\n", "\n", " "], "keep_separator": False},
        },
        "omit_raw_text": False,
    }


def _airbyte_get_or_create_destination() -> str:
    """Return existing Pinecone destination id or create one."""
    list_res = _airbyte_request("GET", f"/destinations?workspaceIds={AIRBYTE_WORKSPACE_ID}")
    destinations = list_res.get("destinations", list_res.get("data", []))
    for d in destinations:
        if d.get("name") == "pinecone-knowledge-base":
            return d["destinationId"]
    dest_def_id = os.getenv("AIRBYTE_DESTINATION_DEFINITION_ID_PINECONE", "3d2b6f84-7f0d-4e3f-a5e5-7c7d4b50eabd")
    create = _airbyte_request("POST", "/destinations", {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": "pinecone-knowledge-base",
        "definitionId": dest_def_id,
        "configuration": _airbyte_pinecone_destination_config(),
    })
    return create["destinationId"]


def _build_google_drive_source_config(tenant: dict, integration: dict) -> dict:
    """Build Airbyte source config for Google Drive. Requires tenant credentials and integration config with folder_id."""
    creds = tenant.get("credentials") or {}
    folder_id = (integration.get("config") or {}).get("folder_id") or tenant.get("drive_folder_id")
    if not folder_id:
        raise HTTPException(status_code=400, detail="Google Drive integration requires folder_id in config or client drive_folder_id")
    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    return {
        "folder_url": folder_url,
        "credentials": {
            "auth_type": "Client",
            "client_id": creds.get("client_id", ""),
            "client_secret": creds.get("client_secret", ""),
            "refresh_token": creds.get("refresh_token", ""),
        },
        "streams": [
            {
                "name": "documents",
                "globs": ["**"],
                "validation_policy": "Emit Record",
                "days_to_sync_if_history_is_full": 3,
                "format": {"filetype": "unstructured"},
            }
        ],
    }


def _airbyte_create_or_update_source_for_integration(client_id: str, tenant: dict, integration: dict) -> str:
    """Create or update Airbyte source for this integration; return source_id."""
    itype = integration.get("integration_type", "google_drive")
    def_id = CONNECTOR_DEFINITION_IDS.get(itype)
    if not def_id:
        raise HTTPException(status_code=400, detail=f"Unknown integration_type: {itype}")
    if itype != "google_drive":
        raise HTTPException(status_code=400, detail="Only google_drive is supported in first cut")
    config = _build_google_drive_source_config(tenant, integration)
    name = integration.get("name") or f"poc-{client_id[:8]}-{itype}"
    list_res = _airbyte_request("GET", f"/sources?workspaceIds={AIRBYTE_WORKSPACE_ID}")
    sources = list_res.get("sources", list_res.get("data", []))
    for s in sources:
        if s.get("name") == name:
            _airbyte_request("PATCH", f"/sources/{s['sourceId']}", {"name": name, "configuration": config})
            return s["sourceId"]
    create = _airbyte_request("POST", "/sources", {
        "workspaceId": AIRBYTE_WORKSPACE_ID,
        "name": name,
        "definitionId": def_id,
        "configuration": config,
    })
    return create["sourceId"]


def _airbyte_create_or_update_connection_for_integration(
    client_id: str, tenant: dict, integration: dict, source_id: str, destination_id: str
) -> str:
    """Create or update Airbyte connection source -> destination; return connection_id."""
    namespace = tenant.get("pinecone_namespace")
    if not namespace:
        raise HTTPException(status_code=400, detail="Client pinecone_namespace required")
    name = integration.get("name") or f"poc-{client_id[:8]}-conn"
    list_res = _airbyte_request("GET", f"/connections?workspaceId={AIRBYTE_WORKSPACE_ID}")
    connections = list_res.get("connections", list_res.get("data", []))
    for c in connections:
        if c.get("sourceId") == source_id and c.get("destinationId") == destination_id:
            _airbyte_request("PATCH", f"/connections/{c['connectionId']}", {
                "name": name,
                "configurations": {"streams": [{"name": "documents", "syncMode": "full_refresh_overwrite"}]},
                "namespaceDefinition": "destination",
                "namespaceFormat": namespace,
            })
            return c["connectionId"]
    create = _airbyte_request("POST", "/connections", {
        "sourceId": source_id,
        "destinationId": destination_id,
        "name": name,
        "configurations": {"streams": [{"name": "documents", "syncMode": "full_refresh_overwrite"}]},
        "namespaceDefinition": "destination",
        "namespaceFormat": namespace,
    })
    return create["connectionId"]


GDRIVE_SOURCE_IMAGE = os.getenv("GDRIVE_SOURCE_IMAGE", "airbyte/source-google-drive:latest")
CHUNK_SIZE = int(os.getenv("SYNC_CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("SYNC_CHUNK_OVERLAP", "100"))


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += size - overlap
    return chunks


def ensure_airbyte_connection(client_id: str) -> dict:
    """Validate credentials and Drive folder. In API mode: ensure at least one integration (create default Drive if needed)."""
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")
    if "credentials" not in tenant:
        raise HTTPException(status_code=400, detail="No Google credentials. Complete OAuth first.")
    folder_id = tenant.get("drive_folder_id")
    if not folder_id:
        raise HTTPException(status_code=400, detail="No Drive folder configured. Add a folder URL via /links/add.")
    c = tenant["credentials"]
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={"client_id": c["client_id"], "client_secret": c["client_secret"],
              "refresh_token": c["refresh_token"], "grant_type": "refresh_token"},
        timeout=10,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Google token refresh failed: {r.text[:200]}")
    tenant["credentials"]["token"] = r.json().get("access_token", c.get("token"))

    if AIRBYTE_USE_API and _airbyte_configured():
        integrations = tenant.get("integrations") or []
        if not integrations:
            dest_id = _airbyte_get_or_create_destination()
            integration = {"integration_type": "google_drive", "config": {"folder_id": folder_id}, "name": f"drive-{client_id[:8]}"}
            source_id = _airbyte_create_or_update_source_for_integration(client_id, tenant, integration)
            connection_id = _airbyte_create_or_update_connection_for_integration(client_id, tenant, integration, source_id, dest_id)
            integration["airbyte_source_id"] = source_id
            integration["airbyte_connection_id"] = connection_id
            integrations.append(integration)
            tenant["integrations"] = integrations
        return {"status": "ready", "folder_id": folder_id, "mode": "airbyte-api", "integrations": len(integrations)}
    tenant["pyairbyte_ready"] = True
    return {"status": "ready", "folder_id": folder_id, "mode": "pyairbyte-local"}


def airbyte_trigger_sync(client_id: str) -> dict:
    """Trigger sync: in API mode POST /jobs per connection; else run PyAirbyte locally."""
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")

    if AIRBYTE_USE_API and _airbyte_configured():
        integrations = tenant.get("integrations") or []
        connection_ids = [i["airbyte_connection_id"] for i in integrations if i.get("airbyte_connection_id")]
        if not connection_ids:
            raise HTTPException(
                status_code=400,
                detail="No Airbyte connections. Call POST /airbyte/connect first to add an integration.",
            )
        jobs = []
        for conn_id in connection_ids:
            try:
                job = _airbyte_request("POST", "/jobs", {"connectionId": conn_id, "jobType": "sync"})
                job_id = job.get("jobId") or job.get("id")
                jobs.append({"connectionId": conn_id, "jobId": job_id, "status": job.get("status", "pending")})
            except HTTPException as e:
                jobs.append({"connectionId": conn_id, "error": str(e.detail)})
        tenant["last_sync_at"] = now_iso()
        return {"status": "ok", "mode": "airbyte-api", "jobs": jobs}

    if "credentials" not in tenant:
        raise HTTPException(status_code=400, detail="No Google credentials. Complete OAuth first.")
    folder_id = tenant.get("drive_folder_id")
    if not folder_id:
        raise HTTPException(status_code=400, detail="No Drive folder configured.")

    import airbyte as ab

    credentials_from_store(client_id)
    c = tenant["credentials"]
    namespace = tenant.get("pinecone_namespace")
    if not namespace:
        raise HTTPException(status_code=400, detail="No Pinecone namespace configured.")

    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    print(f"[PyAirbyte] Starting sync for client={client_id[:8]} folder={folder_id}", flush=True)

    source = ab.get_source(
        "source-google-drive",
        docker_image=GDRIVE_SOURCE_IMAGE,
        config={
            "folder_url": folder_url,
            "credentials": {
                "auth_type": "Client",
                "client_id": c["client_id"],
                "client_secret": c["client_secret"],
                "refresh_token": c["refresh_token"],
            },
            "streams": [{"name": "documents", "globs": ["**"],
                         "validation_policy": "Emit Record",
                         "days_to_sync_if_history_is_full": 3,
                         "format": {"filetype": "unstructured"}}],
        },
        streams=["documents"],
    )

    cache = ab.get_default_cache()
    source.read(cache=cache, force_full_refresh=True)
    df = cache["documents"].to_pandas()
    print(f"[PyAirbyte] Read {len(df)} document(s)", flush=True)

    vectors, synced_docs = [], []
    for _, row in df.iterrows():
        doc_key = str(row.get("document_key", ""))
        content = str(row.get("content") or "").strip()
        if not content:
            print(f"[PyAirbyte] Skipping empty doc: {doc_key}", flush=True)
            continue
        chunks = _chunk_text(content)
        for i, chunk in enumerate(chunks):
            emb = openai_client.embeddings.create(model=OPENAI_EMBED_MODEL, input=chunk, dimensions=OPENAI_EMBED_DIMENSIONS)
            vec_id = f"{client_id[:8]}-{doc_key}-{i}"
            vectors.append({
                "id": vec_id,
                "values": emb.data[0].embedding,
                "metadata": {"flow_document": json.dumps(
                    {"doc_id": doc_key, "chunk_index": i, "chunk_text": chunk}
                )},
            })
        synced_docs.append({"doc_key": doc_key, "chunks": len(chunks)})

    if vectors:
        # Upsert in batches of 100
        for batch_start in range(0, len(vectors), 100):
            index.upsert(vectors=vectors[batch_start:batch_start + 100], namespace=namespace)
        print(f"[PyAirbyte] Upserted {len(vectors)} vectors to Pinecone ns={namespace}", flush=True)

    tenant["last_sync_at"] = now_iso()
    return {
        "status": "ok",
        "documents_synced": len(synced_docs),
        "vectors_upserted": len(vectors),
        "docs": synced_docs,
        "namespace": namespace,
    }


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

    embed = openai_client.embeddings.create(model=OPENAI_EMBED_MODEL, input=query, dimensions=OPENAI_EMBED_DIMENSIONS)
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
    out = {
        "ok": True,
        "time": now_iso(),
        "openai_chat_model": OPENAI_CHAT_MODEL,
        "openai_embed_model": OPENAI_EMBED_MODEL,
        "pinecone_index": PINECONE_INDEX,
        "airbyte_use_api": AIRBYTE_USE_API,
        "airbyte_ui_url": AIRBYTE_API_URL if (AIRBYTE_USE_API and _airbyte_configured()) else None,
    }
    if out["airbyte_ui_url"]:
        out["airbyte_ui_note"] = "Admin: open this URL in browser for Airbyte OSS UI (sources, connections, sync history)."
    return out


@app.post("/clients")
def create_client(req: ClientCreateRequest):
    client_id = str(uuid.uuid4())
    TENANT_STORE[client_id] = {
        "name": req.name,
        "pinecone_namespace": req.pinecone_namespace,
        "drive_folder_id": req.drive_folder_id,
        "registered_docs": {},
        "integrations": [],
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
        "integrations": tenant.get("integrations", []),
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
    if _airbyte_configured():
        try:
            ensure_airbyte_connection(req.client_id)
        except Exception:
            pass
    return {"ok": True, "client_id": req.client_id}


def _oauth_error_html(title: str, err_msg: str, status: int = 500, tb: Optional[str] = None) -> HTMLResponse:
    tb_html = f"<pre style='background:#f5f5f5;padding:12px;overflow:auto;font-size:12px;'>{html.escape(tb or '')}</pre>" if tb else ""
    return HTMLResponse(
        content=f"""
        <html><body style="font-family: sans-serif; padding: 24px; max-width: 720px;">
        <h3>{html.escape(title)}</h3>
        <p><strong>Error:</strong> <code>{html.escape(err_msg)}</code></p>
        {tb_html}
        <p>Checks: (1) GOOGLE_REDIRECT_URI in .env is exactly <code>http://localhost:8000/oauth/callback-ui</code>.
        (2) In Google Cloud Console, Authorized redirect URIs contains that exact URL (no trailing slash).
        (3) client_secret.json exists and is for the same GCP project. (4) If server restarted, start OAuth again from the app.</p>
        </body></html>
        """,
        status_code=status,
    )


@app.get("/oauth/callback-ui", response_class=HTMLResponse)
def oauth_callback_ui(request: Request):
    try:
        return _oauth_callback_ui_impl(request)
    except Exception as e:
        return _oauth_error_html(
            "OAuth callback error",
            str(e),
            status=500,
            tb=traceback.format_exc(),
        )


def _oauth_callback_ui_impl(request: Request) -> HTMLResponse:
    full_url = str(request.url)
    state = parse_state_from_redirect_url(full_url)
    if not state or state not in OAUTH_STATE_TO_CLIENT:
        return _oauth_error_html(
            "Invalid or expired OAuth state",
            "State not found. If the server restarted, go back to the app and click Authenticate with Google again.",
            status=400,
        )

    client_id = OAUTH_STATE_TO_CLIENT[state]
    tenant = TENANT_STORE.get(client_id)
    if not tenant:
        return _oauth_error_html("Client not found", f"No tenant for client_id {client_id[:8]}...", status=404)

    try:
        flow = Flow.from_client_secrets_file(
            GOOGLE_CLIENT_SECRETS_FILE,
            scopes=GOOGLE_SCOPES,
            state=state,
            redirect_uri=GOOGLE_REDIRECT_URI,
        )
        flow.fetch_token(authorization_response=full_url)
        creds = flow.credentials
    except Exception as e:
        return _oauth_error_html("OAuth token exchange failed", str(e), status=500, tb=traceback.format_exc())

    try:
        scopes = list(creds.scopes) if getattr(creds, "scopes", None) else []
        expiry = getattr(creds, "expiry", None)
        stored_creds = {
            "token": getattr(creds, "token", None),
            "refresh_token": getattr(creds, "refresh_token", None),
            "token_uri": getattr(creds, "token_uri", ""),
            "client_id": getattr(creds, "client_id", ""),
            "client_secret": getattr(creds, "client_secret", ""),
            "scopes": scopes,
            "expiry": expiry.isoformat() if expiry else None,
        }
        tenant["credentials"] = stored_creds
        # Save to file for debugging/testing
        try:
            with open("tokens.json", "w") as _f:
                json.dump({
                    "client_id": client_id,
                    "google": stored_creds,
                    "drive_folder_id": tenant.get("drive_folder_id"),
                    "saved_at": now_iso(),
                }, _f, indent=2)
            print(f"[OAuth] tokens saved to tokens.json", flush=True)
        except Exception as _e:
            print(f"[OAuth] WARNING: could not save tokens.json: {_e}", flush=True)
    except Exception as e:
        return _oauth_error_html("Failed to store credentials", str(e), status=500, tb=traceback.format_exc())

    if _airbyte_configured():
        try:
            ensure_airbyte_connection(client_id)
        except Exception:
            pass

    client_id_escaped = json.dumps(client_id)
    return HTMLResponse(
        content=f"""
        <html>
          <body style="font-family: sans-serif; padding: 24px;">
            <h3>OAuth success</h3>
            <p>Account linked for client <code>{html.escape(client_id)}</code>.</p>
            <p>Redirecting back to the app...</p>
            <script>
              if (window.opener) {{
                window.opener.postMessage({{ type: 'oauth-success', clientId: {client_id_escaped} }}, '*');
                setTimeout(function() {{ window.close(); }}, 800);
              }} else {{
                window.location.href = '/ui/';
              }}
            </script>
          </body>
        </html>
        """
    )


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


@app.post("/airbyte/debug-source")
def airbyte_debug_source(req: OAuthInitRequest, variant: Optional[str] = None):
    """
    Debug: try source config variant(s) and return raw Airbyte responses.
    No query: tries all 3 (minimal, csv, unstructured). ?variant=minimal (or csv|unstructured): try only that one.
    """
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")
    tenant = TENANT_STORE[req.client_id]
    if "credentials" not in tenant:
        raise HTTPException(status_code=400, detail="No Google credentials")
    credentials_from_store(req.client_id)
    c = tenant["credentials"]
    folder_id = tenant.get("drive_folder_id") or "root"
    def_id = os.getenv("AIRBYTE_SOURCE_DEFINITION_ID_GOOGLE_DRIVE", "9f8dda77-1048-4368-815b-269bf54ee9b8")
    base_cred = {"auth_type": "Client", "client_id": c["client_id"], "client_secret": c["client_secret"], "refresh_token": c["refresh_token"]}
    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"

    variants = {
        "minimal": {"folder_url": folder_url, "credentials": base_cred, "streams": [{"name": "s1", "globs": ["**"]}]},
        "csv": {
            "folder_url": folder_url,
            "credentials": base_cred,
            "streams": [{
                "name": "stream1", "globs": ["**"],
                "validation_policy": "Emit Record", "days_to_sync_if_history_is_full": 3,
                "format": {"filetype": "csv", "header_definition": {"header_definition_type": "From CSV"}},
            }],
        },
        "unstructured": {
            "folder_url": folder_url,
            "credentials": base_cred,
            "streams": [{"name": "documents", "globs": ["**"], "validation_policy": "Emit Record", "days_to_sync_if_history_is_full": 3, "format": {"filetype": "unstructured"}}],
        },
    }
    if variant:
        if variant not in variants:
            raise HTTPException(status_code=400, detail=f"variant must be one of: minimal, csv, unstructured")
        variants = {variant: variants[variant]}

    results = []
    for variant, config in variants.items():
        body = {"workspaceId": AIRBYTE_WORKSPACE_ID, "name": f"debug-{variant}-{req.client_id[:8]}", "definitionId": def_id, "configuration": config}
        status, text = _airbyte_request_raw("POST", "/sources", body, timeout=15)
        results.append({
            "variant": variant,
            "response_status": status,
            "response_body": text[:3000],
            "success": 200 <= status < 300,
        })

    return {
        "client_id": req.client_id,
        "airbyte_url": f"{AIRBYTE_API_URL}{AIRBYTE_API_PATH_PUBLIC}/sources",
        "results": results,
        "summary": {r["variant"]: "OK" if r["success"] else ("Timeout" if r["response_status"] == 0 else f"HTTP {r['response_status']}") for r in results},
    }


@app.post("/airbyte/integrations")
def add_integration(req: AddIntegrationRequest):
    """Add an Airbyte integration (e.g. Google Drive) for a client. Requires AIRBYTE_USE_API=1 and Airbyte configured."""
    if not AIRBYTE_USE_API or not _airbyte_configured():
        raise HTTPException(status_code=503, detail="Set AIRBYTE_USE_API=1 and configure Airbyte (API URL, workspace, credentials)")
    tenant = TENANT_STORE.get(req.client_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Client not found")
    if req.integration_type != "google_drive":
        raise HTTPException(status_code=400, detail="Only integration_type=google_drive is supported in first cut")
    if "credentials" not in tenant:
        raise HTTPException(status_code=400, detail="Complete OAuth first")
    if "integrations" not in tenant:
        tenant["integrations"] = []
    config = req.config or {}
    folder_id = config.get("folder_id") or tenant.get("drive_folder_id")
    if not folder_id:
        raise HTTPException(status_code=400, detail="Provide folder_id in config or set client drive_folder_id")
    integration = {
        "integration_type": req.integration_type,
        "config": {"folder_id": folder_id},
        "name": req.name or f"drive-{req.client_id[:8]}-{len(tenant['integrations'])}",
    }
    dest_id = _airbyte_get_or_create_destination()
    source_id = _airbyte_create_or_update_source_for_integration(req.client_id, tenant, integration)
    connection_id = _airbyte_create_or_update_connection_for_integration(
        req.client_id, tenant, integration, source_id, dest_id
    )
    integration["airbyte_source_id"] = source_id
    integration["airbyte_connection_id"] = connection_id
    tenant["integrations"].append(integration)
    return {"integration": integration, "client_id": req.client_id}


@app.post("/airbyte/connect")
def airbyte_connect(req: OAuthInitRequest):
    """Validate credentials and Drive folder; in API mode ensure at least one integration exists."""
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")
    return ensure_airbyte_connection(req.client_id)


@app.post("/airbyte/trigger-sync")
def airbyte_trigger_sync_endpoint(req: SyncAllRequest):
    """Sync Google Drive → Pinecone via PyAirbyte (runs connector locally in Docker)."""
    if req.client_id not in TENANT_STORE:
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        return airbyte_trigger_sync(req.client_id)
    except Exception as e:
        err_msg = str(e)
        if "docker" in err_msg.lower() or "Connector failed" in err_msg or "AirbyteConnectorFailedError" in type(e).__name__:
            raise HTTPException(
                status_code=503,
                detail=f"Sync failed: Docker may not be running. Start Docker Desktop and retry. Details: {err_msg[:400]}",
            )
        raise


@app.delete("/airbyte/cleanup-sources")
def airbyte_cleanup_sources():
    """Delete all stale/duplicate Google Drive sources in the workspace (keeps none). Use before reconnecting."""
    if not _airbyte_configured():
        raise HTTPException(status_code=503, detail="Airbyte not configured")
    res = _airbyte_request("GET", f"/sources?workspaceIds={AIRBYTE_WORKSPACE_ID}")
    sources = res.get("data", [])
    deleted, errors = [], []
    for s in sources:
        sid = s.get("sourceId")
        try:
            _airbyte_request("DELETE", f"/sources/{sid}")
            deleted.append(sid)
        except Exception as e:
            errors.append({"sourceId": sid, "error": str(e)})
    return {"deleted": len(deleted), "errors": errors, "deleted_ids": deleted}


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
