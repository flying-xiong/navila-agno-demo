#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
CLOSED_LOOP_PYTHON="${CLOSED_LOOP_PYTHON:-python3}"
exec "$CLOSED_LOOP_PYTHON" closed_loop_runner.py "$@"
