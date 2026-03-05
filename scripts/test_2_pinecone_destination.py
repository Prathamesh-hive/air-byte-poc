#!/usr/bin/env python3
"""
Test: Pinecone destination (cause #2 in AIRBYTE_CONNECTION_CREATE_500.md).

Checks that indexes exist, dimensions match, and API key works. Mismatch → 500.

Usage:
  python scripts/test_2_pinecone_destination.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# Index name -> expected dimension for Airbyte (pm-test, us-east-1, text-embedding-3-small)
EXPECTED = [
    (os.getenv("PINECONE_INDEX", "pm-test"), 1536),
]


def main():
    print("Test 2: Pinecone destination (index exists, dimension, API key)")
    print("  Doc: AIRBYTE_CONNECTION_CREATE_500.md §2 — index/dimension/env mismatch → 500")
    print()

    api_key = os.getenv("PINECONE_API_KEY")
    if not api_key:
        print("SKIP: Set PINECONE_API_KEY in .env")
        sys.exit(2)

    try:
        from pinecone import Pinecone
    except ImportError:
        print("SKIP: pip install pinecone")
        sys.exit(2)

    pc = Pinecone(api_key=api_key)
    all_ok = True
    for name, exp_dim in EXPECTED:
        try:
            desc = pc.describe_index(name)
            dim = getattr(desc, "dimension", None) or (desc.get("dimension") if isinstance(desc, dict) else None)
            host = getattr(desc, "host", "") or (desc.get("host", "") if isinstance(desc, dict) else "")
            if dim != exp_dim:
                print(f"  {name}: dimension={dim} (expected {exp_dim}) MISMATCH")
                all_ok = False
            else:
                print(f"  {name}: dimension={dim} OK, host={host[:50]}...")
        except Exception as e:
            print(f"  {name}: ERROR — {e}")
            all_ok = False

    print()
    if all_ok:
        print("VERDICT: PASS — Pinecone indexes and dimensions match. Cause #2 is unlikely.")
        sys.exit(0)
    else:
        print("VERDICT: FAIL — Index missing or dimension mismatch. Can cause connection create 500.")
        print("  Fix: Create index in Pinecone with correct dimension; set pinecone_environment=us-east-1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
