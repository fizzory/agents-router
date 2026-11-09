"""Health state, claims and leases; subprocesses only run Python in isolated homes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import health
from agents_router.accounts import _home_key
from agents_router.config import Account, RuntimeConfig
from agents_router.fsutil import process_start_time, read_json, try_lock, unlock, write_json


# Inherit helpers' isolated environment. No worker invokes any model CLI.
WRITE_FAILURE = """
import sys
from pathlib import Path
from agents_router import health
from agents_router.config import Account, RuntimeConfig
runtime = RuntimeConfig('codex', Path(sys.argv[1]), circuit_failure_threshold=100)
account = Account('work', Path(sys.argv[1]))
health.record_outcome(runtime, account, 'm', outcome='error',
                      health_failure='transport', generation=0, now=1000)
"""

HOLD_TRIAL = """
import sys
from pathlib import Path
from agents_router import health
from agents_router.config import Account, RuntimeConfig
from agents_router.fsutil import process_start_time
runtime = RuntimeConfig('codex', Path(sys.argv[1]))
account = Account('work', Path(sys.argv[1]))
lease = health.acquire_trial(runtime, account, 'm', 'runner', generation=0, now=1061)
assert lease is not None and lease.scopes == ('account',)
lease.set_owner(int(sys.argv[2]), process_start_time(int(sys.argv[2])))
Path(sys.argv[3]).write_text('ready', encoding='utf-8')
sys.stdin.read()
lease.release()
"""


class HealthTests(TempHome):
    def setUp(self):
        super().setUp()
        self.runtime = RuntimeConfig("codex", self.primary)
        self.account = Account("work", self.primary)
        self.path = self.home / "state" / "codex-health.json"

    def state(self, now=1000, model="m", **kwargs):
        return health.query_state(self.runtime, self.account, model, now=now, **kwargs)

    def health_record(self, now=1001):
        return health.read_record(self.runtime, self.account, now=now)

    def outcome(self, kind="transport", now=1000, model="m", *, generation=0, trials=None):
        return health.record_outcome(
            self.runtime, self.account, model, outcome="success" if kind == "success" else "error",
            generation=generation, health_failure=None if kind == "success" else kind, trials=trials, now=now)

    def trip(self, kind="transport", now=1000, model="m", *, generation=0):
        self.outcome(kind, now, model, generation=generation)
        self.outcome(kind, now + 1, model, generation=generation)

    def acquire(self, job="J", now=1061, *, generation=0, **kwargs):
        lease = health.acquire_trial(self.runtime, self.account, "m", job,
                                     generation=generation, now=now, **kwargs)
        if lease is not None:
            self.addCleanup(lease.release)
        return lease

    def claim(self, job="J", now=1061, *, generation=0, **kwargs):
        return health.claim_trial(self.runtime, self.account, "m", job,
                                  generation=generation, now=now, **kwargs)

    def test_CB_S1_two_capacity_failures_trip_only_the_named_model(self):
        """CB-S1: two failures inside 300 s open only account/model capacity."""

        self.outcome("capacity", 1000)
        self.assertEqual(self.state().state, "healthy")
        self.outcome("capacity", 1300)
        state = self.state(1300)
        self.assertEqual(state.state, "unhealthy")
        self.assertTrue(state.blocked)
        self.assertEqual([(c.scope, c.state) for c in state.circuits],
                         [("account", "healthy"), ("model:m", "unhealthy")])
        self.assertEqual(state.circuits[1].until, 1360)
        self.assertEqual(state.circuits[1].reason, "model at capacity")
        self.assertEqual(self.state(1300, model="other").state, "healthy")
        self.assertFalse((self.home / "state" / "codex-usage.json").exists())

    def test_failures_301_seconds_apart_start_a_new_window(self):
        self.outcome(now=1000)
        self.outcome(now=1301)
        c = self.health_record(1301)["circuits"]["account"]
        self.assertEqual((c["failures"], c["first_failure"]), (1, 1301))
        self.assertEqual(self.state(1301).state, "healthy")

    def test_failure_window_runs_from_first_failure_not_previous_failure(self):
        self.runtime.circuit_failure_threshold = 3
        for now in (1000, 1200, 1400):
            self.outcome(now=now)
        self.assertEqual(self.health_record(1400)["circuits"]["account"]["failures"], 1)
        self.assertEqual(self.state(1400).state, "healthy")

    def test_CB_S4_rest_expires_at_boundary_and_trial_success_resets(self):
        """CB-S4: rest expiry admits a real trial, whose success resets backoff."""

        self.trip()
        self.assertEqual(self.state(1060).state, "unhealthy")
        self.assertIsNone(self.acquire(now=1060))
        self.assertEqual(self.state(1061).trials, ("account",))
        lease = self.acquire()
        self.assertIsNotNone(lease)
        self.outcome(now=1062, trials=lease)
        lease.release()
        self.assertEqual(self.health_record(1062)["circuits"]["account"]["open_seconds"], 120)
        lease = self.acquire(now=1182)
        self.outcome("success", 1183, trials=lease)
        lease.release()
        for scope in ("account", "model:m"):
            c = self.health_record(1183)["circuits"][scope]
            self.assertEqual((c["failures"], c["open_seconds"], c["opened_at"], c["claim"]),
                             (0, 60, None, None))
        self.assertEqual(self.state(1183).state, "healthy")

    def test_CB_S4_failed_trials_double_rest_up_to_cap_forever(self):
        """CB-S4: unsuccessful real trials rest 120, 240, 480, 900, 900 s."""

        self.trip()
        now = 1061
        for expected in (120, 240, 480, 900, 900):
            with self.subTest(seconds=expected):
                lease = self.acquire(now=now)
                self.assertIsNotNone(lease)
                self.outcome(now=now, trials=lease)
                lease.release()
                c = self.health_record(now)["circuits"]["account"]
                self.assertEqual((c["open_seconds"], c["open_until"]), (expected, now + expected))
                now += expected

    def test_claim_blocks_other_jobs_and_is_adopted_by_its_runner(self):
        self.trip()
        claimed = self.claim()
        self.assertEqual(claimed.trials, ("account",))
        self.assertEqual(self.health_record(1061)["circuits"]["account"]["claim"],
                         {"job": "J", "generation": 0, "deadline": 1181})
        self.assertTrue(self.state(1061).blocked)
        self.assertFalse(self.state(1061, job_id="J").blocked)
        self.assertIsNone(self.claim("K"))
        self.assertIsNone(self.acquire("K"))
        self.assertIsNotNone(self.claim(now=1100))  # idempotent: do not extend deadline
        self.assertEqual(self.health_record(1100)["circuits"]["account"]["claim"]["deadline"], 1181)
        lease = self.acquire(now=1100)
        self.assertIsNotNone(lease)
        self.assertIsNone(self.health_record(1100)["circuits"]["account"]["claim"])
        self.assertIsNone(self.acquire("K", now=1182))  # lease outlives the claim deadline
        lease.release()
        self.assertIsNotNone(self.acquire("K", now=1182))

    def test_unadopted_claim_expires_after_120_seconds_and_needs_fresh_acquire(self):
        self.trip()
        self.claim()
        self.assertIsNone(self.claim("K", now=1180))
        self.assertIsNotNone(self.claim("K", now=1181))
        self.assertIsNone(self.acquire("J", now=1181))
        health.release_claim(self.runtime, self.account, "K", generation=0, now=1181)
        self.assertIsNotNone(self.acquire("J", now=1181))

    def test_release_claim_only_releases_its_job_and_generation(self):
        self.trip()
        self.claim()
        for job, generation in (("K", 0), ("J", 1)):
            health.release_claim(self.runtime, self.account, job, generation=generation, now=1061)
            self.assertIsNone(self.claim("K"))
        health.release_claim(self.runtime, self.account, "J", generation=0, now=1061)
        self.assertIsNotNone(self.claim("K"))

    def test_second_trial_lease_is_refused_while_first_is_held(self):
        self.trip()
        lease = self.acquire()
        self.assertIsNotNone(lease)
        lease.set_owner(os.getpid(), process_start_time(os.getpid()))
        self.assertTrue(self.state(1061).circuits[0].trial_taken)
        self.assertIsNone(self.acquire("K"))
        lease.release()
        lease.release()
        self.assertIsNotNone(self.acquire("K"))

    def test_both_trials_are_claimed_and_acquired_all_or_none(self):
        self.trip()
        self.trip("capacity")
        self.assertEqual(self.claim().trials, ("account", "model:m"))
        lease = self.acquire()
        self.assertEqual(lease.scopes, ("account", "model:m"))
        self.assertTrue(all(c["claim"] is None for c in self.health_record(1061)["circuits"].values()))
        self.assertIsNone(self.acquire("K"))
        lease.release()
        # Simulate another process holding the second lock: no partial account lease/claim.
        path = health._lease_path(self.runtime, self.account, "model:m", 0)
        handle = try_lock(path)
        self.assertIsNotNone(handle)
        try:
            self.assertIsNone(self.claim("K"))
            self.assertIsNone(self.acquire("K"))
            free = try_lock(health._lease_path(self.runtime, self.account, "account", 0))
            self.assertIsNotNone(free)
            unlock(free)
        finally:
            unlock(handle)

    def test_acquire_releases_partial_locks_if_second_lock_is_lost(self):
        self.trip()
        self.trip("capacity")
        self.claim()
        from agents_router.accounts import _take_slot

        def lose_second(path):
            return None if path == health._lease_path(self.runtime, self.account, "model:m", 0) else _take_slot(path)

        with patch("agents_router.accounts._take_slot", side_effect=lose_second):
            self.assertIsNone(self.acquire())
        self.assertIsNotNone(self.acquire())  # original claims and first lock survived cleanup

    def test_reset_clears_claims_rejects_late_writes_and_separates_generations(self):
        self.trip()
        self.claim()
        old_lease = self.acquire()
        self.assertTrue(health.reset(self.runtime, self.account, now=1062))
        record = self.health_record(1062)
        self.assertEqual((record["generation"], record["circuits"]), (1, {}))
        self.assertFalse(self.outcome(now=1063, trials=old_lease))
        self.assertFalse(self.outcome("success", 1063, trials=old_lease))
        self.assertEqual(self.health_record(1063), record)
        self.trip(now=1063, generation=1)
        self.assertIsNone(self.acquire(now=1124, generation=0))
        self.assertIsNone(self.claim(now=1124, generation=0))
        self.assertIsNotNone(self.claim("new", now=1124, generation=1))
        new_lease = self.acquire("new", now=1124, generation=1)
        self.assertIsNotNone(new_lease)
        self.assertNotEqual(old_lease._slots[0][1], new_lease._slots[0][1])
        old_lease.release()
        self.assertTrue(self.state(1124).blocked)  # old cleanup cannot release the new lock

    def test_reset_of_empty_record_keeps_a_tombstone(self):
        self.assertFalse(health.reset(self.runtime, self.account, now=1000))
        self.assertEqual(self.health_record()["generation"], 1)
        self.assertFalse(self.outcome())

    def test_capacity_on_both_trials_doubles_both_rests_in_one_write(self):
        self.trip()
        self.trip("capacity")
        self.trip("capacity", model="other")
        lease = self.acquire()
        with patch("agents_router.health.write_json", wraps=write_json) as writer:
            self.outcome("capacity", 1062, trials=lease)
        self.assertEqual(writer.call_count, 1)
        circuits = self.health_record(1062)["circuits"]
        for scope in ("account", "model:m"):
            self.assertEqual((circuits[scope]["open_seconds"], circuits[scope]["open_until"]), (120, 1182))
        self.assertEqual(circuits["model:other"]["open_seconds"], 60)

    def test_account_failure_doubles_held_model_trial_without_poisoning_other_models(self):
        for kind in ("launch", "transport", "no_result", "parser"):
            with self.subTest(kind=kind):
                generation = self.health_record()["generation"]
                self.trip("capacity", generation=generation)
                lease = self.acquire(generation=generation)
                self.assertEqual(lease.scopes, ("model:m",))
                self.outcome(kind, 1062, generation=generation, trials=lease)
                lease.release()
                circuits = self.health_record(1062)["circuits"]
                self.assertEqual(circuits["account"]["failures"], 1)
                self.assertIsNone(circuits["account"]["open_until"])
                self.assertEqual(circuits["model:m"]["open_seconds"], 120)
                health.reset(self.runtime, self.account, now=1062)

    def test_failure_without_trials_does_not_extend_an_already_open_rest(self):
        self.trip()
        original = self.health_record()["circuits"]["account"].copy()
        self.outcome(now=1010)
        self.outcome("capacity", 1010)
        circuits = self.health_record(1010)["circuits"]
        self.assertEqual(circuits["account"], original)
        self.assertEqual(circuits["model:m"]["failures"], 1)
        self.outcome(now=1061)  # an old, nontrial job cannot reopen a recovering circuit
        self.assertEqual(self.health_record(1061)["circuits"]["account"], original)

    def test_success_from_an_already_running_job_closes_both_circuits(self):
        self.trip()
        self.trip("capacity")
        self.trip("capacity", model="other")
        self.outcome("success", 1002)
        self.assertEqual(self.state(1002).state, "healthy")
        self.assertEqual(self.state(1002, model="other").state, "unhealthy")

    def test_success_on_unchanged_healthy_circuits_does_not_write(self):
        self.assertTrue(self.outcome("success", 1000))
        before = self.path.read_bytes()
        with patch("agents_router.health.write_json", wraps=write_json) as writer:
            self.assertTrue(self.outcome("success", 1001))
        writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_healthy_success_still_saves_clock_repairs_for_another_model(self):
        self.outcome("success", 1000)
        self.outcome("capacity", 2000, model="other")
        with patch("agents_router.health.write_json", wraps=write_json) as writer:
            self.assertTrue(self.outcome("success", 1001))
        self.assertEqual(writer.call_count, 1)
        circuit = read_json(self.path)[_home_key(self.primary)]["circuits"]["model:other"]
        self.assertEqual((circuit["failures"], circuit["first_failure"]), (0, None))

    def test_neutral_outcomes_do_not_count_or_change_recovering_trials(self):
        for kind in (None, "error", "usage_limit", "auth", "timeout", "lost", "cancelled",
                     "max_turns", "no_account", "configuration", "permission"):
            self.assertFalse(health.record_outcome(self.runtime, self.account, "m", outcome=kind,
                                                  generation=0, now=1000))
        self.assertFalse(self.path.exists())
        self.trip()
        lease = self.acquire()
        before = self.health_record(1061)
        self.assertFalse(health.record_outcome(self.runtime, self.account, "m", outcome="cancelled",
                                              generation=0, trials=lease, now=1061))
        lease.release()
        self.assertEqual(self.health_record(1061), before)
        self.assertFalse(self.state(1061).blocked)
        self.assertEqual(self.state(1061).state, "recovering")

    def test_clock_rollback_is_persisted_as_elapsed_and_future_windows_are_cleared(self):
        self.trip(now=2000)
        self.claim(now=2061)
        state = self.state(1000)
        self.assertEqual(state.state, "recovering")
        self.assertFalse(state.blocked)
        c = read_json(self.path)[_home_key(self.primary)]["circuits"]["account"]
        self.assertEqual((c["opened_at"], c["open_until"], c["first_failure"], c["failures"], c["claim"]),
                         (1000, 1000, None, 0, None))
        self.assertEqual(self.state(1500).state, "recovering")

    def test_clock_rollback_clears_a_future_claim_even_without_an_opened_at_rollback(self):
        self.trip()
        self.claim(now=2000)
        self.assertFalse(self.state(1500).blocked)
        self.assertIsNone(self.health_record(1500)["circuits"]["account"]["claim"])

    def test_missing_empty_non_json_and_malformed_records_read_healthy_and_repair_on_write(self):
        key = _home_key(self.primary)
        examples = (None, "", "not-json", "null", "[]", json.dumps({key: {"circuits": None}}),
                    json.dumps({key: 1}), json.dumps({key: {"circuits": {"account": {"failures": "bad"}}}}))
        for contents in examples:
            with self.subTest(contents=contents):
                self.path.unlink(missing_ok=True)
                if contents is not None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(contents, encoding="utf-8")
                self.assertEqual(self.state().state, "healthy")
                self.outcome()
                self.assertEqual(read_json(self.path)[key]["circuits"]["account"]["failures"], 1)

    def test_same_folder_aliases_share_health_but_same_login_in_another_folder_does_not(self):
        self.trip()
        alias = Account("alias", self.primary, login="same")
        other = Account("other", self.root / "other", login="same")
        self.account.login = "same"
        self.assertEqual(health.query_state(self.runtime, alias, "m", now=1001).state, "unhealthy")
        self.assertEqual(health.query_state(self.runtime, other, "m", now=1001).state, "healthy")
        other_runtime = RuntimeConfig("claude", self.primary)
        self.assertEqual(health.query_state(other_runtime, self.account, "m", now=1001).state, "healthy")

    def test_model_default_and_scope_hash_use_portable_paths(self):
        self.assertEqual(health.model_scope(self.runtime), "model:default")
        self.runtime.default_model = "default-m"
        self.assertEqual(health.model_scope(self.runtime), "model:default-m")
        self.assertEqual(health.model_scope(self.runtime, "explicit"), "model:explicit")
        self.trip("capacity", model="a/b:c")
        lease = health.acquire_trial(self.runtime, self.account, "a/b:c", "J", generation=0, now=1061)
        self.addCleanup(lease.release)
        path = lease._slots[0][1]
        self.assertNotIn(":", path.name)
        self.assertNotIn("/", path.name)
        self.assertEqual(path.parent.name, _home_key(self.primary))
        self.assertEqual(path.parent.parent.name, "trial")
        self.assertTrue(path.name.endswith(".g0.lock"))

    def test_custom_threshold_window_and_rest_settings(self):
        self.runtime.circuit_failure_threshold = 3
        self.runtime.circuit_failure_window_seconds = 10
        self.runtime.circuit_open_seconds = 7
        self.runtime.circuit_max_open_seconds = 20
        for now in (1000, 1001, 1010):
            self.outcome(now=now)
        self.assertEqual(self.health_record(1010)["circuits"]["account"]["open_until"], 1017)
        for now, expected in ((1017, 14), (1031, 20), (1051, 20)):
            lease = self.acquire(now=now)
            self.outcome(now=now, trials=lease)
            lease.release()
            self.assertEqual(self.health_record(now)["circuits"]["account"]["open_seconds"], expected)

    def test_live_check_skip_rest_still_requires_exclusive_trials_and_records_outcomes(self):
        self.trip()
        self.assertIsNone(self.claim(now=1002))
        self.assertEqual(self.claim(now=1002, skip_rest=True).trials, ("account",))
        self.assertIsNone(self.claim("K", now=1002, skip_rest=True))
        lease = self.acquire(now=1002, skip_rest=True)
        self.assertIsNotNone(lease)
        self.assertIsNone(self.acquire("K", now=1002, skip_rest=True))
        self.outcome("success", 1003, trials=lease)
        lease.release()
        self.assertEqual(self.state(1003).state, "healthy")

    def test_disabled_breaker_never_reads_writes_locks_or_blocks(self):
        self.trip()
        before = self.path.read_bytes()
        self.runtime.circuit_breaker = False
        def forbidden(*args, **kwargs):
            self.fail("disabled health store performed I/O")
        with patch("agents_router.health.file_lock", forbidden), patch("agents_router.health.read_json", forbidden), \
                patch("agents_router.health.write_json", forbidden), patch("agents_router.health._busy", forbidden):
            self.assertEqual(self.state().state, "healthy")
            self.assertFalse(self.state().blocked)
            self.assertEqual(self.health_record()["circuits"], {})
            self.assertIsNotNone(self.claim())
            lease = self.acquire()
            self.assertEqual(lease.scopes, ())
            lease.set_owner(os.getpid(), process_start_time(os.getpid()))
            lease.release()
            health.release_claim(self.runtime, self.account, "J", generation=0)
            self.assertFalse(self.outcome())
            self.assertFalse(health.reset(self.runtime, self.account))
        self.assertEqual(self.path.read_bytes(), before)

    def test_write_and_lock_errors_propagate_and_failed_adoption_releases_locks(self):
        with patch("agents_router.health.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.outcome()
        with patch("agents_router.health.file_lock", side_effect=TimeoutError("busy")):
            with self.assertRaises(TimeoutError):
                self.outcome()
        self.trip()
        self.claim()
        with patch("agents_router.health._save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.acquire()
        self.assertIsNotNone(self.acquire())

    def test_twenty_concurrent_subprocess_failure_writes_all_land(self):
        writers = []
        try:
            for _ in range(20):
                writers.append(subprocess.Popen([sys.executable, "-c", WRITE_FAILURE, str(self.primary)],
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for writer in writers:
                out, err = writer.communicate(timeout=30)
                self.assertEqual(writer.returncode, 0, out + err)
        finally:
            for writer in writers:
                if writer.poll() is None:
                    writer.kill()
                writer.communicate(timeout=10)
        self.assertEqual(self.health_record()["circuits"]["account"]["failures"], 20)

    def test_killed_runner_trial_stays_held_until_its_worker_exits(self):
        self.trip()
        # A Python stand-in is registered as the trial worker. Keep its Popen in
        # this test so it can be reaped reliably on Windows and POSIX alike.
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        ready = self.root / "trial-ready"
        runner = None
        try:
            runner = subprocess.Popen(
                [sys.executable, "-c", HOLD_TRIAL, str(self.primary), str(worker.pid), str(ready)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 10
            while not ready.exists() and runner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "trial runner did not acquire its lease")
            runner.kill()
            out, err = runner.communicate(timeout=10)
            self.assertNotEqual(runner.returncode, 0, out + err)
            self.assertIsNone(worker.poll())
            self.assertTrue(self.state(1061).blocked)
            self.assertIsNone(self.acquire("K"))
            worker.kill()
            worker.wait(timeout=10)
            self.assertIsNotNone(self.acquire("K"))
        finally:
            if runner is not None:
                if runner.poll() is None:
                    runner.kill()
                runner.communicate(timeout=10)
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
