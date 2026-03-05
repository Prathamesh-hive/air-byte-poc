#!/usr/bin/env python3
"""Fetch Pinecone index details for pm-test (or PINECONE_INDEX)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

api_key = os.getenv("PINECONE_API_KEY")
if not api_key:
    print("PINECONE_API_KEY not set")
    sys.exit(1)

from pinecone import Pinecone
pc = Pinecone(api_key=api_key)

index_name = os.getenv("PINECONE_INDEX", "pm-test")
expected = [(index_name, 1536, "us-east-1")]

for name, exp_dim, exp_region in expected:
    try:
        desc = pc.describe_index(name)
        # Handle both object and dict-like response
        if hasattr(desc, "dimension"):
            dim = desc.dimension
            host = getattr(desc, "host", "") or ""
        else:
            dim = desc.get("dimension")
            host = desc.get("host", "")
        meta = getattr(desc, "metadata", None)
        if meta is None and hasattr(desc, "get"):
            meta = desc.get("metadata") or {}
        if meta is None:
            meta = {}
        if hasattr(meta, "get"):
            cloud = meta.get("cloud", "") or ""
            region = meta.get("region", "") or ""
        else:
            cloud = getattr(meta, "cloud", "") or ""
            region = getattr(meta, "region", "") or ""
        if not region and host:
            for r in ["us-east-1", "us-west-2"]:
                if r in host:
                    region = r
                    break
        ok_dim = dim == exp_dim
        ok_region = exp_region in str(region) or exp_region in host
        print(f"Index: {name}")
        print(f"  dimension: {dim} (expected {exp_dim}) {'OK' if ok_dim else 'MISMATCH'}")
        print(f"  host: {host[:75]}..." if len(host) > 75 else f"  host: {host}")
        print(f"  cloud/region: {cloud} / {region} (expected region {exp_region}) {'OK' if ok_region else 'CHECK'}")
        if not ok_dim or not ok_region:
            print(f"  -> Airbyte config: index={name}, dimensions={dim}, pinecone_environment={exp_region}")
        print()
    except Exception as e:
        print(f"Index: {name} -> ERROR: {e}")
        import traceback
        traceback.print_exc()
        print()

print("Summary: Use in Airbyte destination config:")
print(f"  {index_name} -> index={index_name}, dimensions=1536, pinecone_environment=us-east-1")
print("Dimension match is required; region us-east-1 is correct for AWS (serverless may not expose it in API).")
