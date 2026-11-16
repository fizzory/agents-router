"""App prerequisite checks never install anything, including on a fake PATH."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401 (isolates the test process)

ROOT = Path(__file__).resolve().parents[1]


class AppToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agt-app-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "build" / "tools").mkdir(parents=True)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        # Scripts resolve the repository relative to themselves, so the test never
        # inspects or writes the real build/tools directory.
        for name in ("check_app_tools.sh", "prepare_app_tools.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        self.executable("dirname", '#!/bin/sh\ncase "$1" in --) shift;; esac\nprintf "%s\\n" "${1%/*}"\n')
        # An unexpected installation is both a failure and a durable tree change.
        for name in ("cargo", "rustup", "curl", "brew"):
            self.executable(name, '#!/bin/sh\nprintf installed > "$HOME/UNEXPECTED-INSTALL"\nexit 99\n')

    def executable(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o700)

    def snapshot(self):
        return [(str(p.relative_to(self.root)), p.lstat().st_mode, p.lstat().st_mtime_ns,
                 p.read_bytes() if p.is_file() else None)
                for p in sorted(self.root.rglob("*"))]

    def run_check(self):
        before = self.snapshot()
        result = subprocess.run(["/bin/sh", str(self.root / "scripts/check_app_tools.sh")],
                                env={"PATH": str(self.bin), "HOME": str(self.root)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(self.snapshot(), before, "read-only check changed the before/after listing")
        self.assertFalse((self.root / "UNEXPECTED-INSTALL").exists())
        return result

    def test_missing_rustc(self):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rustc is missing", result.stderr)

    def test_old_rustc(self):
        self.executable("rustc", "#!/bin/sh\necho 'rustc 1.89.0 (fake)'\n")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs rustc >= 1.90", result.stderr)

    def test_missing_tauri(self):
        self.executable("rustc", "#!/bin/sh\necho 'rustc 1.90.0 (fake)'\n")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build/tools/bin/cargo-tauri", result.stderr)
        self.assertEqual(list((self.root / "build/tools").iterdir()), [])

    def prepare(self, node=True, packages=True):
        self.executable("rustc", "#!/bin/sh\necho 'rustc 1.95.0 (fake)'\n")
        targets = [self.root / "build/tools/bin/cargo-tauri"]
        if node:
            targets.append(self.root / "build/node/bin/node")
        for target in targets:
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            target.chmod(0o700)
        if packages:
            # macos/ui/node_modules is a link into build/ after npm ci.
            modules = self.root / "build/ui-npm/node_modules"
            modules.mkdir(parents=True)
            (modules / ".package-lock.json").write_text("{}", encoding="utf-8")
            (self.root / "macos/ui").mkdir(parents=True)
            (self.root / "macos/ui/node_modules").symlink_to("../../build/ui-npm/node_modules")

    def test_prepared_tools_pass(self):
        self.prepare()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_node_or_ui_packages(self):
        for node, packages in ((False, True), (True, False)):
            with self.subTest(node=node, packages=packages):
                self.setUp()
                self.prepare(node=node, packages=packages)
                result = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("pinned Node in build/node and the UI packages", result.stderr)
                self.assertIn("prepare_app_tools.sh --yes", result.stderr)

    def test_unparseable_version_fails(self):
        self.executable("rustc", "#!/bin/sh\necho 'rustc unknown'\n")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not parse", result.stderr)

    def test_prepare_refuses_without_yes_and_changes_nothing(self):
        before = self.snapshot()
        result = subprocess.run(["/bin/sh", str(self.root / "scripts/prepare_app_tools.sh")],
                                env={"PATH": str(self.bin), "HOME": str(self.root)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit consent", result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
