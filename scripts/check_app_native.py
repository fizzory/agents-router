#!/usr/bin/env python3
"""Run the debug NSPanel/wake smoke check against golden fixtures, then quit.

Build first with cargo-tauri build --debug --bundles app -- --locked.
Requires a macOS desktop session; never runs the real agt or an agent CLI.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "macos/src-tauri/target/debug/bundle/macos/Agents Router.app/Contents/MacOS/agents-router"
LOG = ROOT / "build/app-native-self-test.log"


def main():
    if not BINARY.is_file():
        raise SystemExit("Build the debug app bundle first.")
    with tempfile.TemporaryDirectory(prefix="agt-app-native-") as folder:
        scratch = Path(folder)
        home = scratch / "home"
        agt_home = home / ".agents-router"
        for path in (agt_home / "state", agt_home / "jobs"):
            path.mkdir(parents=True)
        calls = scratch / "calls.jsonl"
        env = dict(os.environ)
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        env.update(HOME=str(home), AGT_HOME=str(agt_home), AGT_CONFIG_DIR=str(agt_home),
                   AGT_APP_TEST_LAUNCHER=str(ROOT / "scripts/fake_app_agt.py"),
                   AGT_APP_TEST_CALLS=str(calls), AGT_APP_TEST_UPDATE_VERSION="0.17.1")
        # Direct execution preserves the isolated environment and captures native stderr.
        with LOG.open("w") as stream:
            try:
                result = subprocess.run([str(BINARY), "--self-test-wake"], env=env,
                                        cwd=scratch, stdout=stream, stderr=stream, timeout=30)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"Native self-test timed out (child killed); see {LOG}") from None
        text = LOG.read_text()
        sizes = re.findall(r"resized-to-content requested=(\d+) height=(\d+) cap=(\d+) top=(-?\d+) mode=(\w+)", text)
        checks = [result.returncode == 0,
                  "class=AgtPopoverPanel NSPanel" in text,
                  text.count("self-test posted NSWorkspaceDidWakeNotification") == 1,
                  text.count("usage read reason=debounced refresh=false") == 1,
                  "hidden reason=blur; never closed; webview retained" in text,
                  "hidden reason=Esc; never closed; webview retained" in text,
                  any(int(requested) > 80 and int(height) == min(int(requested), int(cap))
                      for requested, height, cap, _top, _mode in sizes),
                  ("popover reduce-motion=true" in text or
                   any(mode == "animate" for _requested, _height, _cap, _top, mode in sizes)),
                  "self-test complete; quitting" in text]
        requests = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        checks += [bool(requests), requests.count(["usage", "--json"]) == 1,
                   all(argv in (["usage", "--json"], ["usage", "--json", "--refresh"]) for argv in requests)]
        for line in text.splitlines():
            if any(key in line for key in ("class=AgtPopoverPanel", "posted NSWorkspace", "reason=debounced", "hidden reason=blur", "hidden reason=Esc", "popover resized-to-content", "self-test complete")):
                print(line)
        if not all(checks):
            raise SystemExit(f"Native self-test FAILED ({checks}); see {LOG}")
        print(f"Native self-test OK ({len(requests)} fixture reads; exactly one debounced read; app quit)")


if __name__ == "__main__":
    main()
