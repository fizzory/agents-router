#!/bin/sh
# Explicit, consented preparation only; builds call check_app_tools.sh instead.
set -eu
if [ "$#" -ne 1 ] || [ "$1" != '--yes' ]; then
    echo 'Tool installation requires explicit consent: scripts/prepare_app_tools.sh --yes' >&2
    exit 2
fi
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
export CARGO_HOME="$repo/build/cargo-home"
export CARGO_TARGET_DIR="$repo/build/tools-target"
mkdir -p "$CARGO_HOME" "$CARGO_TARGET_DIR"
# Pinned Node (scripts/node-runtime.lock, SHA256-checked) and npm ci for macos/ui.
python3 "$repo/scripts/app_node.py" install
if [ "$("$repo/build/tools/bin/cargo-tauri" --version 2>/dev/null)" = 'tauri-cli 2.12.4' ]; then
    echo 'tauri-cli 2.12.4 already in build/tools.'
    exit 0
fi
exec cargo install tauri-cli --version '=2.12.4' --locked --root "$repo/build/tools"
