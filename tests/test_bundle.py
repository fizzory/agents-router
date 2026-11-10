"""App ownership and launch lifetimes, with scratch runtimes and fake host CLIs only."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from unittest.mock import patch

import helpers  # isolate homes, real CLIs, launchd and background runners first
from helpers import TempHome
from test_setup import SetupBase

from agents_router import __version__, bundle, cli, fsutil, hooks, jobs, setup, usage_snapshot
from agents_router.config import load_config
from agents_router.runtimes.base import parent_server
from agents_router.share import triggers


REPO = Path(__file__).resolve().parents[1]


def runtime(root, version=__version__):
    folder = root / "runtimes" / version
    package = folder / "lib/python3.13/site-packages/agents_router/bundle.py"
    package.parent.mkdir(parents=True)
    package.write_text("# staged package\n")
    python = folder / "bin/python3"
    python.parent.mkdir()
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(version)}\n")
    python.chmod(0o755)
    plugin = folder / "plugin"
    for name in (".claude-plugin", "hooks"):
        shutil.copytree(REPO / name, plugin / name)
    for name in ("mcp_config.json", "plugin.json"):
        shutil.copy2(REPO / name, plugin / name)
    data = {"schema_version": 1, "source": "app", "app_version": version, "runtime_version": version,
            "python_version": "3.13.0", "plugin_path": "plugin"}
    (folder / bundle.MANIFEST).write_text(json.dumps(data))
    return folder, python, package, data


class BundleTests(TempHome):
    def setUp(self):
        super().setUp()
        self.support = self.root / "App Support/Agents Router"
        self.env = patch.dict(os.environ, {"AGT_APP_SUPPORT": str(self.support), "AGT_BIN_DIR": str(self.root / "bin")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.folder, self.python, self.package, self.manifest = runtime(self.support)
        self.install = bundle.detect(executable=self.python, package_file=self.package)

    def test_only_manifest_claims_the_running_package_and_python(self):
        self.assertEqual(self.install.runtime, self.folder)
        (self.support / "current").symlink_to(self.folder, target_is_directory=True)
        self.assertEqual(bundle.detect(executable=self.support / "current/bin/python3",
                                       package_file=self.support / "current" / self.package.relative_to(self.folder)), self.install)
        self.assertIsNone(bundle.detect())  # a separate app install cannot claim the source checkout
        (self.folder / bundle.MANIFEST).unlink()
        self.assertIsNone(bundle.detect(executable=self.python, package_file=self.package))
        self.assertIsNone(bundle.detect(executable=sys.executable, package_file=self.package))

    def test_malformed_manifests_and_escaped_plugin_paths_are_not_app_installs(self):
        path = self.folder / bundle.MANIFEST
        for data in ([], {}, {**self.manifest, "source": "uv"}, {**self.manifest, "schema_version": True},
                     {**self.manifest, "runtime_version": "../../outside"},
                     {**self.manifest, "plugin_path": "../outside"}, {**self.manifest, "plugin_path": "/outside"}):
            with self.subTest(data=data):
                path.write_text(json.dumps(data))
                self.assertIsNone(bundle.detect(executable=self.python, package_file=self.package))
        path.write_text("{")
        self.assertIsNone(bundle.detect(executable=self.python, package_file=self.package))

    def test_plugin_copy_pins_hooks_and_mcp_without_mutating_the_payload(self):
        original = (self.install.plugin / "hooks/hooks.json").read_bytes()
        source = bundle.prepare_plugin(self.install)
        self.assertEqual(source, bundle.plugin_source(self.install))
        self.assertEqual((self.install.plugin / "hooks/hooks.json").read_bytes(), original)
        self.assertIn(shlex.quote(str(bundle.stable_launcher())), (source / "hooks/hooks.json").read_text())
        for name in (".claude-plugin/plugin.json", "mcp_config.json"):
            data = json.loads((source / name).read_text())
            self.assertEqual(data["mcpServers"]["agents-router"]["command"], str(bundle.stable_launcher()))

    def test_uv_and_pipx_ownership_stays_external_and_update_commands_are_unchanged(self):
        with patch.object(bundle, "detect", return_value=None):
            for method, source in (("uv", "uv"), ("pipx", "pipx"), ("pipx-global", "pipx"), ("checkout", "source"), ("pip", "source")):
                with patch.object(setup, "_install_info", return_value=(method, None)):
                    self.assertEqual(bundle.installation(), {"source": source, "version": __version__})
            self.assertEqual(setup.detect_install("/tmp/uv/tools/agt", "/x", None, None, "py")[0], "uv")
            self.assertEqual(setup.detect_install("/tmp/pipx/venvs/agt", "/x", None, None, "py")[0], "pipx")

    def test_doctor_reports_versions_and_distinguishes_aliases_from_conflicts(self):
        stable = bundle.stable_launcher()
        stable.parent.mkdir(parents=True)
        stable.write_text("#!/bin/sh\n")
        stable.chmod(0o755)
        public = bundle.bin_dir() / "agt"
        public.parent.mkdir()
        public.symlink_to(stable)
        foreign = self.root / "foreign/agt"
        foreign.parent.mkdir()
        foreign.write_text("#!/bin/sh\n")
        foreign.chmod(0o755)
        with patch.object(bundle, "detect", return_value=self.install), \
                patch.dict(os.environ, {"PATH": os.pathsep.join(map(str, (foreign.parent, public.parent)))}):
            report = bundle.diagnostics()
            self.assertEqual(report["path_entries"], [str(foreign), str(public)])
            self.assertEqual(report["path_conflicts"], [str(foreign)])
            lines = "\n".join(bundle.doctor_lines(report, verbose=True))
            for text in ("install: app", f"app {__version__}", f"agt {__version__}", "python ", "3.13.0", str(foreign), str(public)):
                self.assertIn(text, lines)
            for arguments in (("--json",), ("--verbose",)):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    cli.main(["doctor", "--runtime", "claude", *arguments])
                if "--json" in arguments:
                    self.assertEqual(json.loads(out.getvalue())["install"], report)
                else:
                    self.assertIn(f"warn: another agt on PATH: {foreign}", out.getvalue())

    def test_jobs_and_per_job_helpers_pin_the_physical_runtime(self):
        self.write_config(accounts=("a1",))
        current = self.support / "current"
        current.symlink_to(self.folder, target_is_directory=True)
        with patch.object(bundle, "detect", return_value=self.install), \
                patch.object(sys, "executable", str(current / "bin/python3")), \
                patch.object(jobs, "spawn_detached", return_value=123) as spawn:
            status = jobs.create_job(runtime="claude", prompt="fixture", cwd=str(self.project), account="a1")
            spec = jobs.read_spec(status["job_id"])
            self.assertEqual(status["install_runtime"], str(self.folder))
            self.assertEqual(spec.install_runtime, str(self.folder))
            self.assertEqual(spawn.call_args.args[0][:4], [str(self.python), "-I", "-m", "agents_router"])
            self.assertNotIn("PYTHONPATH", spawn.call_args.kwargs["env"])
            self.assertEqual(fsutil.python_executable(), str(self.python))
            server = parent_server(spec.job_id)
            self.assertEqual(server["command"], str(self.python))
            self.assertEqual(server["args"][:3], ["-I", "-m", "agents_router"])
            self.assertNotIn("PYTHONPATH", server["env"])
            self.assertIn(shlex.quote(str(self.python)), hooks.worker_inbox_hook(spec.job_id, self.home)["hooks"][0]["command"])
            self.assertIn(shlex.quote(str(self.python)), hooks.worker_read_hook()["hooks"][0]["command"])

    def test_reusable_launcher_follows_current_after_a_switch(self):
        self.support.joinpath("current").symlink_to(self.folder, target_is_directory=True)
        stable = bundle.stable_launcher()
        stable.parent.mkdir()
        stable.write_text('#!/bin/sh\nexec "$(dirname "$0")/../current/bin/python3" "$@"\n')
        stable.chmod(0o755)
        with patch.object(bundle, "detect", return_value=self.install):
            launcher, _ = triggers.write_launcher()
        text = launcher.read_text()
        self.assertIn(shlex.quote(str(stable)), text)
        self.assertNotIn(str(self.folder), text)
        self.assertIsNone(triggers.launcher_problem())
        self.assertEqual(subprocess.check_output([str(launcher), "--version"], text=True).strip(), __version__)
        other, _, _, _ = runtime(self.support, "0.99.0")
        (self.support / "current").unlink()
        (self.support / "current").symlink_to(other, target_is_directory=True)
        self.assertEqual(subprocess.check_output([str(launcher), "--version"], text=True).strip(), "0.99.0")
        stable.unlink()
        self.assertIn("is gone", triggers.launcher_problem())
        response = subprocess.check_output([str(launcher), "share", "session"], text=True)
        self.assertIn("Repair", json.loads(response)["systemMessage"])

    def test_usage_reports_app_ownership_without_running_commands(self):
        with patch.object(bundle, "detect", return_value=self.install), \
                patch("subprocess.run", side_effect=AssertionError("usage ran a process")):
            self.assertEqual(usage_snapshot.snapshot(load_config())["install"], {"source": "app", "version": __version__})


class BundledSetupTests(SetupBase):
    def setUp(self):
        super().setUp()
        self.support = self.root / "App Support"
        self.folder, python, package, _ = runtime(self.support)
        self.install = bundle.detect(executable=python, package_file=package)
        self.app_env = patch.dict(os.environ, {"AGT_APP_SUPPORT": str(self.support), "AGT_BIN_DIR": str(self.root / "bin"),
                                              "AGT_PLUGIN_SOURCE": "should/never-fetch"})
        self.app_env.start()
        self.addCleanup(self.app_env.stop)
        self.detector = patch.object(bundle, "detect", return_value=self.install)
        self.detector.start()
        self.addCleanup(self.detector.stop)

    def tearDown(self):
        # SetupBase restores its environment in tearDown, before unittest cleanups.
        # Unwind our nested patches first so no deleted scratch home leaks to later modules.
        self.detector.stop()
        self.app_env.stop()
        super().tearDown()

    def seed(self):
        for kind, folder in (("claude", self.claude_dir), ("codex", self.codex_dir), ("agy", self.agy_dir)):
            key = "agents-router" if kind == "agy" else setup.PLUGIN_ID
            (folder / "fake_plugins.json").write_text(json.dumps({"marketplaces": ["agents-router"], "plugins": {key: True}}))

    def test_app_update_skips_package_and_refreshes_hosts_and_launcher_offline(self):
        self.seed()
        # Start with the interpreter-pinned launcher left by a non-app install.
        with patch.object(bundle, "detect", return_value=None):
            launcher, _ = triggers.write_launcher()
            self.assertIn(f"PY={shlex.quote(fsutil.python_executable())}\n", launcher.read_text())
        self.assertNotIn(str(bundle.stable_launcher()), launcher.read_text())
        # An existing, unpinned plugin tree must be refreshed too, not just reused.
        source = bundle.plugin_source(self.install)
        shutil.copytree(self.install.plugin, source)
        metadata = [source / name for name in (".claude-plugin/plugin.json", "mcp_config.json")]
        for path in metadata:
            self.assertEqual(json.loads(path.read_text())["mcpServers"]["agents-router"]["command"], "agt")
        hook_file = source / "hooks/hooks.json"
        self.assertNotIn(str(bundle.stable_launcher()), hook_file.read_text())
        with patch.object(setup, "_run_shown", wraps=setup._run_shown) as shown, \
                patch.object(setup, "finish_in_new_agt", side_effect=AssertionError("app reinstalled a package")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["update"]), 0, out.getvalue())
        self.assertIn("Check for Updates", out.getvalue())
        source = str(source)
        calls = self.calls()
        for kind in ("claude", "codex"):
            commands = [c["argv"] for c in calls if c["kind"] == kind]
            self.assertIn(["plugin", "marketplace", "remove", "agents-router"], commands)
            self.assertIn(["plugin", "marketplace", "add", source], commands)
        self.assertIn(["plugin", "install", source], [c["argv"] for c in calls if c["kind"] == "agy"])
        for call in shown.call_args_list:
            argv = call.args[1]
            self.assertNotIn("https://", " ".join(argv))
            self.assertNotIn("should/never-fetch", argv)
            self.assertNotIn("upgrade", argv)
            self.assertNotIn("update", argv)
        self.assertIn(f"AGT={shlex.quote(str(bundle.stable_launcher()))}\n", launcher.read_text())
        for path in metadata:
            self.assertEqual(json.loads(path.read_text())["mcpServers"]["agents-router"]["command"],
                             str(bundle.stable_launcher()))
        self.assertIn(shlex.quote(str(bundle.stable_launcher())), hook_file.read_text())

    def test_setup_and_settings_declaration_take_the_bundled_tree(self):
        code, out = self.run_cli("--host", "claude", "--host", "codex", "--host", "antigravity", "--yes", "--roles", "off")
        self.assertEqual(code, 0, out)
        self.assertTrue(bundle.plugin_source(self.install).is_dir())
        report = setup.Report()
        self.claude_dir.joinpath("settings.json").write_text(json.dumps({"enabledPlugins": {setup.PLUGIN_ID: True},
                    "extraKnownMarketplaces": {"agents-router": {"autoUpdate": False,
                        "source": {"source": "github", "repo": setup.REPO}}}}))
        setup._declare_in_settings(self.claude_dir, report, why=None)
        source = json.loads(self.claude_dir.joinpath("settings.json").read_text())["extraKnownMarketplaces"]["agents-router"]["source"]
        self.assertEqual(source, {"source": "directory", "path": str(bundle.plugin_source(self.install))})
        self.assertIs(json.loads(self.claude_dir.joinpath("settings.json").read_text())
                      ["extraKnownMarketplaces"]["agents-router"]["autoUpdate"], False)

    def test_update_refreshes_a_settings_only_profile_without_a_cli(self):
        path = self.claude_dir / "settings.json"
        path.write_text(json.dumps({"enabledPlugins": {setup.PLUGIN_ID: True},
            "extraKnownMarketplaces": {"agents-router": {"source": {"source": "github", "repo": setup.REPO}}}}))
        with patch.object(setup.get_runtime("claude"), "find_binary", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            code, report = setup.run_update(load_config(), ["claude"])
        self.assertEqual(code, 0, report.failed)
        self.assertEqual(json.loads(path.read_text())["extraKnownMarketplaces"]["agents-router"]["source"],
                         {"source": "directory", "path": str(bundle.plugin_source(self.install))})

    def test_dry_run_is_read_only_and_missing_payload_cannot_fall_back_to_network(self):
        self.seed()
        with contextlib.redirect_stdout(io.StringIO()):
            code, report = setup.run_update(load_config(), ["auto"], dry_run=True)
        self.assertEqual(code, 0, report.failed)
        self.assertFalse(bundle.plugin_source(self.install).exists())
        (self.install.plugin / "hooks/hooks.json").unlink()
        with patch.object(setup, "run_command", side_effect=AssertionError("damaged payload ran a host command")):
            code, report = setup.run_update(load_config(), ["auto"])
        self.assertEqual(code, 1)
        self.assertIn("bundled plugin is missing", report.failed[0])
