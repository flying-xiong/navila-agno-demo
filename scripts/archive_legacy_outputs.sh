#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

stamp="$(date +%Y%m%d-%H%M%S)"
destination="archive/legacy_${stamp}"
mkdir -p "$destination"

for path in data/go2_frames output/output_closed_loop output_closed_loop; do
  if [ -e "$path" ]; then
    target="$destination/$path"
    mkdir -p "$(dirname "$target")"
    mv "$path" "$target"
    echo "moved $path -> $target"
  fi
done

echo "legacy artifacts archived in $destination"
