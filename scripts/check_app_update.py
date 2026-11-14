#!/usr/bin/env python3
"""Build and exercise a real signed N -> N+1 update, entirely under build/.

Uses the prepared tools, an ephemeral key and a localhost-only config. No real
provider CLI or account is used. The copied Cargo/package versions alone change.
Requires an Apple Silicon Mac with an active desktop session.
"""
import contextlib
from functools import partial
import http.server
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import quote

import build_app
from app_env import scratch_env
from app_updater import TEST_IDENTIFIER, TEST_NAME

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
LOG = BUILD / "app-update-check.log"


def next_test_version(version):
    """The update the rehearsal installs: the next patch as a prerelease (0.17.1 -> 0.17.2-test)."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ValueError(f"{version!r} is not a release version (X.Y.Z)")
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{int(patch) + 1}-test"


def child_env(scratch, toolchain, base=None):
    """The build's and the app's environment: every agt folder in `scratch`, none from the caller."""
    home = scratch / "home"
    return scratch_env({"CARGO_HOME": BUILD / "cargo-home",
               "CARGO_TARGET_DIR": BUILD / "update-target", "CI": "true",
               "RUSTUP_AUTO_INSTALL": "0", "MACOSX_DEPLOYMENT_TARGET": "14.0",
               "PIP_CACHE_DIR": BUILD / "pip-cache", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
               "PATH": os.pathsep.join([str(BUILD / "tools/bin"), str(Path(toolchain) / "bin"),
                                        (os.environ if base is None else base)["PATH"]]),
               "HOME": home, "AGT_APP_SUPPORT": scratch / "support",
               "AGT_BIN_DIR": scratch / "public-bin", "AGT_HOME": home / "agt-home",
               "AGT_CONFIG_DIR": home / "agt-home", "CLAUDE_CONFIG_DIR": home / ".claude",
               "CODEX_HOME": home / ".codex", "AGT_APPLICATIONS_DIRS": scratch / "Applications"}, base)


def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("Updater rehearsal requires an Apple Silicon Mac desktop.")
    subprocess.run([str(ROOT / "scripts/check_app_tools.sh")], check=True)
    BUILD.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="update-check-", dir=BUILD) as directory, LOG.open("w") as log:
        scratch = Path(directory).resolve()
        (scratch / "test-only").touch()
        home = scratch / "home"
        # Not home/.agents-router: a test build refuses the release's agt home.
        for path in (home / "agt-home/state", home / "agt-home/jobs", home / ".claude",
                     home / ".codex", scratch / "Applications", scratch / "support"):
            path.mkdir(parents=True, exist_ok=True)
        # This is an installed baseline, with onboarding already completed.
        (scratch / "support/install.json").write_text(json.dumps(dict(previous=None, foreign=None, replaced=False, setup_unknown=False, setup_pending=False, first_complete=True)))
        toolchain = subprocess.run(["rustc", "--print", "sysroot"], check=True, text=True, capture_output=True).stdout.strip()
        env = child_env(scratch, toolchain)
        for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "TAURI_SIGNING_PUBLIC_KEY"):
            env.pop(key, None)
        def run(*args, cwd=None, capture=False, env=env):
            result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                                    stdout=subprocess.PIPE if capture else log, stderr=log,
                                    text=True, check=True)
            return result.stdout
        build_app.run = run
        lock = tomllib.loads((ROOT / "scripts/python-runtime.lock").read_text())
        print(f"Building isolated updater rehearsal; diagnostics: {LOG}", flush=True)
        with contextlib.redirect_stdout(log):
            current = build_app.versions()
            runtime = build_app.stage(current, lock)
        following = next_test_version(current)
        # The self-test reads both versions from here (update_app.rs spells none).
        (scratch / "versions.json").write_text(json.dumps({"from": current, "to": following}))
        key = scratch / "throwaway.key"
        run(BUILD / "tools/bin/cargo-tauri", "signer", "generate", "--ci", "-p", "", "-w", key, capture=True)
        key.chmod(0o600)
        public = key.with_suffix(".key.pub").read_text().strip()
        source = scratch / "source"
        shutil.copytree(ROOT / "macos", source, ignore=shutil.ignore_patterns("target", "gen"))
        cargo = source / "src-tauri"
        # A test-named bundle with its own identifier: a running development app
        # can't receive this test's single-instance activation or claim its relaunch.
        identifier = f"{TEST_IDENTIFIER}.update-{os.getpid()}"
        config = json.loads((cargo / "tauri.conf.json").read_text())
        config["productName"] = TEST_NAME
        config["identifier"] = identifier
        (cargo / "tauri.conf.json").write_text(json.dumps(config))
        feed = scratch / "feed"
        feed.mkdir()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(feed)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        app = scratch / f"Applications/{TEST_NAME}.app"
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            for version in (current, following):
                current_runtime = scratch / f"runtime-{version}"
                shutil.copytree(runtime, current_runtime, symlinks=True)
                if version != current:
                    init = current_runtime / "lib/python3.13/site-packages/agents_router/__init__.py"
                    init.write_text(init.read_text().replace(f'"{current}"', f'"{following}"'))
                    manifest_path = current_runtime / "agt-bundle.json"
                    manifest = json.loads(manifest_path.read_text())
                    manifest.update(app_version=version, runtime_version=version)
                    manifest_path.write_text(json.dumps(manifest))
                    plugin_path = current_runtime / "plugin/.claude-plugin/plugin.json"
                    plugin = json.loads(plugin_path.read_text()); plugin["version"] = version
                    plugin_path.write_text(json.dumps(plugin))
                    run(current_runtime / "bin/python3", "-I", "-B", "-m", "compileall", "-q",
                        "--invalidation-mode", "checked-hash", "-s", current_runtime, current_runtime / "lib/python3.13")
                    with contextlib.redirect_stdout(log):
                        build_app.sign_runtime(current_runtime)
                    manifest_path = cargo / "Cargo.toml"
                    manifest_path.write_text(manifest_path.read_text().replace(f'version = "{current}"', f'version = "{following}"', 1))
                    manifest_path = cargo / "Cargo.lock"
                    manifest_path.write_text(manifest_path.read_text().replace(f'name = "agents-router-app"\nversion = "{current}"', f'name = "agents-router-app"\nversion = "{following}"'))
                overlay = scratch / f"{version}.test.conf.json"
                overlay.write_text(json.dumps({"version": version,
                    "bundle": {"resources": {str(current_runtime): "runtime/"}, "createUpdaterArtifacts": True},
                    "plugins": {"updater": {"pubkey": public, "endpoints": [endpoint + "/latest.json"],
                                            "dangerousInsecureTransportProtocol": True}}}))
                signed_env = {**env, "TAURI_SIGNING_PRIVATE_KEY": str(key), "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": ""}
                run("cargo", "tauri", "build", "--debug", "--bundles", "app", "--config", overlay, "--", "--locked", cwd=cargo, env=signed_env)
                bundle = Path(env["CARGO_TARGET_DIR"]) / f"debug/bundle/macos/{TEST_NAME}.app"
                run("codesign", "--verify", "--strict", bundle)
                if version == current:
                    shutil.copytree(bundle, app, symlinks=True)
                else:
                    archive = bundle.with_suffix(".app.tar.gz")
                    shutil.copy2(archive, feed / archive.name)
                    metadata = {"version": version, "notes": "Isolated updater rehearsal", "pub_date": "2026-11-10T00:00:00Z",
                                "platforms": {"darwin-aarch64": {"url": f"{endpoint}/{quote(archive.name)}",
                                  "signature": Path(str(archive) + ".sig").read_text().strip()}}}
                    (feed / "latest.json").write_text(json.dumps(metadata))
            # Direct launch keeps all initial writes isolated; the debug harness
            # reestablishes that isolation from argv on the LaunchServices restart.
            process = subprocess.Popen([str(app / "Contents/MacOS/agents-router"), "--self-test-update", str(scratch)],
                                       cwd=home, env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 120
                while not (scratch / "relaunch.json").is_file() and not (scratch / "failure.txt").is_file() and time.monotonic() < deadline:
                    time.sleep(0.2)
                if not (scratch / "relaunch.json").is_file():
                    raise RuntimeError(f"Updater rehearsal did not relaunch: {(scratch / 'failure.txt').read_text() if (scratch / 'failure.txt').exists() else 'timeout'}; see {LOG}")
                report = json.loads((scratch / "relaunch.json").read_text())
                assert report["version"] == following, report
                with (app / "Contents/Info.plist").open("rb") as file:
                    assert plistlib.load(file)["CFBundleShortVersionString"] == following
                print(f"OK: relaunched app reports {following}")
                attrs = run("xattr", "-lr", app, capture=True)
                assert "com.apple.quarantine" not in attrs
                print("OK: updated app has no quarantine attribute")
                run("codesign", "--verify", "--strict", app)
                print("OK: updated app passes codesign --verify --strict")
                assert report["runtime"] == following and report["refresh_record"], report
                assert (scratch / "refresh-calls").read_text().splitlines() == [following]
                installed_version = run(scratch / "support/bin/agt", "--version", capture=True).strip()
                assert installed_version.split()[-1] == following, installed_version
                print(f"OK: CLI runtime refreshed to {following}; bundled agt update ran exactly once")
                process.wait(timeout=15)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                # The relaunched instance belongs to LaunchServices. Kill only
                # processes whose executable is inside this exact scratch app.
                listing = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True).stdout
                for line in listing.splitlines():
                    match = re.match(r"\s*(\d+)\s+(.+)", line)
                    if match and match[2].startswith(str(app / "Contents/MacOS/agents-router")):
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(int(match[1]), signal.SIGTERM)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    main()
