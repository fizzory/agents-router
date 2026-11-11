"""Usage contract checks run only against throwaway homes and fake processes."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome
from generate_usage_fixture import FIXTURE, NOW, POPOVER_FOLDER, fixture_text, fixture_texts, popover_states_text

from agents_router import accounts, cli, health, profiles, usage_snapshot
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig, load_config
from agents_router.fsutil import lock_busy, read_json, try_lock, unlock, write_json


def listing(root):
    """Include directories, links, mtimes and full bytes, including the root."""
    if not root.exists():
        return None
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return result


class PopoverFixtureTests(unittest.TestCase):
    def test_golden_popover_inputs_match_the_producer(self):
        for name, text in fixture_texts().items():
            with self.subTest(fixture=name):
                self.assertEqual((FIXTURE.parent / name).read_text(), text)
        self.assertEqual((POPOVER_FOLDER / "states.json").read_text(), popover_states_text())


class UsageTests(TempHome):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.runtimes = {name: RuntimeConfig(name, self.root / name, _found=[]) for name in RUNTIMES}
        self.runtime = self.runtimes["claude"]
        self.a = Account("claude-work", self.root / "claude-work", max_jobs=2)
        self.b = Account("claude-other", self.root / "claude-other", max_jobs=2)
        self.runtime.accounts = [self.a, self.b]
        self.runtime.primary = self.a.home
        self.config = Config(self.runtimes)
        self.auth(self.a, True)
        self.auth(self.b, True)
        self._time = patch("time.time", return_value=self.now)
        self._time.start()
        self.addCleanup(self._time.stop)

    def auth(self, account, value, checked=None):
        path = self.home / "state" / "claude-profiles.json"
        data = read_json(path, {})
        data[str(account.home.resolve())] = {"logged_in": value, "stamp": None,
                                            "checked": self.now if checked is None else checked}
        write_json(path, data)

    def usage(self, data, runtime="claude"):
        write_json(self.home / "state" / f"{runtime}-usage.json", data)

    def circuit(self, account=None, scope="account", until=None, **extra):
        account = account or self.a
        path = self.home / "state" / "claude-health.json"
        data = read_json(path, {})
        record = data.setdefault(accounts._home_key(account.home), {"generation": 0, "circuits": {}})
        record["circuits"][scope] = {"opened_at": self.now - 60,
                                     "open_until": self.now + 120 if until is None else until,
                                     "reason": "model at capacity", **extra}
        write_json(path, data)

    def report(self):
        with patch("subprocess.run", side_effect=AssertionError("snapshot started a subprocess")), \
                patch("subprocess.Popen", side_effect=AssertionError("snapshot started a worker")):
            return usage_snapshot.snapshot(self.config, now=self.now)

    def entries(self):
        return {a["name"]: a for p in self.report()["providers"] for a in p["accounts"] if p["runtime"] == "claude"}

    def slot(self, account=None, index=0, owner=None, runtime=None):
        path = accounts._slot_path(runtime or self.runtime, account or self.a, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0")
        if owner:
            write_json(accounts._owner_path(path), owner)
        return path

    def hold(self, path):
        handle = try_lock(path)
        self.assertIsNotNone(handle)
        self.addCleanup(unlock, handle)

    def job(self, job_id, account=None, state="running", updated=None, **extra):
        write_json(self.home / "jobs" / job_id / "status.json", {
            "runtime": "claude", "state": state, "account": account,
            "updated_at": self.now if updated is None else updated, **extra})

    def test_every_state_and_precedence(self):
        self.usage({self.a.name: {"cooldown_until": self.now + 300}})
        self.circuit()
        for i in range(2):
            self.hold(self.slot(index=i))
        checks = [(False, False, "off"), (True, False, "logged_out"), (True, True, "unhealthy")]
        for enabled, auth, expected in checks:
            self.a.enabled = enabled
            self.auth(self.a, auth)
            self.assertEqual(self.entries()[self.a.name]["availability"], expected)
        self.circuit(until=self.now - 1)
        self.assertEqual(self.entries()[self.a.name]["availability"], "cooling")
        self.usage({})
        self.assertEqual(self.entries()[self.a.name]["availability"], "recovering")
        self.runtime.circuit_breaker = False
        self.assertEqual(self.entries()[self.a.name]["availability"], "busy")
        self.a.max_jobs = 3
        self.assertEqual(self.entries()[self.a.name]["availability"], "ok")
        self.auth(self.a, None)
        self.assertEqual(self.entries()[self.a.name]["availability"], "unknown")
        self.assertEqual(self.entries()[self.a.name]["busy_slots"], 2)
        self.assertEqual(self.entries()[self.a.name]["capacity"], 3)

    def test_shared_login_cooldown_windows_and_source_observation(self):
        self.usage({self.a.name: {"login": "same-login", "observed_at": self.now - 100,
                                 "windows": {"five_hour": {"utilization": .3, "resets_at": self.now + 1}}},
                    self.b.name: {"login": "same-login", "observed_at": self.now - 4000,
                                 "cooldown_until": self.now + 300, "cooldown_reason": "usage limit",
                                 "windows": {"seven_day": {"utilization": .7, "resets_at": self.now + 30}}}})
        for a in self.entries().values():
            self.assertEqual(a["availability"], "cooling")
            self.assertEqual(a["until"], self.now + 300)
            self.assertEqual([w["used_percent"] for w in a["windows"]], [30, 70])
            self.assertEqual([w["stale"] for w in a["windows"]], [False, True])
            self.assertEqual([w["observed_at"] for w in a["windows"]], [self.now - 100, self.now - 4000])
        self.usage({self.a.name: {"login": "same-login", "cooldown_until": self.now + 50},
                    self.b.name: {"login": "same-login"}})
        self.assertEqual(self.entries()[self.b.name]["availability"], "cooling")

    def test_model_only_circuit_does_not_block_account(self):
        self.circuit(scope="model:opus")
        a = self.entries()[self.a.name]
        self.assertEqual(a["availability"], "ok")
        self.assertEqual(a["unhealthy_models"], [{"model": "opus", "reason": "model at capacity",
                                                 "until": self.now + 120, "availability": "unhealthy"}])

    def test_auth_expired_missing_invalidated_unknown_and_not_inferred_from_usage(self):
        for checked, value in ((self.now - 601, True), (self.now + 1, True), (self.now, "yes")):
            self.auth(self.a, value, checked)
            self.assertEqual(self.entries()[self.a.name]["availability"], "unknown")
        (self.home / "state" / "claude-profiles.json").unlink()
        self.usage({self.a.name: {"windows": {"five_hour": {"utilization": .17}}}})
        self.assertEqual(self.entries()[self.a.name]["availability"], "unknown")
        self.auth(self.a, True)
        self.a.home.mkdir()
        (self.a.home / ".claude.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.entries()[self.a.name]["availability"], "unknown")

    def test_window_conversion_stale_reset_and_missing_times(self):
        self.usage({self.a.name: {"updated": self.now - 60, "windows": {
            "five_hour": {"utilization": .173, "resets_at": self.now + 300},
            "seven_day": {"utilization": .7, "observed_at": self.now - 4000, "resets_at": self.now - 1},
            "other": {"utilization": 0}}}})
        windows = self.entries()[self.a.name]["windows"]
        self.assertEqual([w["used_percent"] for w in windows], [17, 70, 0])
        self.assertEqual([w["reset"] for w in windows], [False, True, False])
        self.assertEqual([w["stale"] for w in windows], [False, True, False])
        self.assertIsNone(windows[-1]["resets_at"])
        self.usage({self.a.name: {"windows": {"five_hour": {"utilization": 1}}}})
        self.assertIsNone(self.entries()[self.a.name]["windows"][0]["observed_at"])
        self.assertTrue(self.entries()[self.a.name]["windows"][0]["stale"])

    def test_kiro_antigravity_no_windows(self):
        for name in ("kiro", "antigravity"):
            self.usage({"primary": {"windows": {"five_hour": {"utilization": .5}}}}, name)
        report = self.report()
        for p in report["providers"]:
            if p["runtime"] in ("kiro", "antigravity"):
                self.assertEqual(p["accounts"][0]["windows"], [])
                self.assertEqual(p["accounts"][0]["availability"], "unknown")

    def test_bad_records_degrade_only_one_account_and_json_is_finite(self):
        for bad in ([], "bad", {"windows": []}, {"windows": {"five_hour": []}}, {"pools": []},
                    {"login": []}, {"pools": {"opus": []}}, {"cooldown_reason": {}},
                    {"cooldown_until": "later"}, {"updated": float("inf")}, {"updated": 10 ** 400},
                    {"windows": {"five_hour": {"utilization": float("nan")}}},
                    {"windows": {"five_hour": {"utilization": True}}},
                    {"windows": {"five_hour": {"utilization": .3, "resets_at": float("-inf")}}}):
            with self.subTest(bad=bad):
                self.usage({self.a.name: bad})
                result = self.entries()
                self.assertEqual(result[self.a.name]["availability"], "unknown")
                self.assertIn("problem", result[self.a.name])
                self.assertEqual(result[self.b.name]["availability"], "ok")
                json.dumps(self.report(), allow_nan=False)
        self.usage({})
        self.circuit(open_until=float("nan"))
        self.assertIn("problem", self.entries()[self.a.name])
        self.assertEqual(self.entries()[self.b.name]["availability"], "ok")

    def test_snapshot_does_not_repair_health_claims_or_touch_any_files(self):
        self.circuit(opened_at=self.now + 300, claim={"job": "old", "generation": 0, "deadline": self.now - 1})
        self.slot(owner={"pid": 999999, "job_id": "dead"})
        # Empty locks must stay empty; stale owners must remain byte-for-byte intact.
        empty = self.slot(index=1)
        empty.write_bytes(b"")
        self.hold(self.slot(account=self.b))
        before = listing(self.home)
        self.report()
        self.assertEqual(listing(self.home), before)
        with patch("agents_router.config.load_config", return_value=self.config), \
                patch("subprocess.run", side_effect=AssertionError("snapshot started a subprocess")), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.main(["usage", "--json"]), 0)
        self.assertEqual(json.loads(stream.getvalue())["schema_version"], 1)
        self.assertEqual(listing(self.home), before)

    def test_anonymous_occupied_lease_blocks_uninstall_without_inventing_jobs(self):
        self.hold(self.slot())
        report = self.report()
        self.assertEqual(report["jobs"]["active"], 0)
        self.assertEqual(report["uninstall_blockers"], [{"kind": "occupied_lease", "job_id": None,
                                                       "runtime": "claude", "account": self.a.name}])

    def test_surviving_worker_without_job_id_is_counted_once_across_leases(self):
        self.slot(owner={"pid": 111, "started": 1})
        self.slot(index=1, owner={"pid": 111, "started": 1})
        before = listing(self.home)
        with patch("agents_router.usage_snapshot.same_process", side_effect=lambda pid, started: pid == 111):
            report = self.report()
        self.assertEqual(report["jobs"]["active"], 1)
        self.assertEqual(report["jobs"]["surviving_workers"], 1)
        self.assertEqual(len(report["uninstall_blockers"]), 1)
        self.assertEqual(listing(self.home), before)

    def test_profile_discovery_and_primary_aliases_use_only_the_cache(self):
        user = self.root / "user"
        folder = user / ".claude-work"
        folder.mkdir(parents=True)
        state = folder / ".claude.json"
        state.write_text(json.dumps({"oauthAccount": {"accountUuid": "fake-login"}}), encoding="utf-8")
        self.runtime.accounts = []
        self.runtime._found = None
        self.runtime.primary = folder
        self.auth(Account("claude-work", folder), True)
        cache = read_json(self.home / "state" / "claude-profiles.json")
        cache[str(folder.resolve())]["stamp"] = state.stat().st_mtime
        write_json(self.home / "state" / "claude-profiles.json", cache)
        self.usage({"primary": {"cooldown_until": self.now + 300}})
        before = listing(self.home)
        with patch.dict(os.environ, {"HOME": str(user), "USERPROFILE": str(user)}):
            report = self.report()
        account = report["providers"][0]["accounts"][0]
        self.assertEqual((account["name"], account["availability"]), ("claude-work", "cooling"))
        self.assertEqual(len(report["providers"][0]["accounts"]), 1)
        self.assertEqual(listing(self.home), before)

    def test_missing_home_plain_cli_and_subprocess_contract_create_nothing(self):
        missing = self.root / "missing-agt"
        with patch.dict(os.environ, {"AGT_HOME": str(missing)}):
            before = listing(missing)
            output = subprocess.run([sys.executable, "-m", "agents_router", "usage", "--json"],
                                    capture_output=True, text=True, check=True, cwd=self.project)
            report = json.loads(output.stdout)
            self.assertEqual(report["jobs"]["active"], 0)
            self.assertEqual(report["uninstall_blockers"], [])
            self.assertTrue(all(a["availability"] == "unknown" for p in report["providers"] for a in p["accounts"]))
            self.assertEqual(listing(missing), before)

    def test_plain_cli_with_workspace_discovered_profile_never_probes_or_writes(self):
        user = self.root / "user"
        folder = user / ".claude-work"
        folder.mkdir(parents=True)
        (folder / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": "fake-login"}}), encoding="utf-8")
        self.write_config(accounts=())
        settings = self.project / ".agt" / "settings.toml"
        settings.parent.mkdir()
        settings.write_text('[claude]\naccounts = ["claude-work"]\n', encoding="utf-8")
        previous_cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous_cwd)
        with patch.dict(os.environ, {"HOME": str(user), "USERPROFILE": str(user)}):
            for args in (["usage"], ["usage", "--json"]):
                with self.subTest(args=args):
                    (self.home / "state" / "claude-profiles.json").unlink(missing_ok=True)
                    before = listing(self.root)
                    with patch("agents_router.runtimes.claude.ClaudeRuntime.auth_status",
                               return_value={"loggedIn": True}) as auth, \
                            patch("subprocess.run", side_effect=AssertionError("usage started a subprocess")) as run, \
                            patch("subprocess.Popen", side_effect=AssertionError("usage started a worker")) as popen, \
                            contextlib.redirect_stdout(io.StringIO()) as stream:
                        self.assertEqual(cli.main(args), 0)
                    auth.assert_not_called()
                    run.assert_not_called()
                    popen.assert_not_called()
                    self.assertEqual(listing(self.root), before)
                    if "--json" in args:
                        account = next(a for p in json.loads(stream.getvalue())["providers"]
                                       if p["runtime"] == "claude" for a in p["accounts"]
                                       if a["name"] == "claude-work")
                        self.assertEqual(account["availability"], "unknown")
                    else:
                        self.assertIn("Claude Code claude-work unknown", stream.getvalue())

    def test_populated_home_real_cli_entrypoint_is_read_only(self):
        self.write_config(accounts=("claude-work", "claude-other"))
        self.circuit(claim={"job": "expired", "generation": 0, "deadline": self.now - 1})
        self.slot(owner={"pid": 999999, "job_id": "dead"})
        self.usage({self.a.name: {"observed_at": self.now, "windows": {
            "five_hour": {"utilization": .42, "resets_at": self.now + 300}}}})
        before = listing(self.home)
        completed = subprocess.run([sys.executable, "-m", "agents_router", "usage", "--json"],
                                   capture_output=True, text=True, check=True, cwd=self.project)
        report = json.loads(completed.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(listing(self.home), before)
        self.assertEqual(self.records(), [])

    def test_malformed_whole_store_is_reported_without_repairs(self):
        path = self.home / "state" / "claude-usage.json"
        path.write_text("{truncated", encoding="utf-8")
        before = listing(self.home)
        entries = self.entries()
        self.assertTrue(all(a["availability"] == "unknown" and a.get("problem") for a in entries.values()))
        self.assertEqual(listing(self.home), before)

    def test_occupied_leases_survive_runner_removed_account_and_unassigned_jobs(self):
        self.job("waiting", state="queued")
        self.job("removed", account="removed-account", runner_pid=111)
        self.job("orphan", account=self.a.name, updated=self.now - 60, child_pid=111, child_started=1)
        self.job("dead", account=self.b.name, updated=self.now - 60, runner_pid=222)
        self.job("done-worker", account="removed-account", state="done", child_pid=111, child_started=1)
        self.slot(owner={"pid": 111, "started": 1, "job_id": "orphan"})
        removed = Account("gone", self.root / "gone")
        self.slot(account=removed, owner={"pid": 111, "started": 1, "job_id": "no-record"})
        # Execution caps and the account's lease must not count one job twice.
        execution = self.home / "state" / "leases" / "execution" / "global" / "0.lock"
        execution.parent.mkdir(parents=True)
        execution.write_bytes(b"\0")
        write_json(accounts._owner_path(execution), {"pid": 111, "job_id": "orphan"})
        before = listing(self.home)
        with patch("agents_router.usage_snapshot.same_process", side_effect=lambda pid, started: pid == 111):
            report = self.report()
        self.assertEqual(report["jobs"], {"active": 5, "queued": 1, "starting": 0, "running": 4,
                                          "unassigned": 2, "removed_accounts": 2, "surviving_workers": 3})
        self.assertEqual(len(report["uninstall_blockers"]), 5)
        entry = next(a for a in report["providers"][0]["accounts"] if a["name"] == self.a.name)
        self.assertEqual((entry["running_jobs"], entry["busy_slots"], entry["capacity"]), (1, 1, 2))
        self.assertEqual(listing(self.home), before)

    def test_human_canonical_names_back_and_reset_beats_stale(self):
        self.usage({self.a.name: {"cooldown_until": self.now + 300, "observed_at": self.now - 4000,
                                 "windows": {"five_hour": {"utilization": .4, "resets_at": self.now - 1},
                                             "seven_day": {"utilization": .6, "resets_at": self.now + 10}}}})
        output = "\n".join(usage_snapshot.lines(self.report()))
        self.assertIn("Claude Code claude-work cooling · back ", output)
        self.assertNotIn("Claude Code primary", output)
        self.assertIn("5h 40% used · resets ", output)
        self.assertIn(" · reset · week 60% used", output)
        self.assertIn(" · stale", output)

    def test_free_refresh_updates_codex_only_on_request(self):
        runtime = self.runtimes["codex"]
        folder = runtime.primary / "sessions" / "2026" / "11" / "10"
        folder.mkdir(parents=True)
        rollout = folder / "rollout-fake.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 97, "window_minutes": 300, "resets_at": self.now + 100}}}}) + "\n",
                           encoding="utf-8")
        os.utime(rollout, (self.now - 10, self.now - 10))
        with patch("agents_router.config.load_config", return_value=self.config), \
                patch("agents_router.runtimes.codex.probe_rate_limits") as probe:
            before = listing(self.home)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["usage", "--json"]), 0)
            self.assertEqual(listing(self.home), before)
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(cli.main(["usage", "--json", "--refresh"]), 0)
            data = json.loads(stream.getvalue())
            window = next(p for p in data["providers"] if p["runtime"] == "codex")["accounts"][0]["windows"][0]
            self.assertEqual(window["used_percent"], 97)
            self.assertEqual(accounts.load_usage("codex")["primary"]["observed_at"], self.now - 10)
            probe.assert_not_called()  # even a held-back account only rescans local rollouts

    def test_fixture_is_generated_by_the_producer_and_repeatable(self):
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), fixture_text())
        self.assertEqual(fixture_text(), fixture_text())

    def test_read_only_lock_probe_never_creates_a_missing_or_empty_lock(self):
        missing = self.home / "missing" / "slot.lock"
        self.assertFalse(lock_busy(missing))
        self.assertFalse(missing.parent.exists())
        path = self.slot()
        path.write_bytes(b"")
        before = listing(self.home)
        self.assertFalse(lock_busy(path))
        self.assertEqual(listing(self.home), before)
        self.hold(path)
        self.assertTrue(lock_busy(path))


if __name__ == "__main__":
    unittest.main()
