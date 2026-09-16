#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export GO2_BRIDGE_HOST="${GO2_BRIDGE_HOST:-0.0.0.0}"
export GO2_BRIDGE_PORT="${GO2_BRIDGE_PORT:-8013}"
export GO2_NETWORK_INTERFACE="${GO2_NETWORK_INTERFACE:-}"
# 安全默认：未显式设置 ENABLE_MOTION=1 时不会执行真实运动。
export ENABLE_MOTION="${ENABLE_MOTION:-0}"
export ENABLE_VIDEO="${ENABLE_VIDEO:-1}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" go2_bridge.py "$@"
