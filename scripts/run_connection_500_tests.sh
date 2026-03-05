#!/bin/bash
# Run all connection-500 diagnostic tests (see docs/AIRBYTE_CONNECTION_CREATE_500.md).
# Set SOURCE_ID and DEST_ID if you have them (e.g. from airbyte_setup.py output).

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

echo "=============================================="
echo "Connection 500 diagnostics (AIRBYTE_CONNECTION_CREATE_500.md)"
echo "=============================================="
echo ""

run() {
  echo "--- $1 ---"
  python3 "$SCRIPT_DIR/$1" || true
  echo ""
}

run "test_2_pinecone_destination.py"
run "test_3_google_drive_source.py"
run "test_1_stream_discovery.py"
run "test_4_connection_create.py"

echo "Done. Check VERDICT for each test."
