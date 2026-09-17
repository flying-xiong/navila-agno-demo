#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export NAVILA_ROOT="${NAVILA_ROOT:-}"
export NAVILA_MODEL_PATH="${NAVILA_MODEL_PATH:-${NAVILA_ROOT}/ckpt}"
export NAVILA_CUDA_VISIBLE_DEVICES="${NAVILA_CUDA_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-0}}"
export CUDA_VISIBLE_DEVICES="$NAVILA_CUDA_VISIBLE_DEVICES"
export NAVILA_DEVICE="${NAVILA_DEVICE:-cuda:0}"
NAVILA_PYTHON="${NAVILA_PYTHON:-python3}"
if [ -z "$NAVILA_ROOT" ]; then
  echo "请先设置 NAVILA_ROOT，指向 NaVILA 仓库路径" >&2
  exit 1
fi
exec "$NAVILA_PYTHON" na_vila_bridge.py
