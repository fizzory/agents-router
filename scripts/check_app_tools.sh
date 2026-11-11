#!/bin/sh
# Read-only prerequisite check. Never installs or updates tools.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v rustc >/dev/null 2>&1; then
    echo 'App build needs rustc >= 1.90; rustc is missing. Install Rust separately, then retry.' >&2
    exit 1
fi
version=$(rustc --version) || { echo 'Could not read rustc version.' >&2; exit 1; }
version=${version#rustc }
version=${version%% *}
major=${version%%.*}
minor=${version#*.}
minor=${minor%%.*}
case "$major:$minor" in
    *[!0-9:]*|:*|*:) echo "Could not parse rustc version: $version" >&2; exit 1 ;;
esac
if [ "$major" -lt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -lt 90 ]; }; then
    echo "App build needs rustc >= 1.90; found $version. Update Rust separately, then retry." >&2
    exit 1
fi
if [ ! -x "$repo/build/tools/bin/cargo-tauri" ]; then
    echo 'App build needs build/tools/bin/cargo-tauri. After consent, run scripts/prepare_app_tools.sh --yes.' >&2
    exit 1
fi
echo "App tools ready (rustc $version; repo-local cargo-tauri)."
