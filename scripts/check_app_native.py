#!/usr/bin/env python3
"""Run the debug NSPanel/wake smoke check against golden fixtures, then quit.

Build first with cargo-tauri build --debug --bundles app -- --locked.
Requires a macOS desktop session; never runs the real agt or an agent CLI.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile

from app_env import scratch_env

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "macos/src-tauri/target/debug/bundle/macos/Agents Router Test.app/Contents/MacOS/agents-router"
LOG = ROOT / "build/app-native-self-test.log"


def child_env(scratch, calls, base=None):
    """The app's environment: every agt folder in `scratch`, none from the caller."""
    home = scratch / "home"
    # Not home/.agents-router: a test build refuses the release's agt home.
    agt_home = home / "agt-home"
    return scratch_env({"HOME": home, "CFFIXED_USER_HOME": home,
                        "AGT_APP_SUPPORT": scratch / "support", "AGT_BIN_DIR": scratch / "bin",
                        "AGT_HOME": agt_home, "AGT_CONFIG_DIR": agt_home,
                        "AGT_APPLICATIONS_DIRS": scratch / "Applications",
                        "AGT_APP_TEST_LAUNCHER": ROOT / "scripts/fake_app_agt.py",
                        "AGT_APP_TEST_CALLS": calls, "AGT_APP_TEST_UPDATE_VERSION": "0.17.1"}, base)


def main():
    if not BINARY.is_file():
        raise SystemExit("Build the debug app bundle first.")
    with tempfile.TemporaryDirectory(prefix="agt-app-native-") as folder:
        scratch = Path(folder).resolve()  # the app logs physical paths
        calls = scratch / "calls.jsonl"
        env = child_env(scratch, calls)
        agt_home = Path(env["AGT_HOME"])
        for path in (agt_home / "state", agt_home / "jobs"):
            path.mkdir(parents=True)
        # Direct execution preserves the isolated environment and captures native stderr.
        with LOG.open("w") as stream:
            try:
                result = subprocess.run([str(BINARY), "--self-test-wake"], env=env,
                                        cwd=scratch, stdout=stream, stderr=stream, timeout=30)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"Native self-test timed out (child killed); see {LOG}") from None
        text = LOG.read_text()
        def callback_json(marker):
            match = re.search(re.escape(marker) + r"=(.+)", text)
            if not match:
                return None
            value = json.loads(match[1])
            return json.loads(value) if isinstance(value, str) else value

        # Settings opens at the account target: one row per fixture account.
        locked = json.loads((ROOT / "tests/fixtures/app/accounts-list-locked.json").read_text())
        expected_rows = sum(len(runtime["accounts"]) for runtime in locked["runtimes"])
        # The fake agt's default usage fixture: every account is one popover row.
        usage = json.loads((ROOT / "tests/fixtures/app/usage.json").read_text())
        expected_accounts = sum(len(provider["accounts"]) for provider in usage["providers"])
        popover = callback_json("self-test popover-content") or {}
        sizes = re.findall(r"resized-to-content requested=(\d+) height=(\d+) cap=(\d+) top=(-?\d+) mode=(\w+)", text)
        checks = [result.returncode == 0,
                  # A test build logs its folders once and keeps them out of the release's.
                  text.count("test build paths app_support=") == 1,
                  f"agt_home={agt_home} (AGT_HOME)" in text,
                  "class=AgtPopoverPanel NSPanel" in text,
                  text.count("self-test posted NSWorkspaceDidWakeNotification") == 1,
                  text.count("usage read reason=debounced refresh=false") == 1,
                  "hidden reason=blur; never closed; webview retained" in text,
                  "hidden reason=Esc; never closed; webview retained" in text,
                  any(int(requested) > 80 and int(height) == min(int(requested), int(cap))
                      for requested, height, cap, _top, _mode in sizes),
                  ("popover reduce-motion=true" in text or
                   any(mode == "animate" for _requested, _height, _cap, _top, mode in sizes)),
                  popover.get("rows") == expected_accounts,
                  callback_json("self-test settings-content") == {"pane":"accounts", "rows":expected_rows},
                  "settings hidden activation=Accessory; webview retained" in text,
                  "single-instance callback on main thread" in text,
                  "self-test second-launch-success=true" in text,
                  "self-test settings-reopened-visible=true" in text,
                  callback_json("self-test settings-retained") == {"pane":"general", "retained":"yes"},
                  "self-test complete; quitting" in text]
        requests = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        allowed_requests = [["usage", "--json"], ["usage", "--json", "--refresh"],
                            ["usage", "--probe-codex", "--json"],
                            ["config", "show", "--all", "--json"],
                            ["accounts", "list", "--cached-only", "--json"],
                            ["share", "status", "--user-only", "--json"]]
        # Opening the popover checks enabled fixture accounts with no cached login check.
        allowed_requests.extend(["accounts", "check", account["name"], "--runtime", runtime["runtime"], "--json"]
                                for runtime in locked["runtimes"] for account in runtime["accounts"]
                                if account["enabled"] and account["login_check"] is None)
        # The client runs one usage read at a time. The reads asked for meanwhile share the next one,
        # which is a refresh when any of them was, so the plain `usage --json` children number at most
        # the plain reads logged. The wake's own debounced read is the log line counted above.
        plain_usage_reads = len(re.findall(r"\busage read reason=\S+ refresh=false$", text, re.MULTILINE))
        checks += [bool(requests), 1 <= requests.count(["usage", "--json"]) <= plain_usage_reads,
                   all(argv in allowed_requests for argv in requests)]
        for line in text.splitlines():
            if any(key in line for key in ("class=AgtPopoverPanel", "posted NSWorkspace", "reason=debounced", "hidden reason=blur", "hidden reason=Esc", "popover resized-to-content", "self-test complete")):
                print(line)
        if not all(checks):
            raise SystemExit(f"Native self-test FAILED ({checks}); see {LOG}")
        print(f"Native self-test OK ({len(requests)} fixture reads; exactly one debounced read; app quit)")


if __name__ == "__main__":
    main()
