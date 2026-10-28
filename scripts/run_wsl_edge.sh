#!/usr/bin/env bash
# Run in WSL/Linux; results stay on the native Linux filesystem.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
base=${1:-"$HOME/edge-results/esp32c3-wifi-link"}
output=$base
version=2
while [[ -e "$output" ]]; do
    output="${base}-v${version}"
    version=$((version + 1))
done
mkdir -p -- "$(dirname -- "$output")"
make -C "$repo_root/linux"
printf 'Linux output: %s\n' "$output"
exec "$repo_root/linux/build/edge_scheduler" --bind 0.0.0.0 --port 9000 \
    --policy priority --capacity 32 --window 8 --passes 1 --output "$output"
