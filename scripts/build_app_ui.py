#!/usr/bin/env python3
"""Build the app's web UI (macos/ui) into build/ui-dist with the pinned Node.

tauri.conf.json runs this as beforeBuildCommand; run it yourself before
`cargo test`. It never installs anything: without the pinned Node or the npm
packages it stops and names scripts/prepare_app_tools.sh --yes.

  python3 scripts/build_app_ui.py              # vite build, then check the output
  python3 scripts/build_app_ui.py --typecheck  # tsc --noEmit
  python3 scripts/build_app_ui.py --test       # vitest run: the React component tests (jsdom)
  python3 scripts/build_app_ui.py --watch      # vite build --watch (dev loop)
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

import app_node

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "build/ui-dist"
PAGES = ("index.html", "popover.html", "settings.html")
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
REFERENCE = re.compile(r"""\s(?:src|href)\s*=\s*["']([^"']*)["']""", re.I)
NONLOCAL = re.compile(r"^(?:[a-z][\w+.-]*:|//)", re.I)
CSS_URL = re.compile(r"""url\(\s*["']?([^"')\s]+)""", re.I)


def check_dist(dist):
    """What the CSP (script-src/style-src 'self', no data:) and Tauri need from the build."""
    errors = []
    for page in PAGES:
        if not (dist / page).is_file():
            errors.append(f"{page} is missing")
    for path in sorted(dist.rglob("*")):
        relative = path.relative_to(dist)
        if {"node_modules", "src-tauri"} & set(relative.parts):
            errors.append(f"{relative}: build output must not contain node_modules or src-tauri")
        if not path.is_file():
            continue
        if path.suffix == ".html":
            text = path.read_text(encoding="utf-8")
            for attributes, body in SCRIPT.findall(text):
                if not re.search(r"\ssrc\s*=", attributes) or body.strip():
                    errors.append(f"{relative}: inline <script>")
            if re.search(r"<style\b", text, re.I):
                errors.append(f"{relative}: inline <style>")
            if re.search(r"""<[^>]*\sstyle\s*=""", text, re.I):
                errors.append(f"{relative}: style= attribute")
            if re.search(r"""<[^>]*\son[a-z]+\s*=""", text, re.I):
                errors.append(f"{relative}: inline event handler")
            references = REFERENCE.findall(text)
        elif path.suffix == ".css":
            references = CSS_URL.findall(path.read_text(encoding="utf-8"))
        else:
            continue
        for reference in references:
            if reference.startswith("#"):
                continue
            if NONLOCAL.match(reference):
                errors.append(f"{relative}: non-local reference {reference}")
                continue
            local = reference.split("#", 1)[0].split("?", 1)[0]
            target = (dist / local.lstrip("/")) if local.startswith("/") else (path.parent / local)
            if not target.resolve().is_relative_to(dist.resolve()) or not target.is_file():
                errors.append(f"{relative}: {reference} is not a file in the build")
    return errors


def run(*args, watch=False):
    node = app_node.require()
    command = [str(node), *args]
    if watch:
        command.append("--watch")
    subprocess.run(command, cwd=app_node.UI, env=app_node.env(), check=True)


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--typecheck"], ["--test"], ["--watch"]):
        raise ValueError("Usage: python3 scripts/build_app_ui.py [--typecheck | --test | --watch]")
    modules = app_node.LINK
    if arguments == ["--typecheck"]:
        run(str(modules / "typescript/bin/tsc"), "--noEmit", "-p", str(app_node.UI))
        print("UI typecheck OK (tsc --noEmit).", flush=True)
        return
    if arguments == ["--test"]:
        run(str(modules / "vitest/vitest.mjs"), "run", "--config", str(app_node.UI / "vitest.config.ts"))
        print("UI component tests OK (vitest run).", flush=True)
        return
    run(str(modules / "vite/bin/vite.js"), "build", "--config", str(app_node.UI / "vite.config.ts"),
        watch=arguments == ["--watch"])
    errors = check_dist(DIST)
    if errors:
        raise ValueError("UI build output breaks the app's rules:\n  " + "\n  ".join(errors))
    print(f"UI built and checked: {DIST.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (app_node.ToolsMissing, ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"App UI build failed: {error}")
