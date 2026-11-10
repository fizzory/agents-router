#!/bin/sh
# One release build on the maintainer's Mac and CI. Installs no host tools.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
export CARGO_HOME="$repo/build/cargo-home"
export RUSTUP_AUTO_INSTALL=0
# Keep this before Python, downloads, mkdir, or any other staging operation.
"$repo/scripts/check_app_tools.sh"
exec python3 "$repo/scripts/build_app.py" "$@"
