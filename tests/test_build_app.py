"""The release entry point must fail before downloads or runtime staging."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import base64
import unittest
from unittest import mock

import helpers  # noqa: F401 (isolates all real homes and provider CLIs)

ROOT = Path(__file__).resolve().parents[1]


def build_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("build_app", ROOT / "scripts/build_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildAppTests(unittest.TestCase):
    def test_placeholder_and_override_cannot_publish_and_metadata_is_complete(self):
        module = build_module()
        config = json.loads((ROOT / "macos/src-tauri/tauri.conf.json").read_text())
        config["plugins"]["updater"]["pubkey"] = "AGT_UPDATER_PUBLIC_KEY_REQUIRED_BEFORE_RELEASE"
        with self.assertRaisesRegex(ValueError, "real public key"):
            module.validate_release(config)
        # Structurally valid public-key fixture, never an actual signing key.
        config["plugins"]["updater"]["pubkey"] = base64.b64encode(
            b"untrusted comment: minisign public key\n" + base64.b64encode(b"Ed" + bytes(40)) + b"\n").decode()
        module.validate_release(config)
        metadata = module.latest("0.17.1", "signature\n", "Release notes")
        self.assertEqual(set(metadata), {"version", "notes", "pub_date", "platforms"})
        self.assertEqual(metadata["version"], "0.17.1")
        self.assertEqual(metadata["notes"], "Release notes")
        self.assertTrue(metadata["pub_date"].endswith("Z"))
        self.assertEqual(metadata["platforms"]["darwin-aarch64"], {
            "url": "https://github.com/fizzory/agents-router/releases/download/v0.17.1/Agents-Router-0.17.1-arm64.app.tar.gz",
            "signature": "signature"})
        self.assertEqual(module.archive_name("0.17.1"), "Agents-Router-0.17.1-arm64.app.tar.gz")
        with self.assertRaises(ValueError):
            module.archive_name("0.17.1/unsafe")

    def test_selftest_validator_rejects_partial_and_false_positive_reports(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from run_app_selftest import validate_report, PROBES, PANES
        import copy
        pane = {"width": 400, "height": 200, "count": 1, "ok": True, "fixture": True, "rows": True}
        page = {"ready": True, "errors": [], "fixture": {"ok": True}, "probes": dict.fromkeys(PROBES, True), "layout": {"ok": True}}
        report = {"schema_version": 1, "pass": True, "os_version": "26.6.2", "os_build": "25G83", "architecture": "arm64",
                  "webkit_cf_bundle_version": "123", "baseline_macos_14_0": "unverified", "error": None, "windows_shown": True,
                  "loads": {"popover": 1, "settings": 1}, "pages": {"popover": copy.deepcopy(page), "settings": copy.deepcopy(page)}}
        report["pages"]["settings"]["panes"] = {name: dict(pane) for name in PANES}
        validate_report(report)
        for mutate in (lambda r: r.update({"pass": False}), lambda r: r["loads"].update(settings=2),
                       lambda r: r["pages"]["popover"].update(errors=[{"kind":"csp"}]),
                       lambda r: r["pages"]["settings"]["panes"]["roles"].update(width=0),
                       lambda r: r["pages"]["settings"]["probes"].pop("css-has")):
            broken = copy.deepcopy(report)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate_report(broken)

    def test_publish_guard_stops_before_staging_with_a_placeholder_or_override(self):
        module = build_module()
        with tempfile.TemporaryDirectory(prefix="agt-key-gate-") as folder:
            root = Path(folder)
            (root / "tauri.conf.json").write_text(json.dumps({"plugins": {"updater": {
                "pubkey": "AGT_UPDATER_PUBLIC_KEY_REQUIRED_BEFORE_RELEASE"}}}))
            for extra in ({}, {"TAURI_SIGNING_PUBLIC_KEY": "throwaway"}):
                with self.subTest(extra=extra), mock.patch.dict(os.environ, {
                    "TAURI_SIGNING_PRIVATE_KEY": "never-read-placeholder", **extra,
                }, clear=True), mock.patch.object(module.sys, "argv", ["build_app"]), \
                        mock.patch.object(module.platform, "system", return_value="Darwin"), \
                        mock.patch.object(module.platform, "machine", return_value="arm64"), \
                        mock.patch.object(module, "versions", return_value="0.17.0"), \
                        mock.patch.object(module, "TAURI", root), \
                        mock.patch.object(module, "stage") as stage:
                    with self.assertRaisesRegex(ValueError, "real public key|overrides cannot publish"):
                        module.main()
                    stage.assert_not_called()

    def test_minimum_os_ignores_sdk_and_linker_versions(self):
        module = build_module()
        self.assertEqual(module.minimum_versions("""binary:
Load command 8
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform MACOS
    minos 11.0
      sdk 15.0
   ntools 1
     tool 4
  version 23.1.3
"""), ["11.0"])
        self.assertEqual(module.minimum_versions("""binary:
Load command 4
      cmd LC_VERSION_MIN_MACOSX
  version 10.13
      sdk 15.0
Load command 5
      cmd LC_SOURCE_VERSION
  version 1.2.3
"""), ["10.13"])
        self.assertEqual(module.minimum_versions("no load commands"), [])

    def test_missing_tools_stops_before_staging(self):
        for rustc in (False, True):
            with self.subTest(rustc=rustc), tempfile.TemporaryDirectory(prefix="agt-build-gate-") as folder:
                root = Path(folder)
                (root / "scripts").mkdir()
                (root / "build/tools").mkdir(parents=True)
                fake = root / "fake-bin"
                fake.mkdir()
                for name in ("build_app.sh", "check_app_tools.sh"):
                    shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)

                def executable(name, text):
                    path = fake / name
                    path.write_text("#!/bin/sh\n" + text)
                    path.chmod(0o700)

                executable("dirname", 'case "$1" in --) shift;; esac\nprintf "%s\\n" "${1%/*}"\n')
                if rustc:
                    executable("rustc", "echo 'rustc 1.95.0 (fake)'\n")
                for name in ("python3", "cargo", "rustup", "curl", "mkdir"):
                    executable(name, 'echo unexpected > "$HOME/UNEXPECTED-STAGING"\nexit 99\n')

                def listing():
                    return [(str(p.relative_to(root)), p.lstat().st_mode, p.lstat().st_mtime_ns,
                             p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))]

                before = listing()
                result = subprocess.run(["/bin/sh", str(root / "scripts/build_app.sh")],
                                        env={"PATH": str(fake), "HOME": str(root)},
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("build/tools/bin/cargo-tauri" if rustc else "rustc is missing", result.stderr)
                self.assertEqual(listing(), before, "tool gate changed the before/after listing")
                self.assertEqual(list((root / "build/tools").iterdir()), [])

    def test_versions_disagree_before_any_staging(self):
        module = build_module()
        with tempfile.TemporaryDirectory(prefix="agt-build-version-") as folder:
            root = Path(folder)
            for name in ("pyproject.toml", ".claude-plugin/plugin.json", "src/agents_router/__init__.py",
                         "macos/src-tauri/Cargo.toml", "macos/src-tauri/tauri.conf.json", "macos/src-tauri/Cargo.lock"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            module.ROOT = root
            module.TAURI = root / "macos/src-tauri"
            version = module.versions()
            path = root / ".claude-plugin/plugin.json"
            data = json.loads(path.read_text())
            data["version"] = version + "-mismatch"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "versions disagree"):
                module.versions()
            self.assertFalse((root / "build").exists())


if __name__ == "__main__":
    unittest.main()
