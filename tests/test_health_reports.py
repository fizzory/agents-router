"""Circuit-breaker reports, reset, launch and explicit live checks use isolated homes and fakes."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, cli, health, health_view, jobs, limits, quota, tools, ui, usage_view
from agents_router.config import load_config
from agents_router.fsutil import read_json, unlock, write_json
from agents_router.runner import Runner
from agents_router.runtimes.claude import ClaudeRuntime


HEALTH_FIELDS = {"unhealthy_until", "unhealthy_reason", "recovering", "unhealthy_models"}
QUOTA_FIELDS = {"runtime", "account", "known", "left_percent", "window", "resets_at", "observed_at", "cooling_until", "text"}


class HealthReportTests(TempHome):
    def setUp(self):
        super().setUp()
        self.write_config(accounts=("a", "b"))
        self.now = time.mktime((2026, 11, 8, 14, 1, 0, 0, 0, -1))
        self.config = load_config()
        self.runtime = self.config.runtime("claude")
        for account in self.runtime.pool_accounts():
            account.home.mkdir(parents=True, exist_ok=True)
            (account.home / "logged_in").touch()
        # Every runtime that a report could inspect has a fake binary, even on a developer's machine.
        for runtime, fake in (("CODEX", "codex"), ("AGY", "agy"), ("KIRO", "kiro")):
            os.environ[f"AGT_{runtime}_COMMAND"] = json.dumps([
                sys.executable, str(Path(__file__).with_name(f"fake_{fake}.py"))])

    def seed(self, name="a", *, scope="account", until=None, reason="worker could not start", runtime=None, now=None):
        runtime = runtime or self.runtime
        now = self.now if now is None else now
        until = now + 240 if until is None else until
        account = runtime.find(name)
        path = self.home / "state" / f"{runtime.runtime}-health.json"
        data = read_json(path, {}) or {}
        record = data.setdefault(accounts._home_key(account.home), {"account": name, "generation": 0, "circuits": {}})
        record["circuits"][scope] = {
            "failures": 2, "first_failure": min(now - 1, until - 61), "opened_at": min(now, until - 60),
            "open_until": until, "open_seconds": 60, "reason": reason, "claim": None,
        }
        write_json(path, data)

    def report(self):
        with patch("time.time", return_value=self.now):
            return tools.account_report("claude")

    def run_cli(self, *argv):
        stream = io.StringIO()
        with patch("time.time", return_value=self.now), contextlib.redirect_stdout(stream):
            result = cli.main(list(argv))
        return result, stream.getvalue()

    def test_account_details_each_circuit_and_exact_json_fields(self):
        self.seed()
        self.seed(scope="model:gpt-6.1-sol", until=self.now + 60, reason="model at capacity")
        entries = {entry["name"]: entry for entry in self.report()["accounts"]}
        entry = entries["a"]
        self.assertEqual({key: entry[key] for key in HEALTH_FIELDS}, {
            "unhealthy_until": "2026-11-08 14:05", "unhealthy_reason": "worker could not start", "recovering": False,
            "unhealthy_models": [{"model": "gpt-6.1-sol", "unhealthy_until": "2026-11-08 14:02",
                                  "unhealthy_reason": "model at capacity", "recovering": False}],
        })
        self.assertFalse(HEALTH_FIELDS & entries["b"].keys())
        self.assertEqual(health_view.account_detail(entry),
                         "a unhealthy until 14:05 · last error: worker could not start; "
                         "a/gpt-6.1-sol unhealthy until 14:02 · last error: model at capacity")
        detail = ui.plain(cli._account_rows(entry, method=True)[-1])
        self.assertIn(health_view.account_detail(entry), detail)
        result, output = self.run_cli("accounts", "--runtime", "claude")
        self.assertEqual(result, 0)
        self.assertIn(health_view.account_detail(entry), output)

    def test_model_only_health_does_not_create_account_scope_fields(self):
        self.seed(scope="model:gpt-6.1-sol", until=self.now + 60, reason="model at capacity")
        entry = self.report()["accounts"][0]
        self.assertEqual(HEALTH_FIELDS & entry.keys(), {"unhealthy_models"})
        self.assertEqual(health_view.account_detail(entry),
                         "a/gpt-6.1-sol unhealthy until 14:02 · last error: model at capacity")

    def test_approved_surface_examples_keep_the_exact_names_and_copy(self):
        self.write_config(accounts=("claude-work", "work"))
        self.config = load_config()
        self.runtime = self.config.runtime("claude")
        self.seed("claude-work")
        self.seed("work", scope="model:gpt-6.1-sol", until=self.now + 60, reason="model at capacity")
        entries = {entry["name"]: entry for entry in self.report()["accounts"]}
        self.assertEqual(health_view.account_detail(entries["claude-work"]),
                         "claude-work unhealthy until 14:05 · last error: worker could not start")
        self.assertEqual(health_view.account_detail(entries["work"]),
                         "work/gpt-6.1-sol unhealthy until 14:02 · last error: model at capacity")
        self.assertEqual(health_view.summary(self.runtime, [self.runtime.find("claude-work")], self.now),
                         "claude-work unhealthy until 14:05")
        self.assertEqual(health_view.summary(self.runtime, [self.runtime.find("work")], self.now),
                         "work/gpt-6.1-sol unhealthy until 14:02")
        self.seed("claude-work", until=self.now)
        entry = self.report()["accounts"][0]
        self.assertEqual(health_view.account_detail(entry), "claude-work recovering · last error: worker could not start")
        self.assertEqual(health_view.summary(self.runtime, [self.runtime.find("claude-work")], self.now), "claude-work recovering")

    def test_recovering_copy_and_fields(self):
        self.seed(until=self.now)
        self.seed(scope="model:gpt-6.1-sol", until=self.now - 60, reason="model at capacity")
        entry = self.report()["accounts"][0]
        self.assertTrue(entry["recovering"])
        self.assertTrue(entry["unhealthy_models"][0]["recovering"])
        self.assertEqual(health_view.account_detail(entry),
                         "a recovering · last error: worker could not start; "
                         "a/gpt-6.1-sol recovering · last error: model at capacity")

    def test_healthy_failure_streak_and_disabled_breaker_omit_fields(self):
        health.record_outcome(self.runtime, self.runtime.find("a"), outcome="error", generation=0,
                              health_failure="launch", now=self.now)
        self.assertFalse(HEALTH_FIELDS & self.report()["accounts"][0].keys())
        self.seed()
        self.runtime.circuit_breaker = False
        self.assertEqual(health_view.account_health(self.runtime, self.runtime.find("a"), self.now), {})

    def test_reports_fail_open_when_health_cannot_be_read(self):
        with patch.object(health, "read_record", side_effect=OSError("unreadable")):
            self.assertFalse(HEALTH_FIELDS & self.report()["accounts"][0].keys())

    def test_cli_json_and_subagent_accounts_share_health_fields(self):
        self.seed()
        result, output = self.run_cli("accounts", "--runtime", "claude", "--json")
        self.assertEqual(result, 0)
        json_entries = json.loads(output)["accounts"]
        with patch("time.time", return_value=self.now):
            mcp = tools.call("subagent_accounts", {"runtime": "claude"})
        self.assertEqual(mcp["runtimes"][0]["accounts"], json_entries)
        self.assertEqual(json_entries[0]["unhealthy_reason"], "worker could not start")
        self.assertFalse(HEALTH_FIELDS & json_entries[1].keys())

    def test_room_summarizes_soonest_circuit_with_more_count(self):
        self.seed()
        self.seed(scope="model:gpt-6.1-sol", until=self.now + 60, reason="model at capacity")
        self.seed("b", until=self.now + 180)
        self.assertEqual(usage_view.room_line(self.config, ["claude"], self.now),
                         "Room: Claude a/gpt-6.1-sol unhealthy until 14:02 (+2 more)")

    def test_room_single_account_and_recovering_copy(self):
        self.seed()
        self.assertEqual(usage_view.room_line(self.config, ["claude"], self.now),
                         "Room: Claude a unhealthy until 14:05")
        self.seed(until=self.now)
        self.assertEqual(usage_view.room_line(self.config, ["claude"], self.now), "Room: Claude a recovering")

    def test_room_soonest_deadline_uses_seconds_before_formatting_minutes(self):
        self.seed("a", until=self.now + 45)
        self.seed("b", until=self.now + 1)
        self.assertEqual(usage_view.room_line(self.config, ["claude"], self.now),
                         "Room: Claude b unhealthy until 14:01 (+1 more)")

    def test_quota_json_contract_is_unchanged_by_health(self):
        account = self.runtime.find("a")
        record = {"windows": {"five_hour": {"utilization": 0.4, "resets_at": self.now + 3600}}}
        before = usage_view.account_quota("claude", account, record, self.now)
        self.seed()
        after = usage_view.account_quota("claude", account, record, self.now)
        self.assertEqual(set(after), QUOTA_FIELDS)
        self.assertEqual(before, after)
        self.assertFalse(HEALTH_FIELDS & after.keys())

    def test_CB_S7_reset_all_four_exact_messages_and_clears_models(self):
        """CB-S7: health and cooldown reset independently, together, or have nothing to clear."""

        with (self.home / "settings.toml").open("a", encoding="utf-8") as stream:
            stream.write(f'\n[codex]\nprimary = {json.dumps(str(self.root / "codex"))}\n'
                         f'[[codex.accounts]]\nname = "work"\nhome = {json.dumps(str(self.root / "codex-work"))}\n')
        runtime = load_config().runtime("codex")
        for cooldown, unhealthy, message in ((False, True, "health cleared"), (True, False, "cooldown cleared"),
                                             (True, True, "cooldown and health cleared"), (False, False, "nothing to clear")):
            with self.subTest(message=message):
                if cooldown:
                    accounts.set_cooldown("codex", "work", self.now + 3600, "limit")
                if unhealthy:
                    self.seed("work", runtime=runtime)
                    self.seed("work", runtime=runtime, scope="model:gpt-6.1-sol", reason="model at capacity")
                result, output = self.run_cli("accounts", "reset", "work", "--runtime", "codex")
                detail = (message + "; ") if message != "nothing to clear" else ""
                self.assertEqual((result, output.strip()), (0, f"codex account 'work': {detail}usage check failed"))
                self.assertEqual(health_view.account_health(runtime, runtime.find("work"), self.now), {})
                self.assertIsNone(accounts.cooldown_until(accounts.load_usage("codex").get("work"), self.now))

    def test_CB_S7_reset_recovering_claim_and_unknown_account(self):
        """CB-S7: reset also frees an unadopted trial claim, keeping the existing unknown-name refusal."""

        self.seed(until=self.now)
        health.claim_trial(self.runtime, self.runtime.find("a"), None, "J", now=self.now)
        result, output = self.run_cli("accounts", "reset", "a")
        self.assertEqual((result, output.strip()), (0, "claude account 'a': health cleared"))
        self.assertEqual(health.read_record(self.runtime, self.runtime.find("a"), now=self.now)["circuits"], {})
        result, output = self.run_cli("accounts", "reset", "missing")
        self.assertEqual(result, 1)
        self.assertIn("No claude account named 'missing'.", output)

    def test_CB_S7_launch_all_unhealthy_exact_message(self):
        """CB-S7: launch reports the soonest rest end without promising a retry."""

        self.seed(until=self.now + 300)
        self.seed("b")
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1, "Every claude account is unhealthy; available again around 14:05."))

    def test_CB_S7_launch_all_recovering_busy_exact_message(self):
        """CB-S7: launch reports running trials without inventing a rest end."""

        for name in ("a", "b"):
            self.seed(name, until=self.now)
            lease = health.acquire_trial(self.runtime, self.runtime.find(name), None, f"J-{name}", now=self.now)
            self.assertIsNotNone(lease)
            self.addCleanup(lease.release)
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1,
                         "Every claude account is recovering and its trial job is running; try again when one finishes."))

    def test_CB_S7_launch_unhealthy_and_recovering_busy_exact_message(self):
        """CB-S7: resting accounts and running trials have distinct health copy."""

        self.seed()
        self.seed("b", until=self.now)
        lease = health.acquire_trial(self.runtime, self.runtime.find("b"), None, "J-b", now=self.now)
        self.assertIsNotNone(lease)
        self.addCleanup(lease.release)
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1,
                         "Every claude account is unavailable: some unhealthy (available again around 14:05), "
                         "some recovering with a trial running."))

    def test_CB_S7_launch_mixed_exact_message(self):
        """CB-S7: unhealthy accounts mixed with usage limits keep both causes."""

        self.seed()
        accounts.set_cooldown("claude", "b", self.now + 3600, "limit")
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1,
                         "Every claude account is unavailable: some unhealthy (available again around 14:05), some at a usage limit."))

    def test_CB_S7_launch_recovering_busy_and_usage_limit_exact_message(self):
        """CB-S7: running trials mixed with usage limits have no time clause."""

        self.seed(until=self.now)
        lease = health.acquire_trial(self.runtime, self.runtime.find("a"), None, "J-a", now=self.now)
        self.assertIsNotNone(lease)
        self.addCleanup(lease.release)
        accounts.set_cooldown("claude", "b", self.now + 3600, "limit")
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1,
                         "Every claude account is unavailable: some recovering with a trial running, some at a usage limit."))

    def test_CB_S7_launch_usage_only_unchanged_message(self):
        """CB-S7: usage-limit-only launch failures keep their existing message."""

        for name in ("a", "b"):
            accounts.set_cooldown("claude", name, self.now + 3600, "limit")
        result, output = self.run_cli("launch", "--runtime", "claude")
        self.assertEqual((result, output.strip()), (1, "Every claude account is cooling down after a usage limit."))

    def usage(self, name, used):
        accounts.update_usage("claude", name, windows={"five_hour": {"utilization": used, "resets_at": self.now + 3600}})

    def test_room_cap_uses_shared_health_model_and_trial_eligibility(self):
        self.runtime.max_jobs_by_room = [[50, 4], [0, 1]]
        self.usage("a", 0.1)
        self.usage("b", 0.8)
        self.seed(scope="model:m", until=self.now + 60, reason="model at capacity")
        self.assertEqual(quota.room_cap(self.runtime, self.now, "m"), (1, 20))
        self.assertEqual(quota.room_cap(self.runtime, self.now, "other"), (4, 90))
        self.runtime.default_model = "m"
        self.assertEqual(quota.room_cap(self.runtime, self.now), (1, 20))
        self.seed(scope="model:m", until=self.now, reason="model at capacity")
        self.assertEqual(quota.room_cap(self.runtime, self.now), (4, 90))
        health.claim_trial(self.runtime, self.runtime.find("a"), "m", "J", now=self.now)
        self.assertEqual(quota.room_cap(self.runtime, self.now), (1, 20))

    def test_room_cap_uses_merged_login_usage_and_cooldowns(self):
        self.runtime.max_jobs_by_room = [[50, 4], [0, 1]]
        for account in self.runtime.accounts:
            account.login = "shared"
        self.usage("a", 0.1)
        self.usage("b", 0.8)
        with patch("time.time", return_value=self.now):
            self.assertEqual(quota.room_cap(self.runtime, self.now), (1, 20))
            accounts.set_cooldown("claude", "b", self.now + 3600, "limit")
            self.assertEqual(quota.room_cap(self.runtime, self.now), (4, None))

    def test_room_cap_without_another_profile_keeps_unknown_usage_cap(self):
        self.runtime.max_jobs_by_room = [[50, 4], [0, 1]]
        self.runtime.accounts = []
        self.runtime.session_home = self.primary
        self.assertEqual(self.runtime.worker_accounts(), [])
        self.assertEqual(quota.room_cap(self.runtime, self.now), (4, None))

    def test_CB_S13_live_doctor_runs_unhealthy_fake_account_and_heals_both_scopes(self):
        """CB-S13: the explicit live check skips rest and records an actual fake-worker success."""

        self.write_config(accounts=("a",))
        self.runtime = load_config().runtime("claude")
        now = time.time()
        self.seed(now=now, until=now + 600)
        self.seed(scope="model:haiku", now=now, until=now + 600, reason="model at capacity")
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a"), "haiku").state, "unhealthy")
        output = io.StringIO()
        with patch.object(cli, "_share_doctor", return_value=[]), \
                patch.object(ClaudeRuntime, "version", return_value="fake-version"), contextlib.redirect_stdout(output):
            result = cli.main(["doctor", "--live", "--runtime", "claude", "--cwd", str(self.project)])
        self.assertEqual(result, 0, output.getvalue())
        self.assertIn("[a] success:", output.getvalue())
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["env"]["CLAUDE_CONFIG_DIR"], str(self.runtime.find("a").home))
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a"), "haiku").state, "healthy")
        spec = jobs.read_spec(next((self.home / "jobs").iterdir()).name)
        self.assertTrue(spec._doctor_live)
        self.assertEqual((spec.account, spec.access), ("a", "read"))

    def test_CB_S13_live_doctor_checks_recovering_account_and_records_failure(self):
        """CB-S13: a failed explicit check records its real outcome and reopens the recovering circuit."""

        self.write_config(accounts=("a",))
        self.runtime = load_config().runtime("claude")
        now = time.time()
        self.seed(now=now, until=now - 1)
        output = io.StringIO()
        with patch.object(cli, "_share_doctor", return_value=[]), \
                patch.object(ClaudeRuntime, "version", return_value="fake-version"), \
                patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "no_result"}), contextlib.redirect_stdout(output):
            result = cli.main(["doctor", "--live", "--runtime", "claude", "--cwd", str(self.project)])
        self.assertEqual(result, 1, output.getvalue())
        self.assertIn("[a] error:", output.getvalue())
        self.assertEqual(len(self.records()), 1)
        circuit = health.read_record(self.runtime, self.runtime.find("a"))["circuits"]["account"]
        self.assertEqual(circuit["open_seconds"], 120)
        self.assertEqual(circuit["reason"], "worker ended without a result")
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a")).state, "unhealthy")

    def test_CB_S13_doctor_rest_bypass_keeps_quota_model_and_access_checks(self):
        """CB-S13: skipping the rest cannot bypass quota cooldowns, model restrictions or access validation."""

        self.seed()
        accounts.set_cooldown("claude", "a", self.now + 3600, "limit")
        with self.assertRaisesRegex(limits.LimitError, "at its usage limit"):
            limits.admit(self.config, "claude", account="a", model="haiku", now=self.now, _doctor_live=True)
        accounts.clear_cooldown("claude", "a")
        self.runtime.find("a").models = ["other"]
        with self.assertRaisesRegex(limits.LimitError, "No claude account allows model 'haiku'"):
            limits.admit(self.config, "claude", account="a", model="haiku", now=self.now, _doctor_live=True)
        with self.assertRaisesRegex(ValueError, "access must be"):
            jobs.create_job(runtime="claude", prompt="check", cwd=str(self.project), account="a",
                            access="invalid", _doctor_live=True)

    def test_CB_S13_doctor_keeps_claim_and_lease_exclusive_and_stays_pinned(self):
        """CB-S13: doctor queues behind another trial and then leases the requested account only."""

        now = time.time()
        self.seed(now=now, until=now + 600)
        self.seed(scope="model:haiku", now=now, until=now + 600, reason="model at capacity")
        account = self.runtime.find("a")
        claimed = health.claim_trial(self.runtime, account, "haiku", "competitor", skip_rest=True)
        self.assertEqual(claimed.trials, ("account", "model:haiku"))
        with patch.object(jobs, "spawn_detached", return_value=99999999):
            status = jobs.create_job(runtime="claude", prompt="check", cwd=str(self.project), account="a", model="haiku",
                                     access="read", _doctor_live=True)
        self.addCleanup(jobs.update_status, status["job_id"], state="done", outcome="cancelled")
        self.assertEqual(status["message"], "queued · a recovering (trial job running)")
        runner = Runner(status["job_id"])
        with patch.object(runner, "cancelled", return_value=True):
            self.assertIsNone(runner._wait_for_account(set()))
        health.release_claim(self.runtime, account, "competitor", generation=claimed.generation)
        lease = runner._wait_for_account(set())
        self.assertIsNotNone(lease)
        self.addCleanup(lease.release)
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(lease.trial.scopes, ("account", "model:haiku"))
        other, _until, busy = accounts.acquire(self.runtime, pinned="a", model="haiku", job_id="other", skip_rest=True)
        self.assertIsNone(other)
        self.assertTrue(busy)
        self.assertEqual(health.query_state(self.runtime, account, "haiku").state, "unhealthy")

    def test_CB_S13_doctor_retains_usage_scaled_caps(self):
        """CB-S13: a check's rest bypass still uses the eligible accounts' real quota for room caps."""

        self.runtime.max_jobs_by_room = [[50, 4], [0, 1]]
        self.seed()
        self.seed("b")
        self.usage("a", 0.8)
        self.usage("b", 0.8)
        with patch("time.time", return_value=self.now):
            self.assertEqual(quota.room_cap(self.runtime, self.now, _doctor_live=True), (1, 20))
            counts = limits.Counts(by_runtime={"claude": 1})
            with patch.object(limits, "count_active", return_value=counts), \
                    self.assertRaisesRegex(limits.LimitError, "max_jobs_by_room"):
                limits.admit(self.config, "claude", account="a", model="haiku", _doctor_live=True)
            health.claim_trial(self.runtime, self.runtime.find("a"), "haiku", "doctor", skip_rest=True)
            health.claim_trial(self.runtime, self.runtime.find("b"), "haiku", "competitor", skip_rest=True)
            slots = limits.acquire_execution_slots(self.config, "claude", "haiku", _doctor_live=True, job_id="doctor")
            self.assertIsNotNone(slots)
            for handle, _path in slots:
                self.addCleanup(unlock, handle)
            self.assertIsNone(limits.acquire_execution_slots(self.config, "claude", "haiku", _doctor_live=True, job_id="doctor"))


if __name__ == "__main__":
    unittest.main()
