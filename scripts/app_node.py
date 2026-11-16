#!/usr/bin/env python3
"""The app UI's pinned Node and npm packages, all below build/.

Builds only find them (`require`). `install` downloads Node per
scripts/node-runtime.lock (SHA256-verified) and runs `npm ci --ignore-scripts`;
only scripts/prepare_app_tools.sh --yes calls it, after the person's consent.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
UI = ROOT / "macos/ui"
LOCK = ROOT / "scripts/node-runtime.lock"
NODE_HOME = BUILD / "node"              # the tarball's top folder, stripped
NPM_ROOT = BUILD / "ui-npm"             # npm ci runs here, on copies of the manifests
MODULES = NPM_ROOT / "node_modules"     # macos/ui/node_modules links here
LINK = UI / "node_modules"
NPM_CACHE = BUILD / "npm-cache"
NPMRC = BUILD / "npmrc"                 # empty user and global npmrc: keep ~/.npmrc out
NPMRC_GLOBAL = BUILD / "npmrc-global"
PREPARE = "After consent, run scripts/prepare_app_tools.sh --yes."


class ToolsMissing(Exception):
    pass


def lock():
    return tomllib.loads(LOCK.read_text(encoding="utf-8"))["node"]


def node():
    return NODE_HOME / "bin/node"


def env(base=None):
    """The pinned Node first on PATH; npm's cache and config below build/."""
    result = dict(os.environ if base is None else base)
    result.update(PATH=os.pathsep.join([str(NODE_HOME / "bin"), result.get("PATH", "/usr/bin:/bin")]),
                  npm_config_cache=str(NPM_CACHE), npm_config_userconfig=str(NPMRC),
                  npm_config_globalconfig=str(NPMRC_GLOBAL), npm_config_update_notifier="false",
                  npm_config_fund="false", npm_config_audit="false",
                  npm_config_registry="https://registry.npmjs.org/")
    return result


def node_version():
    try:
        return subprocess.run([str(node()), "--version"], check=True, text=True,
                              capture_output=True, env=env()).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def require():
    """Fail clearly when the pinned Node or the UI's packages are missing. Never installs."""
    expected = "v" + lock()["version"]
    found = node_version()
    if found != expected:
        raise ToolsMissing(f"App UI build needs the pinned Node {expected} in build/node "
                           f"(found {found or 'none'}). {PREPARE}")
    if not (LINK.is_symlink() and LINK.resolve() == MODULES.resolve()
            and (MODULES / ".package-lock.json").is_file()):
        raise ToolsMissing(f"App UI build needs macos/ui/node_modules -> build/ui-npm/node_modules from npm ci. {PREPARE}")
    return node()


def install_node():
    import build_app  # SHA256-verified download into build/downloads
    pin = lock()
    if node_version() == "v" + pin["version"]:
        print(f"Node {pin['version']} already in build/node.", flush=True)
        return
    archive = build_app.download(pin["url"], pin["sha256"])
    staging = BUILD / "node.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(archive) as file:
        file.extractall(staging, filter="data")
    top, = staging.iterdir()
    shutil.rmtree(NODE_HOME, ignore_errors=True)
    top.rename(NODE_HOME)
    staging.rmdir()
    if node_version() != "v" + pin["version"]:
        raise ToolsMissing(f"Extracted Node reports {node_version()}, expected v{pin['version']}")
    print(f"Node {pin['version']} installed in build/node.", flush=True)


def install_packages():
    """npm ci in build/ui-npm, then link macos/ui/node_modules to its node_modules.

    npm replaces a linked node_modules with a real folder, so it never runs in
    macos/ui itself; it gets copies of the two committed manifests instead.
    """
    NPMRC.touch()
    NPMRC_GLOBAL.touch()
    NPM_ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(UI / name, NPM_ROOT / name)
    npm = NODE_HOME / "lib/node_modules/npm/bin/npm-cli.js"
    subprocess.run([str(node()), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                   cwd=NPM_ROOT, env=env(), check=True)
    if LINK.is_symlink() or LINK.is_file():
        LINK.unlink()
    elif LINK.exists():
        shutil.rmtree(LINK)  # a plain install made in macos/ui; the link replaces it
    LINK.symlink_to(os.path.relpath(MODULES, UI))
    print("UI packages installed in build/ui-npm (npm ci --ignore-scripts).", flush=True)


def main():
    if sys.argv[1:] != ["install"]:
        raise SystemExit("Usage: scripts/prepare_app_tools.sh --yes (runs app_node.py install)")
    BUILD.mkdir(exist_ok=True)
    install_node()
    install_packages()
    require()


if __name__ == "__main__":
    try:
        main()
    except (ToolsMissing, ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"App UI tools failed: {error}")
