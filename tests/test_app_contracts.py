"""Settings/account contracts for the macOS client, using only fake CLIs and temporary homes."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import FAKE_HOST
from generate_settings_fixtures import FOLDER, fixture_texts, share_inputs
from test_config_cli import ConfigBase
from test_usage_cli import listing
from test_roles_cli import RolesBase
from test_share_plan import MacCopy
from test_share_sync import YES

from agents_router import account_check, cli, config, jobs, levels, settings_write, synclog, usage_snapshot
from agents_router.share import apply, settings as share_settings


class SettingsContractTests(ConfigBase):
    def test_show_schema_and_each_level_from_the_same_bytes(self):
        raw = b"max_jobs = 3\r\n[codex]\r\nmax_jobs_per_account = 5\r\n"
        self.user_file.write_bytes(raw)
        self.local_file.write_text("max_jobs = 1\n", encoding="utf-8")
        code, shown = self.json("config", "show")
        self.assertEqual((code, shown["schema_version"]), (0, 1))
        rows = {row["key"]: row for row in shown["settings"]}
        row = rows["codex.max_jobs_per_account"]
        self.assertEqual((row["kind"], row["minimum"], row["cap"], row["choices"], row["editable"]),
                         ("int", 1, True, [], True))
        self.assertEqual(row["levels"]["user"], {"present": True, "value": 5, "file": str(self.user_file),
                                               "line": 3, "revision": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(row["levels"]["workspace"], {"present": False, "value": None, "file": str(self.team_file),
                                                    "line": None, "revision": "missing"})
        self.assertEqual(row["still_limited_by"], [f"max_jobs = 1 (local, {self.local_file})"])
        self.assertEqual(rows["codex.failover"]["choices"], ["off", "on-limit"])
        self.assertFalse(rows["share.enabled"]["editable"])
        self.assertEqual(rows["codex.primary"]["accepted_levels"], ["user"])
        self.assertEqual(rows["codex.accounts"]["accepted_levels"], ["workspace", "local"])
        self.assertEqual(rows["max_depth"]["accepted_levels"], ["user", "workspace", "local"])

    def test_previous_is_selected_level_value_even_when_masked(self):
        self.user_file.write_text("max_depth = 2\n", encoding="utf-8")
        self.local_file.write_text("max_depth = 0\n", encoding="utf-8")
        code, report = self.json("config", "set", "max_depth", "3", "--user")
        self.assertEqual((code, report["old"], report["new"]), (0, 0, 0))
        self.assertEqual(report["previous"], {"level": "user", "present": True, "value": 2})
        self.assertEqual(report["revision"], hashlib.sha256(self.user_file.read_bytes()).hexdigest())
        code, report = self.json("config", "set", "job_retention_days", "5", "--user")
        self.assertEqual(report["previous"], {"level": "user", "present": False, "value": None})

    def test_json_errors_preserve_exit_two_and_settings(self):
        self.user_file.write_text("max_depth = 2\n", encoding="utf-8")
        before = self.user_file.read_bytes()
        for key, value, flag in (("max_depth", "no", "--user"), ("codex.primary", "x", "--workspace"),
                                 ("share.enabled", "true", "--user"), ("max_job", "2", "--user"),
                                 ("codex.usage_ceiling", "nan", "--user")):
            with self.subTest(key=key):
                code, report = self.json("config", "set", key, value, flag)
                self.assertEqual(code, 2)
                self.assertFalse(report["ok"])
                self.assertEqual(report["error"]["key"], key)
                self.assertTrue(report["error"]["message"])
                self.assertEqual(self.user_file.read_bytes(), before)

    def test_unset_absent_and_revision_guard_including_absent_file(self):
        code, report = self.json("config", "unset", "max_depth", "--user", "--if-unchanged", "missing")
        self.assertEqual((code, report["ok"], report["changed"], report["revision"]), (0, True, False, "missing"))
        self.assertEqual(report["previous"], {"level": "user", "present": False, "value": None})
        self.assertFalse(self.user_file.exists())
        code, report = self.json("config", "set", "max_depth", "0", "--user", "--if-unchanged", "missing")
        self.assertEqual(code, 0)
        old_revision = report["revision"]
        self.user_file.write_text("max_depth = 1\n", encoding="utf-8")
        for command in (("set", "max_depth", "2"), ("unset", "job_retention_days")):
            code, refused = self.json("config", *command, "--user", "--if-unchanged", old_revision)
            self.assertEqual(code, 2)
            self.assertIn("changed since", refused["error"]["message"])
            self.assertEqual(self.user_file.read_text(), "max_depth = 1\n")

    def test_revert_checks_level_value_and_preserves_masking(self):
        self.user_file.write_text("max_depth = 2\n", encoding="utf-8")
        self.local_file.write_text("max_depth = 0\n", encoding="utf-8")
        self.json("config", "set", "max_depth", "3", "--user")
        entry = next(synclog.entries())
        code, report = self.json("config", "revert", entry["id"])
        self.assertEqual((code, report["new"]), (0, 0))
        self.assertEqual(levels.read_value(self.user_file.read_text(), None, "max_depth"), 2)
        self.json("config", "set", "max_depth", "3", "--user")
        entry = next(synclog.entries())
        self.user_file.write_text("max_depth = 4\n", encoding="utf-8")
        code, report = self.json("config", "revert", entry["id"])
        self.assertEqual(code, 2)
        self.assertIn("changed since", report["error"]["message"])
        self.assertEqual(self.user_file.read_text(), "max_depth = 4\n")

    def test_revert_unsets_an_originally_absent_key(self):
        self.json("config", "set", "max_depth", "0", "--user")
        code, report = self.json("config", "revert", next(synclog.entries())["id"])
        self.assertEqual((code, report["previous"]["present"]), (0, True))
        self.assertIs(levels.read_value(self.user_file.read_text(), None, "max_depth"), levels.MISSING)

    def test_revert_rechecks_revision_if_a_hand_edit_follows_comparison(self):
        self.json("config", "set", "max_depth", "0", "--user")
        entry = next(synclog.entries())
        original = cli.config_cli._change

        def intervening(args, unset):
            self.user_file.write_text("max_depth = 7\n", encoding="utf-8")
            return original(args, unset)

        with patch("agents_router.config_cli._change", side_effect=intervening):
            code, report = self.json("config", "revert", entry["id"])
        self.assertEqual(code, 2)
        self.assertIn("changed since", report["error"]["message"])

    def test_settings_error_locations_reach_refused_entries(self):
        self.user_file.write_text("[codex]\nmax_jobs_per_account = 0\n", encoding="utf-8")
        with self.assertRaises(levels.SettingsError) as caught:
            config.load_config()
        exc = caught.exception
        self.assertEqual((exc.file, exc.line), (str(self.user_file), 2))
        synclog.refused(exc, None)
        self.assertEqual((next(synclog.entries())["file"], next(synclog.entries())["line"]), (str(self.user_file), 2))
        synclog.refused(levels.SettingsError("can't read", file=self.team_file), None)
        self.assertIsNone(next(synclog.entries())["line"])

    def test_codex_start_settings_error_keeps_refusal_location(self):
        self.user_file.write_text("[codex]\nmax_jobs_per_account = 0\n", encoding="utf-8")
        with self.assertRaises(levels.SettingsError):
            jobs.create_job(runtime="codex", prompt="test", cwd=str(self.project))
        entry = next(synclog.entries())
        self.assertEqual((entry["type"], entry["file"], entry["line"]),
                         ("refused", str(self.user_file), 2))


class AccountContractTests(ConfigBase):
    def setUp(self):
        super().setUp()
        self.write_config()
        fake = lambda kind: json.dumps([sys.executable, str(FAKE_HOST), kind])
        self.fake_env = patch.dict(os.environ, {"AGT_CLAUDE_COMMAND": fake("claude"), "AGT_CODEX_COMMAND": fake("codex"),
                                                "AGT_AGY_COMMAND": fake("agy"), "AGT_KIRO_COMMAND": fake("kiro"),
                                                "FAKE_HOST_LOG": str(self.root / "host.jsonl")})
        self.fake_env.start()
        self.addCleanup(self.fake_env.stop)

    def test_enable_disable_and_refusals_write_nothing(self):
        code, report = self.json("accounts", "disable", "a1")
        self.assertEqual((code, report), (0, {"ok": True, "account": "a1", "enabled": False}))
        self.assertFalse(config.load_config().runtime("claude").find("a1").enabled)
        self.assertEqual(len(list((self.home / "state" / "backups" / "settings").iterdir())), 1)
        self.assertEqual(next(synclog.entries())["key"], "claude.accounts.a1.enabled")
        self.assertEqual(self.json("accounts", "enable", "a1")[1]["enabled"], True)
        for action in ("enable", "disable"):
            for name, expected in (("primary", 2), ("missing", 1), ("bad/name", 2)):
                before = listing(self.home)
                code, report = self.json("accounts", action, name)
                self.assertEqual((code, report["ok"], report["error"]["exit_code"]), (expected, False, expected))
                self.assertEqual((report["error"]["account"], report["error"]["runtime"]), (name, "claude"))
                message = report["error"]["message"]
                self.assertTrue(message)
                if name == "primary":
                    self.assertIn("own login", message)
                    self.assertIn("extra account", message)
                    self.assertIn("agt accounts list", message)
                self.assertEqual(listing(self.home), before)

    def test_discovered_and_canonical_primary_refusals(self):
        for name in (".claude-other", ".claude-primary"):
            folder = self.user_home / name
            folder.mkdir()
            (folder / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"fake"}}', encoding="utf-8")
        self.user_file.write_text('[claude]\nprimary = "~/.claude-primary"\n', encoding="utf-8")
        before = listing(self.home)
        for action in ("enable", "disable"):
            code, report = self.json("accounts", action, "claude-other")
            self.assertEqual((code, report["error"]["exit_code"]), (1, 1))
            self.assertIn("own Claude profiles", report["error"]["message"])
            self.assertEqual(listing(self.home), before)
            code, report = self.json("accounts", action, "claude-primary")
            self.assertEqual(code, 1)
            self.assertIn("primary claude account", report["error"]["message"])
            self.assertEqual(listing(self.home), before)

    def test_add_check_is_side_effect_free_and_real_add_validates_again(self):
        before = listing(self.home)
        for name, runtime, expected in (("spare", "claude", 0), ("a1", "claude", 1),
                                        ("primary", "claude", 2), ("extra", "kiro", 1)):
            code, report = self.json("accounts", "add", name, "--runtime", runtime, "--check")
            self.assertEqual((code, report["ok"]), (expected, expected == 0))
            self.assertEqual(listing(self.home), before)
            self.assertFalse((self.root / "host.jsonl").exists())
        self.assertFalse((self.home / "homes").exists())
        self.assertEqual(self.agt("accounts", "add", "spare", "--no-login")[0], 0)
        saved = self.user_file.read_bytes()
        code, report = self.json("accounts", "add", "spare", "--no-login")
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertEqual(self.user_file.read_bytes(), saved)

    def test_explicit_checks_cache_measured_auth_and_usage_only_reads_it(self):
        (self.primary / "logged_in").touch()
        code, report = self.json("accounts", "check", "primary")
        self.assertEqual((code, report["schema_version"], report["accounts"][0]["logged_in"]), (0, 1, True))
        loaded = config.load_config(cached_only=True)
        self.assertTrue(account_check.cached(loaded.runtime("claude"), loaded.runtime("claude").primary_account()))
        (self.primary / "logged_in").unlink()
        self.assertFalse(self.json("accounts", "check", "primary")[1]["accounts"][0]["logged_in"])
        before = listing(self.home)
        with patch("agents_router.account_check.check", side_effect=AssertionError("usage probed auth")), \
                patch("subprocess.run", side_effect=AssertionError("usage ran a CLI")):
            usage_snapshot.snapshot(loaded)
        self.assertEqual(listing(self.home), before)

    def test_each_runtime_uses_its_quota_free_probe(self):
        for runtime in ("codex", "kiro", "antigravity"):
            code, report = self.json("accounts", "check", "primary", "--runtime", runtime)
            self.assertEqual((code, report["runtime"]), (0, runtime))
            loaded = config.load_config(cached_only=True).runtime(runtime)
            self.assertEqual(account_check.cached(loaded, loaded.primary_account()), report["accounts"][0]["logged_in"])
        records = [json.loads(line) for line in (self.root / "host.jsonl").read_text().splitlines()]
        self.assertEqual([row["argv"][:2] for row in records], [["login", "status"], ["whoami", "--format"], ["models"]])

    def test_login_primary_refreshes_cache_including_exit_zero_without_auth(self):
        self.assertEqual(self.agt("accounts", "login", "primary")[0], 0)
        runtime = config.load_config(cached_only=True).runtime("claude")
        self.assertTrue(account_check.cached(runtime, runtime.primary_account()))
        (self.primary / "logged_in").unlink()
        code, out, err = self.agt("accounts", "login", "primary", env={"FAKE_HOST_LOGIN_LEAVES_LOGGED_OUT": "1"})
        self.assertEqual(code, 0)
        self.assertIn("Login check: logged out", out)
        self.assertFalse(account_check.cached(runtime, runtime.primary_account()))

    def test_add_checks_auth_after_login_even_when_login_leaves_it_logged_out(self):
        code, out, err = self.agt("accounts", "add", "spare", env={"FAKE_HOST_LOGIN_LEAVES_LOGGED_OUT": "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("Login check: logged out", out)
        runtime = config.load_config(cached_only=True).runtime("claude")
        self.assertFalse(account_check.cached(runtime, runtime.find("spare")))

    def test_probe_timeout_is_bounded_and_cached_as_unknown(self):
        runtime = config.load_config(cached_only=True).runtime("claude")
        with patch.dict(os.environ, {"FAKE_HOST_AUTH_DELAY": "1"}):
            report = account_check.check(runtime, runtime.primary_account(), timeout=.05)
        self.assertIsNone(report["logged_in"])
        self.assertEqual(report["error"], "login check failed")


class RolesContractTests(RolesBase):
    def test_choices_and_key_sources_do_not_discover_by_default(self):
        from agents_router.model_catalog import ModelCatalog
        self.local_file.write_text('[roles.reviewer]\nenabled = false\n', encoding="utf-8")
        with patch.object(ModelCatalog, "request", side_effect=AssertionError("implicit discovery")), \
                patch("agents_router.role_run.precheck", side_effect=AssertionError("precheck")):
            code, report = self.json("roles", "show")
        self.assertEqual((code, report["schema_version"]), (0, 1))
        reviewer = next(role for role in report["roles"] if role["name"] == "reviewer")
        self.assertEqual(reviewer["sources"]["enabled"], {"level": "local", "file": str(self.local_file), "line": 2})
        self.assertEqual(reviewer["sources"]["providers"]["level"], "user")
        choices = reviewer["providers"][0]["choices"]
        self.assertEqual(choices["catalog"]["status"], "unavailable")
        self.assertFalse(any(model["offered"] for model in choices["models"]))

    def test_discovered_models_retain_off_plan_choices_and_efforts_are_capped(self):
        from agents_router.runtimes.codex import CodexRuntime
        # A model offered by the CLI can still be excluded by the account plan.
        self.user_file.write_text(self.user_file.read_text().replace('name = "work"',
            'name = "work"\nmodels = ["gpt-6.1-sol"]\nmax_effort = "medium"'))
        with patch.object(CodexRuntime, "discover_models", return_value=("gpt-6.1-sol", "off-plan")) as discover:
            code, shown = self.json("roles", "show", "--discover-models")
        self.assertEqual(code, 0)
        self.assertEqual(discover.call_count, 1)
        choices = shown["roles"][0]["providers"][0]["choices"]
        models = {row["model"]: row for row in choices["models"]}
        self.assertEqual(choices["catalog"]["status"], "ready")
        self.assertTrue(models["gpt-6.1-sol"]["offered"])
        self.assertEqual((models["off-plan"]["offered"], models["off-plan"]["allowed"]), (True, False))
        self.assertTrue(models["off-plan"]["reason"])
        self.assertEqual(choices["efforts"], [None, "minimal", "low", "medium"])

    def test_discovery_has_a_total_deadline(self):
        from agents_router.model_catalog import ModelCatalog, Snapshot
        from agents_router.roles_cli import _json_catalog
        loaded = config.load_config()
        with patch.object(ModelCatalog, "request", return_value=Snapshot("loading")), \
                patch.object(ModelCatalog, "snapshot", return_value=Snapshot("loading")), \
                patch("agents_router.roles_cli.time.monotonic", side_effect=[0, 6]):
            self.assertIsInstance(_json_catalog(loaded, True), ModelCatalog)

    def test_structured_undo_restores_the_selected_level_and_refuses_intervening_change(self):
        code, report = self.json("roles", "config", "reviewer", "--remove", "antigravity:primary", "--user")
        self.assertEqual(code, 0)
        reverse = ["roles", "config", "--from", report["undo_from"], "--" + report["level"],
                   "--if-unchanged", report["if_unchanged"]]
        self.assertEqual(self.json(*reverse)[0], 0)
        self.assertEqual(config.load_config().roles["reviewer"].as_table()["providers"],
                         ["codex:work/gpt-6.1-sol:high", "antigravity:primary"])
        code, report = self.json("roles", "config", "reviewer", "--remove", "antigravity:primary", "--user")
        reverse[3], reverse[-1] = report["undo_from"], report["if_unchanged"]
        self.user_file.write_text(self.user_file.read_text() + "\n# intervening edit\n")
        before = self.user_file.read_bytes()
        code, report = self.json(*reverse)
        self.assertEqual((code, report["ok"], report["error"]["needs_user_words"]), (2, False, False))
        self.assertIn("changed since", report["error"]["message"])
        self.assertEqual(self.user_file.read_bytes(), before)

    def test_errors_are_json_and_user_words_have_a_distinct_code(self):
        for argv, expected, needs in ((["--set-effort", "codex:work=wrong"], 2, False),
                                      (["--disable", "reviewer"], 3, True),
                                      (["reviewer", "--only", "unknown"], 2, False)):
            before = self.user_file.read_bytes()
            code, report = self.json("roles", "config", *argv, env={"CLAUDECODE": "1"})
            self.assertEqual((code, report["ok"], report["error"]["exit_code"], report["error"]["needs_user_words"]),
                             (expected, False, expected, needs))
            self.assertTrue(report["error"]["message"])
            self.assertEqual(self.user_file.read_bytes(), before)


class ShareContractTests(MacCopy):
    def test_bulk_enable_answers_are_remembered_without_setting_entries_and_picks_are_logged(self):
        code, out = self.run_share(*YES)
        self.assertEqual(code, 0, out)
        self.assertEqual([entry for entry in synclog.entries() if entry["type"] == "setting"], [])
        code, out = self.run_share("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual([entry for entry in synclog.entries() if entry["type"] == "setting"], [])
        code, out = self.run_share("pick", "dana", "--not-in", "kiro")
        self.assertEqual(code, 0, out)
        entries = [entry for entry in synclog.entries() if entry["type"] == "setting"]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["key"], entries[0]["new"], entries[0]["by"]),
                         ("share.targets.agents/dana.not_in", ["kiro"], "command"))
        self.assertTrue(Path(entries[0]["backup"]).is_file())

    def test_listing_uses_all_kind_planners_and_exposes_no_secrets_or_commands(self):
        share_inputs(self.user)
        share_settings.save(share_settings.Settings(enabled=True, mcp=True, hooks=True, plugins=True,
            instructions="block", folders=[share_settings.Folder("claude", self.claude, "claude"),
                                          share_settings.Folder("codex", self.user / ".codex", "codex"),
                                          share_settings.Folder("kiro", self.user / ".kiro", "kiro")]))
        before = listing(self.root)
        code, out = self.run_share("list", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(listing(self.root), before)
        self.assertNotIn("synthetic-fixture-secret", out)
        self.assertNotIn("echo example", out)
        items = {item["id"]: item for item in json.loads(out)["items"]}
        self.assertTrue({"mcp/notes", "mcp/private", "plugins/example@tools", "instructions/instructions"} <= items.keys())
        self.assertTrue(any(item["kind"] == "hooks" for item in items.values()))
        self.assertTrue(items["mcp/notes"]["cells"]["codex"]["editable"])
        secret = items["mcp/private"]["cells"]["codex"]
        self.assertFalse(secret["editable"])
        self.assertIn("literal secret", secret["reason"])
        self.assertFalse(items["plugins/example@tools"]["cells"]["kiro"]["editable"])

    def test_listing_exits_zero_and_never_writes_even_with_conflicts_or_sharing_off(self):
        for enabled in (False, True):
            saved = share_settings.Settings(enabled=enabled, folders=[
                share_settings.Folder("claude", self.claude, "claude"),
                share_settings.Folder("claude", self.lab, "lab"),
                share_settings.Folder("codex", self.user / ".codex", "codex")])
            share_settings.save(saved)
            before = listing(self.root)
            code, out = self.run_share("list", "--json")
            self.assertEqual((code, json.loads(out)["schema_version"]), (0, 1))
            self.assertEqual(listing(self.root), before)
            report = json.loads(out)
            items = {item["id"]: item for item in report["items"]}
            self.assertIn("agents/dana", items)
            dana = items["agents/dana"]["cells"]
            self.assertFalse(dana["claude"]["editable"])
            self.assertTrue(dana["claude"]["reason"])
            self.assertEqual(dana["lab"]["editable"], enabled)
            blocked = items["agents/explorer"]["cells"]["codex"]
            self.assertFalse(blocked["editable"])
            self.assertTrue(blocked["reason"])
            if enabled:
                code, out = self.run_share("pick", "--json")
                self.assertEqual((code, json.loads(out)["error"]), (3, "needs_choice"))


class ConcurrentWriterTests(ConfigBase):
    def test_background_sync_does_not_wait_for_the_settings_writer(self):
        self.prepare_sharing()
        with settings_write.locked():
            before = self.user_file.read_bytes()
            result = subprocess.run([sys.executable, "-m", "agents_router", "share", "sync", "--quiet"],
                                    cwd=self.project, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.user_file.read_bytes(), before)

    def prepare_sharing(self):
        self.write_config()
        folder = self.user_home / ".claude"
        source = folder / "skills" / "example" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nname: example\ndescription: Example skill\n---\nExample.\n")
        share_settings.save(share_settings.Settings(enabled=True, watcher=False, push_on_save=False,
            folders=[share_settings.Folder("claude", folder, "claude")]))
        # Baseline includes this initial save's audit entries; assert only later writes below.
        return folder

    def test_config_accounts_and_sharing_wait_for_one_lock_and_preserve_all_changes(self):
        self.prepare_sharing()
        baseline = len(list(synclog.entries()))
        backups_before = len(list((self.home / "state" / "backups" / "settings").iterdir()))
        commands = [["config", "set", "job_retention_days", "9", "--user", "--json"],
                    ["accounts", "disable", "a2", "--json"],
                    ["share", "pick", "skills/example", "--only", "claude", "--json"],
                    ["share", "kinds", "add", "mcp", "--json"],
                    ["config", "set", "share.notify", "false", "--user", "--json"]]
        processes = []
        with settings_write.locked():
            before = self.user_file.read_bytes()
            for argv in commands:
                process = subprocess.Popen([sys.executable, "-m", "agents_router", *argv], cwd=self.project,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=.3)
            self.assertEqual(self.user_file.read_bytes(), before)
            self.assertEqual(len(list(synclog.entries())), baseline)
        for process in processes:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err)
            self.assertIsInstance(json.loads(out), dict)
        loaded = config.load_config(cached_only=True)
        self.assertEqual(loaded.job_retention_days, 9)
        self.assertFalse(loaded.runtime("claude").find("a2").enabled)
        self.assertEqual(share_settings.load().only["skills/example"], ["claude"])
        self.assertTrue(share_settings.load().mcp)
        self.assertFalse(share_settings.load().notify)
        new_entries = list(synclog.entries())[:len(list(synclog.entries())) - baseline]
        self.assertTrue({"job_retention_days", "claude.accounts.a2.enabled", "share.targets.skills/example.only", "share.mcp", "share.notify"}
                        <= {entry.get("key") for entry in new_entries})
        backups = list((self.home / "state" / "backups" / "settings").iterdir())
        self.assertEqual(len(backups) - backups_before, 5)
        self.assertIn(before, [path.read_bytes() for path in backups])

    def test_apply_lock_wait_does_not_invert_the_settings_lock(self):
        folder = self.prepare_sharing()
        processes = []
        try:
            with apply.lock([folder]):
                for argv in (["share", "pick", "skills/example", "--only", "claude", "--json"],
                             ["config", "set", "max_depth", "0", "--user", "--json"]):
                    process = subprocess.Popen([sys.executable, "-m", "agents_router", *argv], cwd=self.project,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append(process)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=.3)
            for process in processes:
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, err)
            self.assertEqual(share_settings.load().only["skills/example"], ["claude"])
            self.assertEqual(config.load_config().max_depth, 0)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


class GoldenFixtureTests(unittest.TestCase):
    def test_settings_and_accounts_golden_contracts_match_the_producers(self):
        for name, text in fixture_texts().items():
            with self.subTest(fixture=name):
                self.assertEqual((FOLDER / name).read_text(encoding="utf-8"), text,
                                 "Regenerate with PYTHONPATH=src:tests python3 tests/generate_settings_fixtures.py")

    def test_pane_states_come_from_producers_and_keep_structured_fields(self):
        fixtures = {name: json.loads(text) for name, text in fixture_texts().items()}
        accounts = fixtures["accounts-list-locked.json"]["runtimes"][0]["accounts"]
        by_name = {row["name"]: row for row in accounts}
        self.assertTrue(by_name["work"]["can_toggle"])
        for name in ("primary", "claude-lab"):
            self.assertFalse(by_name[name]["can_toggle"])
            self.assertTrue(by_name[name]["toggle_reason"])
        self.assertTrue(by_name["claude-lab"]["found"])
        roles = {row["name"]: row for row in fixtures["roles-show-panes.json"]["roles"]}
        self.assertEqual(roles["builder"]["access"], "edit")
        self.assertEqual(roles["reviewer"]["sources"]["enabled"]["level"], "local")
        self.assertEqual(fixtures["share-kinds-unchanged.json"], {"kind": "mcp", "enabled": True, "unchanged": True})
        self.assertIn("written", fixtures["share-kinds-add.json"])
        self.assertFalse(fixtures["share-status-off.json"]["enabled"])
        self.assertTrue(fixtures["share-status-waiting.json"]["attention"])
        entries = fixtures["activity-log.json"]["entries"]
        self.assertTrue(any(row["type"] == "setting" and "old" in row and "new" in row for row in entries))
        self.assertTrue(any(row["type"] == "refused" and row["line"] == 2 for row in entries))
        self.assertTrue(any(row["type"] == "sync" and any(not change["ok"] for change in row["changes"]) for row in entries))


if __name__ == "__main__":
    unittest.main()
