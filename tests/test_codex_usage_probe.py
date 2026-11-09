"""Live Codex quota refresh uses the fake app-server and isolated account homes only."""

import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import __version__, accounts, cli, jobs, limits, tools, usage_view
from agents_router.config import Account, load_config, state_dir
from agents_router.fsutil import file_lock, worker_process, write_json
from agents_router.runtimes.base import JobSpec
from agents_router.runtimes.codex import CodexParser, CodexRuntime, FILE_AUTH, probe_rate_limits


class CodexUsageProbeTests(TempHome):
    def setUp(self):
        super().setUp()
        self.codex_home = self.root / "custom-codex"
        self.codex_home.mkdir()
        (self.home / "settings.toml").write_text(
            f'[codex]\nprimary = {json.dumps(str(self.codex_home))}\n', encoding="utf-8")
        self.config = load_config()
        self.runtime = self.config.runtime("codex")
        self.account = self.runtime.find("primary")
        self.now = time.time()
        self.requests = self.root / "requests.jsonl"
        self.enterContext(patch.dict(os.environ, {
            "AGT_CODEX_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
            "FAKE_CODEX_REQUESTS": str(self.requests), "FAKE_CODEX_APP_SERVER": "eof",
        }))

    def reply(self, used=0, reached=None, *, minutes=10080, secondary=None):
        result = {"ordinaryUsageAllowed": True, "rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": minutes,
                                            "resetsAt": self.now + 7 * 86400},
            "secondary": secondary, "rateLimitReachedType": reached}}
        os.environ["FAKE_CODEX_APP_SERVER"] = json.dumps(result)
        return result

    def pool(self, *names):
        text = f'[codex]\nprimary = {json.dumps(str(self.codex_home))}\ninclude_primary = true\n'
        for name in names:
            home = self.root / name
            home.mkdir()
            text += f'\n[[codex.accounts]]\nname = "{name}"\nhome = {json.dumps(str(home))}\n'
            accounts.update_usage("codex", name, windows={
                "seven_day": {"utilization": 0.95, "resets_at": self.now + 86400}})
        (self.home / "settings.toml").write_text(text, encoding="utf-8")
        self.config = load_config()
        self.runtime = self.config.runtime("codex")
        self.account = self.runtime.find("primary")

    def role(self, provider="codex"):
        with open(self.home / "settings.toml", "a", encoding="utf-8") as handle:
            handle.write(f'\n[roles.builder]\nproviders = ["{provider}", "claude:a1"]\n'
                         '\n[[claude.accounts]]\nname = "a1"\nhome = '
                         + json.dumps(str(self.root / "claude-a1")) + '\n')

    def seed(self, used=0.95, **fields):
        data = accounts.load_usage("codex")
        data.pop("primary", None)
        write_json(accounts._usage_path("codex"), data)
        return accounts.update_usage("codex", "primary", windows={
            "seven_day": {"utilization": used, "resets_at": self.now + 3 * 86400}},
            observed_at=self.now - 3600, **fields)

    def usage_record(self):
        return accounts.load_usage("codex")["primary"]

    def probes(self):
        return [record for record in self.records() if record["argv"][:1] == ["app-server"]]

    def room(self):
        return limits.room(self.config, "codex", limits.Counts(), pinned=None, model=None, now=time.time())

    def run_cli(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(list(argv))
        return code, output.getvalue()

    def test_early_reset_restores_admission_and_records_fresh_observation(self):
        self.seed()
        self.assertEqual(self.room().accounts, [])
        self.reply()
        usage_view.refresh_codex(self.config)
        admission = limits.admit(self.config, "codex")
        self.assertEqual(admission.account, "primary")
        record = self.usage_record()
        self.assertEqual(record["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(record["windows"]["seven_day"]["resets_at"], self.now + 7 * 86400)
        self.assertGreaterEqual(record["observed_at"], self.now)
        self.assertGreaterEqual(record["probed_at"], self.now)
        self.assertEqual([account.name for account, _ in self.room().accounts], ["primary"])

    def test_probe_matches_ids_ignores_notifications_and_uses_account_home(self):
        self.reply(12, minutes=300, secondary={"usedPercent": 25, "windowDurationMins": 10080,
                                            "resetsAt": self.now + 100})
        result = probe_rate_limits(self.runtime, self.account)
        self.assertEqual(result.windows, {
            "five_hour": {"utilization": 0.12, "resets_at": self.now + 7 * 86400},
            "seven_day": {"utilization": 0.25, "resets_at": self.now + 100}})
        self.assertIsNone(result.reached_type)
        self.assertGreaterEqual(result.observed_at, self.now)
        self.assertLessEqual(result.observed_at, time.time())
        self.assertEqual(self.probes()[0]["env"]["CODEX_HOME"], str(self.codex_home))
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual([request["method"] for request in requests],
                         ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(requests[0]["params"]["clientInfo"]["version"], __version__)
        self.assertIsNone(requests[2]["params"])

    def test_extra_account_probe_uses_the_workers_file_auth_override(self):
        extra = Account("work", self.root / "codex-work")
        extra.home.mkdir()
        (extra.home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        self.reply()
        self.assertIsNotNone(probe_rate_limits(self.runtime, extra))
        adapter = CodexRuntime()
        spec = JobSpec("test-auth", "codex", "test", str(self.project))
        worker = adapter.launch(self.runtime, extra, spec, spec.prompt, {})
        auth = adapter._auth_args(extra)
        self.assertEqual(auth, ["-c", FILE_AUTH])
        self.assertEqual(self.probes()[0]["argv"], ["app-server", *auth])
        index = worker.argv.index(FILE_AUTH)
        self.assertEqual(worker.argv[index - 1:index + 1], auth)

    def test_three_hanging_accounts_cost_at_most_one_probe_timeout_per_refresh(self):
        self.seed()
        self.pool("work-1", "work-2")
        os.environ["FAKE_CODEX_APP_SERVER"] = "hang"
        with patch("agents_router.runtimes.codex.probe_rate_limits",
                   side_effect=lambda *args, **kw: probe_rate_limits(*args, timeout=min(0.4, kw["timeout"]))):
            started = time.monotonic()
            usage_view.refresh_codex(self.config)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(self.probes()), 3)
            usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 3)
        self.assertEqual({Path(p["env"]["CODEX_HOME"]).name for p in self.probes()},
                         {self.codex_home.name, "work-1", "work-2"})

    def test_hanging_first_account_does_not_hide_recovered_second_from_role(self):
        self.seed()
        self.pool("work")
        self.role()
        self.reply()
        os.environ["FAKE_CODEX_APP_SERVERS"] = json.dumps({self.codex_home.name: "hang"})
        with patch("agents_router.jobs.spawn_detached", return_value=999999), \
                patch("agents_router.role_run.logged_in", return_value=True):
            started = time.monotonic()
            code, output = self.run_cli("start", "--role", "builder", "--cwd", str(self.project), "test")
        self.assertLess(time.monotonic() - started, 11)
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertEqual([job["provider"] for job in result["jobs"]], ["codex"])
        job = result["jobs"][0]["job_id"]
        self.assertEqual(jobs.read_status(job)["assigned_account"], "work")
        self.assertEqual(len(self.probes()), 2)
        self.assertEqual(accounts.load_usage("codex")["work"]["windows"]["seven_day"]["utilization"], 0)
        jobs.update_status(job, state="done")

    def test_refresh_does_not_wait_for_cleanup_past_shared_deadline(self):
        self.seed()
        self.pool("work")
        release = threading.Event()
        finished = threading.Event()
        result = probe_rate_limits
        self.reply()
        timeouts = []

        def delayed_probe(runtime, account, *, timeout):
            timeouts.append(timeout)
            if account.name == "primary":
                try:
                    release.wait(3)
                    return None
                finally:
                    finished.set()
            return result(runtime, account, timeout=timeout)

        with patch("agents_router.usage_view.CODEX_PROBE_TIMEOUT_SECONDS", 0.3), \
                patch("agents_router.runtimes.codex.probe_rate_limits", side_effect=delayed_probe):
            try:
                started = time.monotonic()
                usage_view.refresh_codex(self.config)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertFalse(finished.is_set())
                self.assertEqual(accounts.load_usage("codex")["work"]["windows"]["seven_day"]["utilization"], 0)
                self.assertEqual(len(timeouts), 2)
                self.assertTrue(all(0 < timeout <= 0.3 for timeout in timeouts))
            finally:
                release.set()
                self.assertTrue(finished.wait(2))

    def test_refresh_subprocess_exits_without_joining_a_hanging_probe(self):
        self.seed()
        script = """
import threading
from agents_router import usage_view
from agents_router.config import load_config
from agents_router.runtimes import codex

entered = threading.Event()
def hang(*args, **kwargs):
    entered.set()
    threading.Event().wait(30)

codex.probe_rate_limits = hang
usage_view.CODEX_PROBE_TIMEOUT_SECONDS = 0.3
usage_view.refresh_codex(load_config())
assert entered.is_set(), 'probe did not start'
print('refreshed')
"""
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "refreshed")

    def test_probe_reservation_uses_remaining_deadline_for_usage_lock(self):
        self.seed()
        with file_lock(accounts._usage_path("codex").with_suffix(".lock")):
            started = time.monotonic()
            result = usage_view.probe_codex(self.runtime, self.account, deadline=started + 0.2)
            self.assertLess(time.monotonic() - started, 0.7)
        self.assertIsNone(result)
        self.assertNotIn("probed_at", self.usage_record())
        self.assertEqual(self.probes(), [])

    def test_probe_storage_uses_remaining_deadline_for_usage_lock(self):
        self.seed()
        self.reply()
        result = probe_rate_limits(self.runtime, self.account)
        self.assertIsNotNone(result)
        with contextlib.ExitStack() as locks:
            def contended_probe(*args, **kwargs):
                locks.enter_context(file_lock(accounts._usage_path("codex").with_suffix(".lock")))
                return result

            with patch("agents_router.runtimes.codex.probe_rate_limits", side_effect=contended_probe):
                started = time.monotonic()
                stored = usage_view.probe_codex(self.runtime, self.account, deadline=started + 0.2)
                self.assertLess(time.monotonic() - started, 0.7)
        self.assertIsNone(stored)
        self.assertIn("probed_at", self.usage_record())
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0.95)

    def test_start_probes_before_admission_and_usage_locks_are_held(self):
        self.seed()
        self.reply()
        checked = []

        def unlocked_probe(*args, **kwargs):
            with file_lock(state_dir() / "admission.lock", timeout=0.05), \
                    file_lock(accounts._usage_path("codex").with_suffix(".lock"), timeout=0.05):
                checked.append(True)
            return probe_rate_limits(*args, **kwargs)

        with patch("agents_router.runtimes.codex.probe_rate_limits", side_effect=unlocked_probe), \
                patch("agents_router.jobs.spawn_detached", return_value=999999):
            code, output = self.run_cli("start", "--runtime", "codex", "--cwd", str(self.project), "test")
        self.assertEqual(code, 0, output)
        self.assertEqual(checked, [True])
        status = json.loads(output)
        self.assertEqual(jobs.read_status(status["job_id"])["assigned_account"], "primary")
        jobs.update_status(status["job_id"], state="done")

    def test_admission_and_room_note_never_probe(self):
        self.seed()
        self.reply()
        with patch("agents_router.runtimes.codex.probe_rate_limits") as probe:
            with self.assertRaises(limits.LimitError):
                limits.admit(self.config, "codex")
            self.assertIn("Room:", tools.room_note(["codex"]))
        probe.assert_not_called()
        self.assertEqual(self.probes(), [])

    def test_role_refreshes_before_skipping_its_first_codex_provider(self):
        self.seed()
        self.reply()
        with open(self.home / "settings.toml", "a", encoding="utf-8") as handle:
            handle.write('\n[roles.builder]\nproviders = ["codex", "claude:a1"]\n'
                         '\n[[claude.accounts]]\nname = "a1"\nhome = '
                         + json.dumps(str(self.root / "claude-a1")) + '\n')
        with patch("agents_router.jobs.spawn_detached", return_value=999999), \
                patch("agents_router.role_run.logged_in", return_value=True):
            code, output = self.run_cli("start", "--role", "builder", "--cwd", str(self.project), "test")
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertEqual([job["provider"] for job in result["jobs"]], ["codex"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(self.probes()), 1)
        jobs.update_status(result["jobs"][0]["job_id"], state="done")

    def test_pinned_job_and_role_submit_the_named_account_first(self):
        self.pool("work")
        self.role("codex:work")
        self.reply()
        os.environ["FAKE_CODEX_APP_SERVERS"] = json.dumps({
            self.codex_home.name: json.dumps(self.reply(95)), "work": json.dumps(self.reply())})
        thread = usage_view.Thread
        submitted = []

        def ordered_thread(*args, **kwargs):
            submitted.append(kwargs["args"][1].name)
            return thread(*args, **kwargs)

        for arguments in (("--runtime", "codex", "--account", "work"), ("--role", "builder")):
            with self.subTest(arguments=arguments):
                self.seed()
                accounts.update_usage("codex", "work", probed_at=0, windows={
                    "seven_day": {"utilization": 0.95, "resets_at": self.now + 86400}})
                submitted.clear()
                with patch("agents_router.jobs.spawn_detached", return_value=999999), \
                        patch("agents_router.role_run.logged_in", return_value=True), \
                        patch("agents_router.usage_view.Thread", side_effect=ordered_thread):
                    code, output = self.run_cli("start", *arguments, "--cwd", str(self.project), "test")
                self.assertEqual(code, 0, output)
                self.assertEqual(submitted, ["work", "primary"])
                result = json.loads(output)
                job = result["jobs"][0]["job_id"] if "jobs" in result else result["job_id"]
                self.assertEqual(jobs.read_status(job)["assigned_account"], "work")
                jobs.update_status(job, state="done")

    def test_failed_garbage_and_timed_out_probes_stay_held_and_are_throttled(self):
        real_probe = probe_rate_limits
        for scenario in ("eof", "error", "garbage", "hang"):
            with self.subTest(scenario=scenario):
                self.seed(probed_at=0)
                os.environ["FAKE_CODEX_APP_SERVER"] = scenario
                started = time.monotonic()
                with patch("agents_router.runtimes.codex.probe_rate_limits",
                           side_effect=lambda *args, **kw: real_probe(*args, timeout=min(0.2, kw["timeout"]))):
                    usage_view.refresh_codex(self.config)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0.95)
                self.assertGreaterEqual(self.usage_record()["probed_at"], self.now)
                self.assertEqual(self.room().accounts, [])
                count = len(self.probes())
                usage_view.refresh_codex(self.config)
                self.assertEqual(len(self.probes()), count)

    def test_unknown_shapes_and_missing_binary_never_raise(self):
        for result in (None, [], {}, {"rateLimits": []}, {"rateLimits": {"primary": {}}},
                       {"rateLimits": {"rateLimitReachedType": None, "primary": {
                           "usedPercent": "zero", "windowDurationMins": 10080}}}):
            with self.subTest(result=result):
                os.environ["FAKE_CODEX_APP_SERVER"] = json.dumps(result)
                self.assertIsNone(probe_rate_limits(self.runtime, self.account, timeout=1))
        with patch.object(CodexRuntime, "find_binary", return_value=None):
            self.assertIsNone(probe_rate_limits(self.runtime, self.account))
        with patch.object(CodexRuntime, "find_binary", return_value=[str(self.root / "missing")]):
            self.assertIsNone(probe_rate_limits(self.runtime, self.account))

    def test_probe_throttles_for_ten_minutes_then_retries(self):
        self.seed()
        self.reply(95)
        usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 1)
        first = self.usage_record()["probed_at"]
        with patch("agents_router.usage_view.time.time", return_value=first + 599):
            usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 1)
        with patch("agents_router.usage_view.time.time", return_value=first + 600):
            usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 2)

    def test_below_ceiling_or_expired_window_never_probes(self):
        self.reply()
        self.seed(0.94)
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.probes(), [])
        accounts.update_usage("codex", "primary", windows={
            "seven_day": {"utilization": 1, "resets_at": self.now - 1}})
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.probes(), [])

    def test_custom_ceiling_is_used(self):
        self.runtime.usage_ceiling = 0.9
        self.seed(0.9)
        self.reply(89)
        usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0.89)

    def test_only_listed_held_accounts_are_probed_in_their_own_home(self):
        self.seed(0.2)
        extra = Account("work", self.root / "codex-work")
        extra.home.mkdir()
        self.runtime.accounts.append(extra)
        for name in ("work", "unlisted"):
            accounts.update_usage("codex", name, windows={
                "seven_day": {"utilization": 0.95, "resets_at": self.now + 86400}})
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 1)
        self.assertEqual(self.probes()[0]["env"]["CODEX_HOME"], str(extra.home))
        usage = accounts.load_usage("codex")
        self.assertEqual(usage["work"]["windows"]["seven_day"]["utilization"], 0)
        self.assertNotIn("probed_at", usage["primary"])
        self.assertNotIn("probed_at", usage["unlisted"])

    def test_failed_probe_keeps_legacy_observation_age(self):
        observed = self.now - 3600
        write_json(accounts._usage_path("codex"), {"primary": {"windows": {
            "seven_day": {"utilization": 0.95, "resets_at": self.now + 86400}}, "updated": observed}})
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["observed_at"], observed)

    def test_usage_cooldown_without_windows_is_probed_and_cleared(self):
        accounts.set_cooldown("codex", "primary", self.now + 3 * 86400, "You've hit your usage limit")
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertIsNone(accounts.cooldown_until(self.usage_record()))
        self.assertEqual(limits.admit(self.config, "codex").account, "primary")

    def test_full_or_reached_account_keeps_usage_cooldown(self):
        for used, reached in ((95, None), (0, "weekly")):
            with self.subTest(used=used, reached=reached):
                self.seed(probed_at=0)
                accounts.set_cooldown("codex", "primary", self.now + 86400, "usage_limit")
                self.reply(used, reached)
                usage_view.refresh_codex(self.config)
                self.assertIsNotNone(accounts.cooldown_until(self.usage_record()))

    def test_other_cooldown_and_pool_cooldowns_are_preserved(self):
        self.seed()
        accounts.set_cooldown("codex", "primary", self.now + 3600, "maintenance")
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["cooldown_reason"], "maintenance")
        self.assertIsNotNone(accounts.cooldown_until(self.usage_record()))
        accounts.update_usage("codex", "primary", probed_at=0)
        accounts.set_cooldown("codex", "primary", self.now + 3600, "usage limit")
        accounts.set_pool_cooldown("codex", "primary", "special", self.now + 3600, "pool limit")
        usage_view.refresh_codex(self.config)
        self.assertIsNone(accounts.cooldown_until(self.usage_record()))
        self.assertIsNotNone(accounts.pool_cooldown_until(self.usage_record(), "special"))

    def test_new_usage_cooldown_set_during_probe_is_preserved(self):
        self.seed()
        accounts.set_cooldown("codex", "primary", self.now + 3600, "usage_limit")
        self.reply()
        new_until = self.now + 7200

        def interleaved_probe(*args, **kwargs):
            result = probe_rate_limits(*args, **kwargs)
            accounts.set_cooldown("codex", "primary", new_until, "usage_limit")
            return result

        with patch("agents_router.runtimes.codex.probe_rate_limits", side_effect=interleaved_probe):
            usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(self.usage_record()["cooldown_until"], new_until)

    def test_identical_usage_cooldown_set_during_probe_is_preserved(self):
        self.seed()
        until = self.now + 3600
        accounts.set_cooldown("codex", "primary", until, "usage_limit")
        revision = self.usage_record()["cooldown_revision"]
        self.reply()

        def interleaved_probe(*args, **kwargs):
            result = probe_rate_limits(*args, **kwargs)
            accounts.set_cooldown("codex", "primary", until, "usage_limit")
            return result

        with patch("agents_router.runtimes.codex.probe_rate_limits", side_effect=interleaved_probe):
            usage_view.refresh_codex(self.config)
        record = self.usage_record()
        self.assertEqual(record["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(record["cooldown_until"], until)
        self.assertEqual(record["cooldown_reason"], "usage_limit")
        self.assertNotEqual(record["cooldown_revision"], revision)

    def test_legacy_usage_cooldown_without_revision_is_preserved(self):
        self.seed()
        until = self.now + 3600
        data = accounts.load_usage("codex")
        data["primary"].update(cooldown_until=until, cooldown_reason="usage_limit")
        write_json(accounts._usage_path("codex"), data)
        self.reply()
        usage_view.refresh_codex(self.config)
        record = self.usage_record()
        self.assertEqual(record["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(record["cooldown_until"], until)
        self.assertNotIn("cooldown_revision", record)

    def test_cooldown_set_just_after_probe_write_is_preserved(self):
        self.seed()
        accounts.set_cooldown("codex", "primary", self.now + 3600, "usage_limit")
        self.reply()
        update = accounts.update_usage
        new_until = self.now + 7200

        def interleaved_update(*args, **kwargs):
            record = update(*args, **kwargs)
            if kwargs.get("windows") is not None:
                accounts.set_cooldown("codex", "primary", new_until, "usage_limit")
            return record

        with patch("agents_router.accounts.update_usage", side_effect=interleaved_update):
            usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["cooldown_until"], new_until)

    def test_non_usage_cooldown_alone_never_probes(self):
        self.seed(0.2)
        accounts.set_cooldown("codex", "primary", self.now + 3600, "maintenance")
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.probes(), [])

    def test_older_rollout_does_not_overwrite_live_windows(self):
        folder = self.codex_home / "sessions" / "2026" / "11" / "09"
        folder.mkdir(parents=True)
        path = folder / "rollout-old.jsonl"
        path.write_text(json.dumps({"payload": {"rate_limits": {"primary": {
            "used_percent": 95, "window_minutes": 10080, "resets_at": self.now + 86400}}}}) + "\n")
        os.utime(path, (self.now - 60, self.now - 60))
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertEqual(len(self.probes()), 1)

        usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(len(self.probes()), 1)

    def test_parser_and_rollout_writers_preserve_live_data_until_a_newer_snapshot(self):
        folder = self.codex_home / "sessions" / "2026" / "11" / "09"
        folder.mkdir(parents=True)
        path = folder / "rollout-test-thread-test.jsonl"
        parser = CodexParser("primary", self.codex_home)
        parser.feed({"type": "thread.started", "thread_id": "thread-test"})

        def snapshot(used, observed):
            path.write_text(json.dumps({"payload": {"rate_limits": {"primary": {
                "used_percent": used, "window_minutes": 10080, "resets_at": self.now + 86400}}}}) + "\n")
            os.utime(path, (observed, observed))

        for writer in (lambda: usage_view.refresh_codex(self.config, live=False), parser._record_usage):
            with self.subTest(writer=writer):
                path.unlink(missing_ok=True)
                self.seed()
                self.reply()
                usage_view.refresh_codex(self.config)
                live_observed = self.usage_record()["observed_at"]
                snapshot(95, live_observed - 60)
                writer()
                record = self.usage_record()
                self.assertEqual(record["windows"]["seven_day"]["utilization"], 0)
                self.assertEqual(record["observed_at"], live_observed)
                snapshot(96, live_observed + 60)
                writer()
                record = self.usage_record()
                self.assertEqual(record["windows"]["seven_day"]["utilization"], 0.96)
                self.assertEqual(record["observed_at"], path.stat().st_mtime)

    def test_unrelated_rollout_append_does_not_freshen_an_old_usage_event(self):
        folder = self.codex_home / "sessions" / "2026" / "11" / "09"
        folder.mkdir(parents=True)
        path = folder / "rollout-test-thread-test.jsonl"
        parser = CodexParser("primary", self.codex_home)
        parser.feed({"type": "thread.started", "thread_id": "thread-test"})
        timestamp = datetime.fromtimestamp(self.now - 60, timezone.utc).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps({"timestamp": timestamp, "payload": {"rate_limits": {"primary": {
            "used_percent": 95, "window_minutes": 10080, "resets_at": self.now + 86400}}}}) + "\n")
        self.seed()
        self.reply()
        usage_view.refresh_codex(self.config)
        observed = self.usage_record()["observed_at"]
        with path.open("a") as handle:
            handle.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                                     "payload": {"type": "agent_message", "message": "unrelated"}}) + "\n")
        os.utime(path, (observed + 60, observed + 60))
        for writer in (lambda: usage_view.refresh_codex(self.config, live=False), parser._record_usage):
            with self.subTest(writer=writer):
                writer()
                self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0)
                self.assertEqual(self.usage_record()["observed_at"], observed)

    def test_rollout_refresh_does_not_reset_probe_throttle(self):
        self.seed(probed_at=self.now)
        folder = self.codex_home / "sessions" / "2026" / "11" / "09"
        folder.mkdir(parents=True)
        (folder / "rollout-new.jsonl").write_text(json.dumps({"payload": {"rate_limits": {"primary": {
            "used_percent": 99, "window_minutes": 10080, "resets_at": self.now + 86400}}}}) + "\n")
        self.reply()
        usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0.99)
        self.assertEqual(self.usage_record()["probed_at"], self.now)
        self.assertEqual(self.probes(), [])

    def test_rollout_cannot_overwrite_a_live_result_from_a_concurrent_refresh(self):
        self.seed()
        folder = self.codex_home / "sessions" / "2026" / "11" / "09"
        folder.mkdir(parents=True)
        path = folder / "rollout-old.jsonl"
        path.touch()
        os.utime(path, (self.now - 60, self.now - 60))
        self.reply()

        def interleaved_read(_path):
            usage_view.probe_codex(self.runtime, self.account, force=True)
            return ({"primary": {"used_percent": 95, "window_minutes": 10080,
                                 "resets_at": self.now + 86400}}, self.now - 60)

        with patch("agents_router.runtimes.codex.last_rate_limits", side_effect=interleaved_read):
            usage_view.refresh_codex(self.config)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0)
        self.assertEqual(len(self.probes()), 1)

    def test_accounts_reset_forces_probe_even_when_recent_and_below_ceiling(self):
        self.seed(0.1, probed_at=self.now)
        self.reply()
        code, output = self.run_cli("accounts", "reset", "primary", "--runtime", "codex")
        self.assertEqual(code, 0)
        self.assertIn("usage now 0% of seven_day", output)
        self.assertNotIn("nothing to clear", output)
        self.assertEqual(len(self.probes()), 1)

    def test_probe_receipt_time_is_not_advanced_while_waiting_to_store(self):
        self.seed()
        self.reply()
        accounts.set_cooldown("codex", "primary", self.now + 3600, "usage_limit")
        calls = 0

        @contextlib.contextmanager
        def interleaved_lock(path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # another writer updates after receipt, before the probe stores
                accounts.update_usage("codex", "primary", windows={
                    "seven_day": {"utilization": 0.96, "resets_at": self.now + 86400}},
                    observed_at=self.now + 1)
                clock.return_value = self.now + 2
            with file_lock(path, **kwargs):
                yield

        with patch("agents_router.usage_view.time.time", return_value=self.now) as clock, \
                patch("agents_router.accounts.file_lock", side_effect=interleaved_lock):
            self.assertIsNotNone(usage_view.probe_codex(self.runtime, self.account))
        record = self.usage_record()
        self.assertEqual(record["windows"]["seven_day"]["utilization"], 0.96)
        self.assertEqual(record["observed_at"], self.now + 1)
        self.assertIsNotNone(accounts.cooldown_until(record))

    def test_probe_receipt_time_precedes_subprocess_cleanup(self):
        self.seed()
        self.reply()

        @contextlib.contextmanager
        def interleaved_cleanup(*args, **kwargs):
            with worker_process(*args, **kwargs) as process:
                yield process
                accounts.update_usage("codex", "primary", windows={
                    "seven_day": {"utilization": 0.96, "resets_at": self.now + 86400}},
                    observed_at=self.now + 1)
                clock.return_value = self.now + 2

        with patch("agents_router.runtimes.codex.time.time", return_value=self.now) as clock, \
                patch("agents_router.runtimes.codex.worker_process", side_effect=interleaved_cleanup):
            result = usage_view.probe_codex(self.runtime, self.account)
        self.assertEqual(result.observed_at, self.now)
        self.assertEqual(self.usage_record()["windows"]["seven_day"]["utilization"], 0.96)
        self.assertEqual(self.usage_record()["observed_at"], self.now + 1)

    def test_accounts_reset_reports_probe_failure_in_one_line(self):
        code, output = self.run_cli("accounts", "reset", "primary", "--runtime", "codex")
        self.assertEqual((code, output), (0, "codex account 'primary': usage check failed\n"))
        self.assertGreaterEqual(self.usage_record()["probed_at"], self.now)

    def test_accounts_reset_preserves_cooldown_clear_report(self):
        accounts.set_cooldown("codex", "primary", self.now + 3600, "usage_limit")
        self.reply()
        code, output = self.run_cli("accounts", "reset", "primary", "--runtime", "codex")
        self.assertEqual(code, 0)
        self.assertIn("cooldown cleared; usage now 0% of seven_day", output)

    def test_accounts_plain_and_json_report_ceiling_hold_and_reset(self):
        self.seed(probed_at=self.now)
        with patch.object(CodexRuntime, "version", return_value="fake"), \
                patch.object(CodexRuntime, "auth_status", return_value={"loggedIn": True}):
            code, output = self.run_cli("accounts", "--runtime", "codex")
            self.assertEqual(code, 0)
            self.assertIn("held back: at the 95% usage ceiling until ", output)
            self.assertIn(usage_view.reset_text(self.now + 3 * 86400, time.time()), output)
            self.assertNotIn("\x1b[", output)
            code, output = self.run_cli("accounts", "--runtime", "codex", "--json")
            entry = json.loads(output)["accounts"][0]
            self.assertTrue(entry["held_back"])
            self.assertIn("95% usage ceiling", entry["held_back_reason"])
            self.seed(0.94)
            _, output = self.run_cli("accounts", "--runtime", "codex", "--json")
            entry = json.loads(output)["accounts"][0]
            self.assertFalse(entry["held_back"])
            self.assertNotIn("held_back_reason", entry)


if __name__ == "__main__":
    unittest.main()
