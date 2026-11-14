#!/usr/bin/env python3
"""Build and run the CI WebKit self-test, with exactly the same checks locally.

No signing secrets, installed agt, provider CLI, or real home is used. Diagnostic
files survive under a fresh mode-0700 RUNNER_TEMP directory, printed on failure.
"""
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile

from app_env import scratch_env, without_agt

ROOT = Path(__file__).resolve().parents[1]
PROBES = {"css-has", "css-color-mix", "css-container", "css-custom-property", "css-grid",
          "js-es-modules", "js-resize-observer", "js-structured-clone", "js-array-at", "js-queue-microtask"}
PANES = {"general", "roles", "accounts", "sharing", "activity"}


def validate_report(report):
    """Fail closed on partial, stale, malformed, or merely truthy reports."""
    if report.get("schema_version") != 1 or report.get("pass") is not True:
        raise ValueError("report is not a passing schema-1 report")
    if (report.get("architecture") != "arm64" or not all(isinstance(report.get(k), str) and report[k]
            for k in ("os_version", "os_build", "webkit_cf_bundle_version", "baseline_macos_14_0"))
            or report.get("error") is not None or report.get("windows_shown") is not True
            or report.get("loads") != {"popover": 1, "settings": 1}):
        raise ValueError("missing host, visibility, or single-load evidence")
    pages = report.get("pages", {})
    if set(pages) != {"popover", "settings"}:
        raise ValueError("both production pages must report")
    for name, page in pages.items():
        if (page.get("ready") is not True or page.get("errors") != []
                or page.get("fixture", {}).get("ok") is not True
                or set(page.get("probes", {})) != PROBES
                or any(v is not True for v in page["probes"].values())):
            raise ValueError(f"{name}: missing readiness, fixtures, probes, or clean error capture")
    if pages["popover"].get("layout", {}).get("ok") is not True:
        raise ValueError("popover has no layout")
    panes = pages["settings"].get("panes", {})
    if set(panes) != PANES:
        raise ValueError("missing Settings panes")
    for name, pane in panes.items():
        if (any(pane.get(k) is not True for k in ("ok", "rows", "fixture"))
                or any(not isinstance(pane.get(k), (int, float)) or pane[k] <= 0 for k in ("width", "height", "count"))):
            raise ValueError(f"{name}: missing fixture content or non-zero layout")


def build_env(base=None):
    """The build's environment: no TAURI_* signing input and no AGT_* folder from the caller."""
    env = {k: v for k, v in without_agt(base).items() if not k.startswith("TAURI_")}
    env.update(CARGO_HOME=str(ROOT / "build/cargo-home"), CARGO_TARGET_DIR=str(ROOT / "build/app-target"),
               RUSTUP_AUTO_INSTALL="0", MACOSX_DEPLOYMENT_TARGET="14.0", CI="true")
    return env


def child_env(scratch, temp, fake, report):
    """The app's whole environment, built from scratch paths alone."""
    home = scratch / "home"
    # Not home/.agents-router: a test build refuses the release's agt home.
    agt_home = home / "agt-home"
    return scratch_env({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "CFFIXED_USER_HOME": home,
           "RUNNER_TEMP": temp, "TMPDIR": scratch / "tmp", "AGT_HOME": agt_home,
           "AGT_CONFIG_DIR": agt_home, "AGT_APP_SUPPORT": scratch / "support",
           "AGT_BIN_DIR": scratch / "bin", "AGT_APPLICATIONS_DIRS": scratch / "Applications",
           "CLAUDE_CONFIG_DIR": home / ".claude", "CODEX_HOME": home / ".codex",
           "GEMINI_CLI_HOME": home / ".gemini", "AGT_APP_SELFTEST": report,
           "AGT_APP_TEST_LAUNCHER": fake, "AGT_APP_TEST_USAGE_FIXTURE": "usage-popover.json",
           "AGT_APP_TEST_CALLS": scratch / "calls.jsonl"}, base={})


def main():
    if sys.argv[1:]:
        raise ValueError("Usage: python3 scripts/run_app_selftest.py")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("self-test requires an Apple Silicon macOS desktop")
    temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="agt-app-selftest-", dir=temp))
    scratch.chmod(0o700)
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"diagnostics={scratch}\n")
    print(f"Self-test diagnostics: {scratch}", flush=True)
    build = build_env()
    with (scratch / "build.log").open("w") as log:
        subprocess.run([str(ROOT / "scripts/check_app_tools.sh")], env=build, check=True, stdout=log, stderr=log)
        subprocess.run([str(ROOT / "build/tools/bin/cargo-tauri"), "build",
                        "--features", "ci-selftest", "--target", "aarch64-apple-darwin", "--bundles", "app",
                        "--", "--locked", "--no-default-features"], cwd=ROOT / "macos/src-tauri", env=build, check=True,
                       stdout=log, stderr=log)
    agt_home = scratch / "home/agt-home"
    for path in (agt_home / "state", agt_home / "jobs", scratch / "tmp", scratch / "support", scratch / "bin"):
        path.mkdir(parents=True, exist_ok=True)
    fake = scratch / "fake-agt"
    # sh quoting is supplied by shlex; this launcher never searches PATH.
    import shlex
    fake.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'scripts/fake_app_agt.py'))} \"$@\"\n")
    fake.chmod(0o700)
    report = scratch / "report.json"
    env = child_env(scratch, temp, fake, report)
    # The base config names this ci-selftest build "Agents Router Test.app"; it never replaces a release bundle.
    binary = ROOT / "build/app-target/aarch64-apple-darwin/release/bundle/macos/Agents Router Test.app/Contents/MacOS/agents-router"
    with (scratch / "native.log").open("w") as log:
        process = subprocess.Popen([str(binary)], cwd=scratch, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError(f"90 s external deadline; diagnostics: {scratch}") from None
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    if code != 0:
        raise ValueError(f"self-test exited {code}; diagnostics: {scratch}")
    if report.is_symlink() or not report.is_file():
        raise ValueError(f"missing Rust JSON report; diagnostics: {scratch}")
    data = json.loads(report.read_text())
    validate_report(data)
    print(f"ci-selftest PASS: {report}")
    print(f"macOS {data['os_version']} ({data['os_build']}); arch={data['architecture']}; WebKit CFBundleVersion={data['webkit_cf_bundle_version']}")
    print(data["baseline_macos_14_0"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        sys.exit(f"App self-test failed: {error}")
