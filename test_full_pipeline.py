#!/usr/bin/env python3
import json, os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()

import airbyte as ab
from openai import OpenAI
from pinecone import Pinecone

with open("tokens.json") as f:
    tok = json.load(f)
g = tok["google"]

OPENAI_EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
PINECONE_INDEX = os.getenv("PINECONE_INDEX", "knowledge-base")
NAMESPACE = "test-pyairbyte"
CHUNK_SIZE, CHUNK_OVERLAP = 1000, 100


def chunk_text(text: str):
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


source = ab.get_source(
    "source-google-drive",
    docker_image="airbyte/source-google-drive:latest",
    config={
        "folder_url": "https://drive.google.com/drive/folders/1rNqaJ1iEAdtzAbWO_T_cA8i_KhcKGSRA",
        "credentials": {
            "auth_type": "Client",
            "client_id": g["client_id"],
            "client_secret": g["client_secret"],
            "refresh_token": g["refresh_token"],
        },
        "streams": [{"name": "documents", "globs": ["**"], "validation_policy": "Emit Record",
                     "days_to_sync_if_history_is_full": 3, "format": {"filetype": "unstructured"}}],
    },
    streams=["documents"],
)

cache = ab.get_default_cache()
source.read(cache=cache, force_full_refresh=True)
df = cache["documents"].to_pandas()
print(f"Read {len(df)} doc(s): {df['document_key'].tolist()}")

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
index = Pinecone(api_key=os.getenv("PINECONE_API_KEY")).Index(PINECONE_INDEX)

vectors = []
for _, row in df.iterrows():
    doc_key = str(row.get("document_key", ""))
    content = str(row.get("content") or "").strip()
    if not content:
        print(f"Skipping empty doc: {doc_key}")
        continue
    for i, chunk in enumerate(chunk_text(content)):
        emb = openai_client.embeddings.create(model=OPENAI_EMBED_MODEL, input=chunk, dimensions=1024)
        vectors.append({
            "id": f"poc-{doc_key}-{i}",
            "values": emb.data[0].embedding,
            "metadata": {"flow_document": json.dumps({"doc_id": doc_key, "chunk_index": i, "chunk_text": chunk})},
        })

if vectors:
    index.upsert(vectors=vectors, namespace=NAMESPACE)
    print(f"Upserted {len(vectors)} vector(s) to namespace={NAMESPACE}")

# Verify with query
qemb = openai_client.embeddings.create(model=OPENAI_EMBED_MODEL, input="new file content", dimensions=1024)
res = index.query(namespace=NAMESPACE, vector=qemb.data[0].embedding, top_k=3, include_metadata=True)
print("Query matches:")
for m in res.matches:
    fd = json.loads(m.metadata.get("flow_document", "{}"))
    print(f"  score={m.score:.4f} | {fd.get('chunk_text', '')[:120]!r}")

print("\nSUCCESS - full pipeline works!")
