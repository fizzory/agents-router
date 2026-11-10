"""The release entry point must fail before downloads or runtime staging."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401 (isolates all real homes and provider CLIs)

ROOT = Path(__file__).resolve().parents[1]


def build_module():
    spec = importlib.util.spec_from_file_location("build_app", ROOT / "scripts/build_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildAppTests(unittest.TestCase):
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
