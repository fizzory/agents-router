"""CB-S5/CB-S6: the account funnel, trial admission and queueing in isolated homes."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Timer
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, health, jobs, limits, tools
from agents_router.config import Account, QuotaPool, RuntimeConfig, load_config, save_config
from agents_router.fsutil import process_start_time, read_json, try_lock, unlock
from agents_router.runner import Runner


class HealthRoutingTests(TempHome):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.config = load_config()
        self.runtime = RuntimeConfig("codex", self.root / "codex",
                                     accounts=[Account(name, self.root / name) for name in ("a", "b")])
        for account in self.runtime.accounts:
            account.home.mkdir()
        self.config.runtimes["codex"] = self.runtime
        os.environ["AGT_CODEX_COMMAND"] = json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))])
        self.spawn = self.enterContext(patch.object(jobs, "spawn_detached", return_value=99999999))
        self.available = self.enterContext(patch.object(limits, "_available_runtime", return_value=False))
        self.save()

    def save(self):
        save_config(self.config)

    def trip(self, name="a", *, kind="transport", model="model-x", recovering=False):
        now = self.now - (120 if recovering else 2)
        account = self.runtime.find(name)
        generation = health.query_state(self.runtime, account, model).generation
        for offset in (0, 1):
            health.record_outcome(self.runtime, account, model, outcome="error", generation=generation,
                                  health_failure=kind, now=now + offset)

    def acquire(self, **kwargs):
        result = accounts.acquire(self.runtime, **kwargs)
        if result[0] is not None:
            lease = result[0]
            self.addCleanup(lambda: lease.release() if not lease.handle.closed else None)
        return result

    def create(self, **kwargs):
        status = jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), **kwargs)
        self.addCleanup(jobs.update_status, status["job_id"], state="done", outcome="cancelled")
        return status

    def test_CB_S6_unhealthy_account_routes_the_next_unpinned_job_to_b(self):
        """CB-S6: health is folder-local, separate from quota cooldowns."""
        self.trip()
        ranking = accounts.ranked_accounts(self.runtime, model="model-x")
        self.assertEqual([a.name for a in ranking.accounts], ["b"])
        self.assertEqual([(b.account, b.kind, b.scope, b.state) for b in ranking.blockers],
                         [("a", "health", "account", "unhealthy")])
        self.assertEqual(ranking.blockers[0].reason, "connection failed")
        self.assertIsNotNone(ranking.blockers[0].until)
        self.assertIsNone(ranking.cooldown_end)
        lease, _, busy = self.acquire(model="model-x")
        self.assertEqual(lease.account.name, "b")
        self.assertFalse(busy)
        self.assertNotIn("cooldown_until", accounts.load_usage("codex").get("a", {}))

    def test_model_capacity_does_not_block_another_model(self):
        self.trip(kind="capacity")
        lease, _, _ = self.acquire(model="model-y")
        self.assertEqual(lease.account.name, "a")
        other, _, _ = self.acquire(model="model-x")
        self.assertEqual(other.account.name, "b")

    def test_an_allowed_account_is_not_hidden_by_another_accounts_model_blocker(self):
        self.runtime.find("b").models = ["model-y"]
        current = limits.room(self.config, "codex", limits.Counts(), pinned=None, model="model-x", now=time.time())
        self.assertEqual([a.name for a, _ in current.accounts], ["a"])
        self.assertFalse(current.model_blocked)
        self.assertEqual(limits.admit(self.config, "codex", model="model-x").account, "a")

    def test_a_model_only_trial_takes_only_the_model_lease(self):
        self.trip(kind="capacity", recovering=True)
        lease, _, _ = self.acquire(model="model-x", job_id="model-trial")
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(lease.trial.scopes, ("model:model-x",))
        other, _, _ = self.acquire(pinned="a", model="model-y", job_id="other-model")
        self.assertEqual(other.account.name, "a")
        self.assertEqual(other.trial.scopes, ())

    def test_CB_S5_pinned_unhealthy_is_refused_even_with_override_or_queue(self):
        """CB-S5: a pin and override never bypass the rest."""
        self.trip()
        until = time.strftime("%H:%M", time.localtime(self.now + 59))
        expected = (f"a is unhealthy until {until} · last error: connection failed. "
                    "Omit account to let agt pick another, or wait for it to recover.")
        for queue in ("refuse", "queue"):
            for override in (None, "run more jobs"):
                with self.subTest(queue=queue, override=override):
                    self.runtime.when_full = queue
                    self.save()
                    with self.assertRaises(limits.LimitError) as raised:
                        self.create(account="a", override=override)
                    self.assertEqual(str(raised.exception).splitlines()[0], expected)
        self.spawn.assert_not_called()
        self.assertFalse(jobs.jobs_dir().exists())
        lease, _, busy = self.acquire(pinned="a", override="run more jobs")
        self.assertIsNone(lease)
        self.assertFalse(busy)

    def test_CB_S5_pinned_recovering_claims_and_adopts_its_trial(self):
        """CB-S5: pinned accounts can recover through real jobs."""
        self.trip(recovering=True)
        status = self.create(account="a", model="model-x")
        circuit = health.read_record(self.runtime, self.runtime.find("a"))["circuits"]["account"]
        self.assertEqual(circuit["claim"]["job"], status["job_id"])
        self.assertEqual(circuit["claim"]["generation"], 0)
        self.assertAlmostEqual(circuit["claim"]["deadline"], time.time() + 120, delta=2)
        lease, _, _ = self.acquire(pinned="a", model="model-x", job_id=status["job_id"])
        self.assertEqual(lease.trial.scopes, ("account",))
        self.assertIsNone(health.read_record(self.runtime, self.runtime.find("a"))["circuits"]["account"]["claim"])

    def test_pinned_recovering_claimed_or_leased_trial_queues(self):
        self.trip(recovering=True)
        first = self.create(account="a")
        second = self.create(account="a")
        expected = "queued · a recovering (trial job running)"
        self.assertEqual(second["message"], expected)
        self.assertEqual(jobs.read_status(second["job_id"])["message"], expected)
        self.assertEqual(second["state"], "queued")
        self.assertIsNone(second["assigned_account"])
        lease, _, _ = self.acquire(pinned="a", job_id=first["job_id"])
        self.assertIsNotNone(lease.trial)
        third = self.create(account="a", override="run more jobs")
        self.assertEqual(third["message"], expected)

    def test_claims_are_taken_under_the_admission_lock(self):
        self.trip(recovering=True)
        original = health.claim_trial

        def check_lock(*args, **kwargs):
            handle = try_lock(self.home / "state" / "admission.lock")
            if handle is not None:
                unlock(handle)
            self.assertIsNone(handle)
            return original(*args, **kwargs)

        with patch.object(health, "claim_trial", side_effect=check_lock) as claim:
            self.create(account="a")
        self.assertEqual(claim.call_count, 1)

    def test_back_to_back_unpinned_creations_do_not_share_the_trial(self):
        self.trip(recovering=True)
        first, second = self.create(), self.create()
        self.assertEqual((first["assigned_account"], second["assigned_account"]), ("a", "b"))

    def test_concurrent_creations_claim_only_one_trial(self):
        self.trip(recovering=True)
        barrier = Barrier(2)

        def create():
            barrier.wait(timeout=5)
            return self.create()

        with ThreadPoolExecutor(max_workers=2) as workers:
            result = list(workers.map(lambda _: create(), range(2)))
        self.assertEqual({s["assigned_account"] for s in result}, {"a", "b"})
        claim = health.read_record(self.runtime, self.runtime.find("a"))["circuits"]["account"]["claim"]
        self.assertEqual(claim["job"], next(s["job_id"] for s in result if s["assigned_account"] == "a"))

    def test_failed_creation_releases_the_claim(self):
        self.trip(recovering=True)
        original = jobs.write_json

        def fail_spec(path, value):
            if path.name == "spec.json":
                raise OSError("disk full")
            return original(path, value)

        with patch.object(jobs, "write_json", side_effect=fail_spec):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.create(account="a")
        self.assertFalse(health.query_state(self.runtime, self.runtime.find("a")).blocked)

    def test_runner_spawn_failure_releases_the_claim(self):
        self.trip(recovering=True)
        self.spawn.side_effect = OSError("runner unavailable")
        status = self.create(account="a")
        self.assertEqual(status["outcome"], "error")
        self.assertFalse(health.query_state(self.runtime, self.runtime.find("a")).blocked)

    def test_admission_claim_race_reranks_before_writing_the_job(self):
        self.trip(recovering=True)
        original = health.claim_trial
        calls = 0

        def race(runtime, account, model, job_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                original(runtime, account, model, "other-job", **kwargs)
            return original(runtime, account, model, job_id, **kwargs)

        with patch.object(health, "claim_trial", side_effect=race):
            status = self.create()
        self.assertEqual(status["assigned_account"], "b")
        self.assertEqual(calls, 2)

    def test_recovering_first_acquire_has_the_lease_second_skips_it(self):
        self.trip(recovering=True)
        first, _, _ = self.acquire(job_id="first")
        second, _, _ = self.acquire(job_id="second")
        self.assertEqual((first.account.name, second.account.name), ("a", "b"))
        self.assertEqual(first.trial.scopes, ("account",))
        self.assertEqual(second.trial.scopes, ())
        first.release()
        third, _, _ = self.acquire(job_id="third")
        self.assertEqual(third.account.name, "a")

    def test_concurrent_acquires_cannot_share_a_recovering_account(self):
        self.trip(recovering=True)
        barrier = Barrier(2)
        original = accounts.ranked_accounts

        def same_ranking(*args, **kwargs):
            ranking = original(*args, **kwargs)
            # Both initial snapshots see the free trial; reranks must not wait.
            if ranking.accounts and ranking.accounts[0].name == "a":
                barrier.wait(timeout=5)
            return ranking

        with patch.object(accounts, "ranked_accounts", side_effect=same_ranking):
            with ThreadPoolExecutor(max_workers=2) as workers:
                leases = list(workers.map(lambda n: self.acquire(job_id=str(n))[0], range(2)))
        self.assertEqual({lease.account.name for lease in leases}, {"a", "b"})
        self.assertEqual(sum(bool(lease.trial.scopes) for lease in leases), 1)

    def test_stale_recovering_ranking_reopened_before_lease_reranks(self):
        self.trip(recovering=True)
        original = accounts.ranked_accounts
        calls = 0

        def reopen(*args, **kwargs):
            nonlocal calls
            ranking = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                trial = health.acquire_trial(self.runtime, self.runtime.find("a"), "model-x", "earlier")
                try:
                    health.record_outcome(self.runtime, self.runtime.find("a"), "model-x", outcome="error",
                                          health_failure="transport", generation=trial.generation, trials=trial)
                finally:
                    trial.release()
            return ranking

        with patch.object(accounts, "ranked_accounts", side_effect=reopen):
            lease, _, _ = self.acquire(model="model-x", job_id="later")
        self.assertEqual(lease.account.name, "b")
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(accounts.busy_slots(self.runtime, self.runtime.find("a")), 0)

    def test_reset_between_ranking_and_acquisition_uses_the_new_generation(self):
        self.trip(recovering=True)
        original = accounts.ranked_accounts
        calls = 0

        def reset(*args, **kwargs):
            nonlocal calls
            ranking = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                health.reset(self.runtime, self.runtime.find("a"))
            return ranking

        with patch.object(accounts, "ranked_accounts", side_effect=reset):
            lease, _, _ = self.acquire(job_id="new-generation")
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(lease.trial.generation, 1)
        self.assertEqual(calls, 2)

    def test_two_recovering_circuits_claim_and_lease_both(self):
        self.trip(recovering=True)
        self.trip(kind="capacity", recovering=True)
        status = self.create(account="a", model="model-x")
        record = health.read_record(self.runtime, self.runtime.find("a"))
        self.assertTrue(all(c["claim"]["job"] == status["job_id"] for c in record["circuits"].values()))
        lease, _, _ = self.acquire(pinned="a", model="model-x", job_id=status["job_id"])
        self.assertEqual(lease.trial.scopes, ("account", "model:model-x"))

    def test_two_recovering_circuits_never_leave_a_partial_lease(self):
        self.trip(recovering=True)
        self.trip(kind="capacity", recovering=True)
        account = self.runtime.find("a")
        model_path = health._lease_path(self.runtime, account, "model:model-x", 0)
        original = accounts._take_slot
        with patch.object(accounts, "_take_slot", side_effect=lambda path: None if path == model_path else original(path)):
            lease, _, busy = self.acquire(pinned="a", model="model-x", job_id="partial")
        self.assertIsNone(lease)
        self.assertTrue(busy)
        self.assertEqual(accounts.busy_slots(self.runtime, account), 0)
        self.assertFalse(health.query_state(self.runtime, account, "model-x").blocked)
        lease, _, _ = self.acquire(pinned="a", model="model-x", job_id="retry")
        self.assertEqual(lease.trial.scopes, ("account", "model:model-x"))

    def test_only_account_trial_held_counts_as_busy_and_runner_waits(self):
        """CB-S6 / row 20: waiting behind a trial is not no_account."""
        self.runtime.accounts = self.runtime.accounts[:1]
        self.save()
        self.trip(recovering=True)
        held, _, _ = self.acquire(job_id="held")
        lease, until, busy = self.acquire(job_id="waiting")
        self.assertIsNone(lease)
        self.assertIsNone(until)
        self.assertTrue(busy)
        status = self.create(start=False)
        runner = Runner(status["job_id"])
        with patch("agents_router.runner.time.sleep", side_effect=lambda _: held.release()) as sleep:
            lease = runner._wait_for_account(set())
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(lease.trial.scopes, ("account",))
        lease.release()

    def test_health_and_quota_and_model_blockers_are_separate(self):
        self.trip()
        accounts.set_cooldown("codex", "b", self.now + 600, "usage limit")
        ranking = accounts.ranked_accounts(self.runtime, model="model-x")
        self.assertEqual({b.kind for b in ranking.blockers}, {"health", "quota"})
        self.assertEqual(ranking.cooldown_end, self.now + 600)
        with self.assertRaisesRegex(limits.LimitError, "usage limit until"):
            limits.admit(self.config, "codex", model="model-x")
        self.runtime.find("b").models = ["model-y"]
        ranking = accounts.ranked_accounts(self.runtime, model="model-x")
        self.assertEqual({b.kind for b in ranking.blockers}, {"health", "model"})

    def test_every_account_excludes_model_keeps_the_existing_model_refusal(self):
        for account in self.runtime.accounts:
            account.models = ["model-y"]
        self.trip()
        current = limits.room(self.config, "codex", limits.Counts(), pinned=None, model="model-x", now=time.time())
        self.assertTrue(current.model_blocked)
        self.assertEqual([b.kind for b in current.blockers], ["model", "model"])
        with self.assertRaisesRegex(limits.LimitError,
                                   "No codex account allows model 'model-x' \\(allowed: model-y; from models\\)"):
            limits.admit(self.config, "codex", model="model-x")

    def test_room_uses_the_requested_or_default_model_and_its_pool(self):
        self.runtime.default_model = "model-x"
        self.runtime.pools = [QuotaPool("x", ["model-x"])]
        self.trip(kind="capacity")
        current = limits.room(self.config, "codex", limits.Counts(), pinned=None, model=None, now=time.time())
        self.assertEqual([a.name for a, _ in current.accounts], ["b"])
        accounts.set_pool_cooldown("codex", "b", "x", self.now + 600, "pool limit")
        current = limits.room(self.config, "codex", limits.Counts(), pinned=None, model=None, now=time.time())
        self.assertEqual(current.accounts, [])
        self.assertEqual(current.cooldown_end, self.now + 600)
        self.assertEqual({b.kind for b in current.blockers}, {"health", "quota"})
        current = limits.room(self.config, "codex", limits.Counts(), pinned=None, model="model-y", now=time.time())
        self.assertEqual([a.name for a, _ in current.accounts], ["a", "b"])

    def test_alternatives_do_not_suggest_an_unhealthy_runtime(self):
        self.trip()
        self.trip("b")
        with patch.object(limits, "_available_runtime", side_effect=lambda name: name == "codex"):
            self.assertEqual(limits._alternatives(self.config, "claude", limits.Counts(), time.time()), [])
            health.reset(self.runtime, self.runtime.find("b"))
            self.assertEqual(limits._alternatives(self.config, "claude", limits.Counts(), time.time()), ["codex"])

    def test_CB_S6_queue_mode_all_unhealthy_ends_no_account_without_waiting(self):
        """CB-S6: queueing cannot send a job to an unhealthy account."""
        self.runtime.when_full = "queue"
        self.save()
        self.trip()
        self.trip("b")
        status = self.create()
        with patch("agents_router.runner.time.sleep", side_effect=AssertionError("unhealthy job queued")):
            self.assertEqual(Runner(status["job_id"]).run(), 1)
        self.assertEqual(jobs.read_status(status["job_id"])["outcome"], "no_account")
        lease, until, busy = self.acquire(override="run more jobs")
        self.assertEqual((lease, until, busy), (None, None, False))

    def test_trial_owner_follows_the_account_lease(self):
        self.trip(recovering=True)
        lease, _, _ = self.acquire(job_id="owner")
        lease.set_owner(os.getpid(), process_start_time(os.getpid()), "owner")
        paths = [path for _, path in lease.trial._slots]
        for path in paths:
            owner = read_json(accounts._owner_path(path))
            self.assertEqual((owner["pid"], owner["job_id"]), (os.getpid(), "owner"))
        lease.release()
        self.assertTrue(lease.trial._released)
        self.assertTrue(all(not accounts._owner_path(path).exists() for path in paths))

    def test_health_read_and_acquire_errors_fail_open(self):
        self.trip()
        with patch.object(health, "query_state", side_effect=OSError("unreadable")):
            with patch.object(health, "acquire_trial", side_effect=AssertionError("untrusted generation used")):
                lease, _, _ = self.acquire()
            status = self.create(account="a")
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(status["assigned_account"], "a")

    def test_health_acquisition_errors_release_no_ordinary_slot(self):
        self.trip(recovering=True)
        with patch.object(health, "acquire_trial", side_effect=OSError("unwritable")):
            lease, _, _ = self.acquire()
        self.assertEqual(lease.account.name, "a")
        self.assertFalse(lease.handle.closed)

    def test_health_claim_errors_fail_open(self):
        with patch.object(health, "claim_trial", side_effect=OSError("unwritable")):
            status = self.create()
        self.assertEqual(status["assigned_account"], "a")

    def test_circuit_breaker_off_does_not_touch_health_storage(self):
        self.trip()
        self.runtime.circuit_breaker = False
        self.save()
        with patch.object(health, "_path", side_effect=AssertionError("health storage accessed")):
            status = self.create(account="a")
            lease, _, _ = self.acquire(pinned="a", job_id=status["job_id"])
        self.assertEqual(lease.account.name, "a")
        self.assertEqual(lease.trial.scopes, ())

    def test_circuit_breaker_off_keeps_existing_cap_check_order(self):
        self.runtime.circuit_breaker = False
        self.config.max_jobs = 1
        with patch.object(limits, "count_active", return_value=limits.Counts(total=1)):
            with self.assertRaisesRegex(limits.LimitError, "agt is at its global limit"):
                limits.admit(self.config, "codex", account="missing")


class HealthRunnerTests(TempHome):
    """CB-S8 / CB-S11: real fake-CLI attempts, recovery and session follow-ups."""

    def setUp(self):
        super().setUp()
        self.config = load_config()
        self.runtime = RuntimeConfig(
            "codex", self.root / "codex", max_jobs_per_account=1,
            accounts=[Account("a", self.root / "a"), Account("b", self.root / "b", fallback=True)],
        )
        for account in self.runtime.accounts:
            account.home.mkdir()
        self.config.runtimes["codex"] = self.runtime
        os.environ.update(
            AGT_CODEX_COMMAND=json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
            FAKE_CODEX_SCENARIO="success", FAKE_CODEX_SCENARIOS="{}", FAKE_CODEX_LIMITED="",
        )
        self.save()

    def save(self):
        save_config(self.config)

    def trip(self, name="a", *, recovering=False, kind="transport", model=None):
        # The runner is a separate process: seed real past times, not a fake clock.
        account = self.runtime.find(name)
        now = time.time() - (120 if recovering else 2)
        generation = health.query_state(self.runtime, account, model).generation
        for offset in (0, 1):
            health.record_outcome(self.runtime, account, model, outcome="error", generation=generation,
                                  health_failure=kind, now=now + offset)

    def create(self, **kwargs):
        return jobs.create_job(runtime="codex", prompt="task", cwd=str(self.project), **kwargs)

    def run_job(self, **kwargs):
        status = self.create(**kwargs)
        status = jobs.wait_for(status["job_id"], 20)
        self.assertEqual(status["state"], "done", status)
        return status

    def runner(self, **kwargs):
        return Runner(self.create(start=False, **kwargs)["job_id"])

    def circuit(self, name="a", scope="account"):
        return health.read_record(self.runtime, self.runtime.find(name))["circuits"].get(scope, {})

    def test_read_no_result_moves_to_b_with_real_cause_and_records_once(self):
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "no_result"})
        status = self.run_job(access="read")
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a", "b"])
        self.assertIn("a is unhealthy (worker ended without a result); continuing on another account", status["warnings"])
        self.assertEqual(self.circuit()["failures"], 1)
        self.assertEqual(self.circuit()["reason"], "worker ended without a result")
        handoff = self.records()[-1]["stdin"]
        self.assertIn("worker ended without a result", handoff)
        self.assertNotIn("usage limit", handoff)
        self.assertNotIn("resume", self.records()[-1]["argv"])

    def test_edit_and_full_mid_run_failures_are_not_moved(self):
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "no_result"})
        for access in ("edit", "full"):
            with self.subTest(access=access):
                health.reset(self.runtime, self.runtime.find("a"))
                status = self.run_job(access=access)
                self.assertEqual(status["outcome"], "error", status)
                self.assertEqual([a["account"] for a in status["attempts"]], ["a"])
                self.assertFalse(any("continuing on another account" in w for w in status["warnings"]))

    def test_no_move_warning_when_no_other_account_can_take_the_job(self):
        self.runtime.accounts = self.runtime.accounts[:1]
        self.save()
        os.environ["FAKE_CODEX_SCENARIO"] = "no_result"
        status = self.run_job(access="read")
        self.assertEqual(status["outcome"], "error", status)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a"])
        self.assertEqual(self.circuit()["failures"], 1)
        self.assertFalse(any("continuing on another account" in w for w in status["warnings"]))

    def test_edit_real_broken_home_launch_moves_and_records_launch(self):
        account = self.runtime.find("a")
        account.home.rmdir()
        account.home.write_text("a regular file, not an account home", encoding="utf-8")
        status = self.run_job(access="edit")
        self.assertEqual(status["outcome"], "success", status)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a", "b"])
        self.assertTrue(status["attempts"][0]["before_start"])
        self.assertEqual((self.circuit()["failures"], self.circuit()["reason"]), (1, "worker could not start"))
        self.assertIn("a is unhealthy (worker could not start); continuing on another account", status["warnings"])

    def test_pinned_and_failover_off_jobs_are_not_moved(self):
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "no_result"})
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                health.reset(self.runtime, self.runtime.find("a"))
                self.runtime.failover = "on-limit" if pinned else "off"
                self.save()
                status = self.run_job(access="read", account="a" if pinned else None)
                self.assertEqual(status["outcome"], "error", status)
                self.assertEqual([a["account"] for a in status["attempts"]], ["a"])
                self.assertFalse(any("continuing on another account" in w for w in status["warnings"]))

    def test_CB_S8_breaker_off_keeps_no_result_error_without_move_warning_or_health_io(self):
        self.trip()
        path = health._path(self.runtime)
        before = path.read_bytes()
        self.runtime.circuit_breaker = False
        self.save()
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "no_result"})
        status = self.run_job(access="read")
        self.assertEqual(status["outcome"], "error", status)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a"])
        self.assertEqual(status["warnings"], [])
        self.assertEqual(path.read_bytes(), before)
        with patch.object(health, "record_outcome", side_effect=AssertionError("health write while off")):
            self.assertEqual(self.runner(access="read").run(), 1)

    def test_trial_success_closes_both_circuits_and_next_job_returns_to_a(self):
        self.trip(recovering=True)
        self.trip(recovering=True, kind="capacity")
        status = self.run_job(access="read")
        self.assertEqual((status["outcome"], status["account"]), ("success", "a"), status)
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a")).state, "healthy")
        for circuit in health.read_record(self.runtime, self.runtime.find("a"))["circuits"].values():
            self.assertEqual((circuit["failures"], circuit["open_seconds"], circuit["claim"]), (0, 60, None))
        next_job = self.run_job(access="read")
        self.assertEqual((next_job["outcome"], next_job["account"]), ("success", "a"))

    def test_trial_health_failure_reopens_both_circuits_for_120_seconds(self):
        self.trip(recovering=True)
        self.trip(recovering=True, kind="capacity")
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "transport"})
        status = self.run_job(access="edit")
        self.assertEqual((status["outcome"], status["account"]), ("error", "a"), status)
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a")).state, "unhealthy")
        for circuit in health.read_record(self.runtime, self.runtime.find("a"))["circuits"].values():
            self.assertEqual(circuit["open_seconds"], 120)
            self.assertEqual(circuit["open_until"] - circuit["opened_at"], 120)
            self.assertEqual(circuit["reason"], "connection failed")
        self.assertEqual(accounts.busy_slots(self.runtime, self.runtime.find("a")), 0)

    def test_cancelled_and_timed_out_trials_release_without_recording(self):
        self.trip(recovering=True)
        os.environ["FAKE_CODEX_SCENARIO"] = "success_pause"
        for outcome in ("cancelled", "timeout"):
            with self.subTest(outcome=outcome):
                runner = self.runner(account="a", access="read")
                before = self.circuit().copy()
                timer = None
                if outcome == "cancelled":
                    timer = Timer(0.2, lambda: (runner.folder / "cancel").touch())
                    timer.start()
                else:
                    runner.spec.timeout_seconds = 1
                try:
                    self.assertEqual(runner.run(), 1)
                finally:
                    if timer:
                        timer.join(3)
                self.assertEqual(jobs.read_status(runner.job_id)["outcome"], outcome)
                self.assertEqual(self.circuit(), before)
                state = health.query_state(self.runtime, self.runtime.find("a"))
                self.assertEqual((state.state, state.blocked), ("recovering", False))
                lease, _, _ = accounts.acquire(self.runtime, pinned="a", job_id="after-neutral")
                self.assertIsNotNone(lease)
                lease.release()

    def test_outcome_written_before_lease_release_with_generation_and_trial(self):
        self.trip(recovering=True)
        runner = self.runner(account="a", access="read")
        original = health.record_outcome

        def record(runtime, account, model, **kwargs):
            self.assertFalse(kwargs["trials"]._released)
            self.assertEqual(kwargs["generation"], kwargs["trials"].generation)
            self.assertEqual(accounts.busy_slots(runtime, account), 1)
            self.assertTrue(health.query_state(runtime, account).blocked)
            return original(runtime, account, model, **kwargs)

        with patch.object(health, "record_outcome", side_effect=record) as write:
            self.assertEqual(runner.run(), 0)
        self.assertEqual(write.call_count, 1)
        self.assertTrue(write.call_args.kwargs["trials"]._released)

    def test_health_write_errors_keep_success_error_and_usage_limit_failover(self):
        for scenario, outcome in (("success", "success"), ("no_result", "error")):
            with self.subTest(scenario=scenario):
                os.environ["FAKE_CODEX_SCENARIO"] = scenario
                runner = self.runner(account="a", access="read")
                with patch.object(health, "record_outcome", side_effect=OSError("health storage unavailable")):
                    runner.run()
                self.assertEqual(jobs.read_status(runner.job_id)["outcome"], outcome)
        os.environ.update(FAKE_CODEX_SCENARIO="success", FAKE_CODEX_LIMITED="a")
        runner = self.runner(access="read")
        with patch.object(health, "record_outcome", side_effect=OSError("health storage unavailable")):
            self.assertEqual(runner.run(), 0)
        status = jobs.read_status(runner.job_id)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a", "b"])
        self.assertEqual(status["outcome"], "success")
        self.assertIsNotNone(accounts.cooldown_until(accounts.load_usage("codex")["a"]))
        self.assertIn("a hit its usage limit; continuing on another account", status["warnings"])

    def test_reset_during_trial_drops_the_runners_late_outcome(self):
        self.trip(recovering=True)
        runner = self.runner(account="a", access="read")
        original = health.record_outcome

        def reset_then_record(runtime, account, model, **kwargs):
            health.reset(runtime, account)
            return original(runtime, account, model, **kwargs)

        with patch.object(health, "record_outcome", side_effect=reset_then_record):
            self.assertEqual(runner.run(), 0)
        record = health.read_record(self.runtime, self.runtime.find("a"))
        self.assertEqual((record["generation"], record["circuits"]), (1, {}))
        self.assertEqual(accounts.busy_slots(self.runtime, self.runtime.find("a")), 0)

    def test_capacity_retries_record_one_failure_then_move_with_capacity_handoff(self):
        self.runtime.capacity_retries = 2
        self.save()
        os.environ["FAKE_CODEX_SCENARIOS"] = json.dumps({"a": "capacity"})
        runner = self.runner(access="read")
        with patch.object(runner, "_wait_for_capacity", return_value=None) as wait:
            self.assertEqual(runner.run(), 0)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual([a["account"] for a in runner.attempts], ["a", "a", "a", "b"])
        self.assertEqual(self.circuit(scope="model:default")["failures"], 1)
        self.assertEqual(self.circuit(), {})
        self.assertIn("a is unhealthy (model at capacity); continuing on another account", runner.warnings)
        self.assertIn("model at capacity", self.records()[-1]["stdin"])
        self.assertTrue(all(accounts.cooldown_until(r) is None for r in accounts.load_usage("codex").values()))

    def test_CB_S11_finished_codex_follow_up_refused_then_resumes_as_trial(self):
        previous = self.run_job(access="read")
        self.trip()
        before = len(list(jobs.jobs_dir().iterdir()))
        expected = ("a is unhealthy; this session lives on that account, so the follow-up can't run yet. "
                    "Try again after it recovers.")
        command = subprocess.run(
            [sys.executable, "-m", "agents_router", "message", previous["job_id"], "follow up"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(command.returncode, 0)
        self.assertIn(expected, command.stderr + command.stdout)
        self.assertEqual(len(list(jobs.jobs_dir().iterdir())), before)
        health.reset(self.runtime, self.runtime.find("a"))
        self.trip(recovering=True)
        original = jobs.spawn_detached
        with patch.object(jobs, "spawn_detached", return_value=99999999):
            reply = tools.call("subagent_message", {"job_id": previous["job_id"], "text": "follow up"})
        new_job = reply["new_job"]["job_id"]
        self.assertEqual(self.circuit()["claim"]["job"], new_job)
        spec = jobs.read_spec(new_job)
        self.assertEqual((spec.account, spec.resume_account, spec.resume_session), ("a", "a", previous["session_id"]))
        original([sys.executable, "-m", "agents_router", "_runner", new_job], jobs.job_dir(new_job) / "runner.log",
                 env=jobs._runner_env())
        status = jobs.wait_for(new_job, 20)
        self.assertEqual((status["outcome"], status["account"]), ("success", "a"), status)
        self.assertIn("resume", self.records()[-1]["argv"])
        self.assertIn(previous["session_id"], self.records()[-1]["argv"])
        self.assertEqual(health.query_state(self.runtime, self.runtime.find("a")).state, "healthy")

    def test_CB_S11_finished_follow_up_queues_behind_held_trial(self):
        previous = self.run_job(access="read")
        self.trip(recovering=True)
        held, _, _ = accounts.acquire(self.runtime, pinned="a", job_id="held")
        self.addCleanup(lambda: held.release() if not held.handle.closed else None)
        with patch.object(jobs, "spawn_detached", return_value=99999999):
            reply = tools.call("subagent_message", {"job_id": previous["job_id"], "text": "follow up"})
        status = reply["new_job"]
        runner = Runner(status["job_id"])
        expected = "queued · a recovering (trial job running)"
        self.assertEqual(jobs.read_status(runner.job_id)["message"], expected)

        def release(_seconds):
            self.assertEqual(jobs.read_status(runner.job_id)["message"], expected)
            held.release()

        with patch("agents_router.runner.time.sleep", side_effect=release) as sleep:
            self.assertEqual(runner.run(), 0)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(jobs.read_status(runner.job_id)["account"], "a")

    def test_CB_S11_claude_follow_up_transfers_from_unhealthy_profile(self):
        runtime = self.config.runtime("claude")
        runtime.primary = self.primary
        runtime.accounts = self.runtime.accounts
        runtime.order = ["a", "b"]
        self.save()
        previous = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read")
        previous = jobs.wait_for(previous["job_id"], 20)
        self.assertEqual((previous["outcome"], previous["account"]), ("success", "a"), previous)
        for offset in (0, 1):
            health.record_outcome(runtime, runtime.find("a"), outcome="error", generation=0,
                                  health_failure="transport", now=time.time() - 2 + offset)
        reply = tools.call("subagent_message", {"job_id": previous["job_id"], "text": "follow up"})
        status = jobs.wait_for(reply["new_job"]["job_id"], 20)
        self.assertEqual((status["outcome"], status["account"]), ("success", "b"), status)
        self.assertEqual(status["attempts"][0]["continued_from"], "a")
        self.assertIn(previous["session_id"], self.records()[-1]["argv"])

    def test_claude_read_health_move_transfers_session_and_names_cause(self):
        runtime = self.config.runtime("claude")
        runtime.primary = self.primary
        runtime.accounts = self.runtime.accounts
        runtime.order = ["a", "b"]
        self.save()
        status = jobs.create_job(runtime="claude", prompt="task", cwd=str(self.project), access="read", start=False)
        runner = Runner(status["job_id"])
        original = runner.runtime.launch

        def launch(config, account, *args):
            result = original(config, account, *args)
            result.env["FAKE_CLAUDE_SCENARIO"] = "transport" if account.name == "a" else "success"
            return result

        with patch.object(runner.runtime, "launch", side_effect=launch):
            self.assertEqual(runner.run(), 0)
        status = jobs.read_status(runner.job_id)
        self.assertEqual([a["account"] for a in status["attempts"]], ["a", "b"])
        self.assertEqual(status["attempts"][-1]["continued_from"], "a")
        self.assertIn("a is unhealthy (connection failed); continuing on another account", status["warnings"])
        self.assertIn("connection failed", self.records()[-1]["stdin"])
        self.assertNotIn("usage limit", self.records()[-1]["stdin"])

    def test_all_unhealthy_no_account_text_and_health_before_execution_caps(self):
        self.runtime.when_full = "queue"
        self.save()
        self.trip()
        self.trip("b")
        status = self.run_job(access="read")
        self.assertEqual(status["outcome"], "no_account", status)
        self.assertRegex(status["message"], r"^every codex account is unhealthy; retrying around \d\d:\d\d$")
        runner = self.runner(access="read")
        with patch.object(limits, "acquire_execution_slots", return_value=None) as cap:
            with patch("agents_router.runner.time.sleep", side_effect=AssertionError("waited behind full cap")):
                self.assertEqual(runner.run(), 1)
        cap.assert_not_called()
        self.assertEqual(jobs.read_status(runner.job_id)["outcome"], "no_account")

    def test_model_unhealthy_and_mixed_quota_messages_remain_distinct(self):
        self.runtime.when_full = "queue"
        self.save()
        self.trip(kind="capacity", model="model-x")
        self.trip("b", kind="capacity", model="model-x")
        status = self.run_job(access="read", model="model-x")
        self.assertEqual(status["outcome"], "no_account", status)
        self.assertRegex(status["message"], r"^every codex account is unhealthy for model-x; retrying around \d\d:\d\d$")
        until = time.time() + 600
        accounts.set_cooldown("codex", "b", until, "usage limit")
        status = self.run_job(access="read", model="model-x")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
        self.assertEqual(status["message"], f"every codex account is at its usage limit; the earliest resets around {when}")
        self.assertNotIn("cooldown_until", accounts.load_usage("codex").get("a", {}))

    def test_unnamed_model_health_message_uses_the_default_scope_name(self):
        self.runtime.when_full = "queue"
        self.save()
        self.trip(kind="capacity")
        self.trip("b", kind="capacity")
        status = self.run_job(access="read")
        self.assertEqual(status["outcome"], "no_account", status)
        self.assertRegex(status["message"], r"^every codex account is unhealthy for default; retrying around \d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
