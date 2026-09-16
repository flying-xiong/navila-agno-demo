#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
SUPERVISOR_PYTHON="${SUPERVISOR_PYTHON:-python3}"
exec "$SUPERVISOR_PYTHON" supervisor_server.py
