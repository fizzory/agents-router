"""Claude usage windows keep their own observation time and merge by one total order (S4)."""

import itertools
import json
import unittest
from unittest.mock import patch

from claude_live_helpers import ClaudeLiveFixture

from agents_router import accounts, usage_snapshot, usage_tap, usage_view, window_order
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig, load_config

NOW = 1_800_000_000.0  # on a whole minute
HOUR = 3600
DAY = 86400

# Three sources of one account's windows. Expected merge, per name:
# five_hour: one window (resets in the same minute); C's newer reading wins over A's older, higher one.
# seven_day: A's is expired, so it loses to B's and C's however high it is; B and C tie but for their reset seconds.
# opus_week: only B has it, and it has expired: it stays, so its age can still be shown.
# no_reset: A's has no reset time and has not expired, so it beats C's expired one.
A = {"five_hour": {"utilization": 0.6, "resets_at": NOW + HOUR + 5, "observed_at": NOW - 100},
     "seven_day": {"utilization": 0.95, "resets_at": NOW - 5, "observed_at": NOW - 50},
     "no_reset": {"utilization": 0.5, "observed_at": NOW - 50}}
B = {"seven_day": {"utilization": 0.4, "resets_at": NOW + 5 * DAY + 10, "observed_at": NOW - 300},
     "opus_week": {"utilization": 0.7, "resets_at": NOW - DAY, "observed_at": NOW - 2 * DAY}}
C = {"five_hour": {"utilization": 0.2, "resets_at": NOW + HOUR + 20, "observed_at": NOW - 10},
     "seven_day": {"utilization": 0.4, "resets_at": NOW + 5 * DAY + 25, "observed_at": NOW - 300},
     "no_reset": {"utilization": 0.8, "resets_at": NOW - 60, "observed_at": NOW - 20}}
EXPECTED_NAMES = {"five_hour": C, "opus_week": B, "no_reset": A}

# In each case the last source is the winner, regardless of arrival order.
OBSERVATION_CASES = (
    ("newer observation with an earlier reset", (
        {"seven_day": {"utilization": 0.9, "resets_at": NOW + 5 * DAY, "observed_at": NOW - 4 * DAY}},
        {"seven_day": {"utilization": 0.2, "resets_at": NOW + 3 * DAY, "observed_at": NOW - 60}},
    )),
    ("same reset minute, newer observation then higher utilization", (
        {"five_hour": {"utilization": 0.9, "resets_at": NOW + HOUR + 25, "observed_at": NOW - 300}},
        {"five_hour": {"utilization": 0.2, "resets_at": NOW + HOUR + 20, "observed_at": NOW - 60}},
        {"five_hour": {"utilization": 0.3, "resets_at": NOW + HOUR + 5, "observed_at": NOW - 60}},
    )),
    ("open beats expired even with an older observation", (
        {"five_hour": {"utilization": 0.9, "resets_at": NOW - 1, "observed_at": NOW}},
        {"five_hour": {"utilization": 0.2, "resets_at": NOW + HOUR, "observed_at": NOW - 4 * DAY}},
    )),
    ("equal observations compare reset minute before utilization", (
        {"five_hour": {"utilization": 0.9, "resets_at": NOW + HOUR, "observed_at": NOW - 60}},
        {"five_hour": {"utilization": 0.2, "resets_at": NOW + 2 * HOUR, "observed_at": NOW - 60}},
    )),
)


class ClaudeWindowTests(ClaudeLiveFixture):
    def setUp(self):
        super().setUp()
        clock = patch("time.time", return_value=NOW)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        (self.home / "settings.toml").write_text(f'[claude]\nprimary = {json.dumps(str(self.primary))}\n',
                                                 encoding="utf-8")

    def at(self, when):
        self.clock.return_value = when

    def event(self, info, login=None):
        accounts.record_rate_limit_event("claude", "primary", info, login=login)

    def both(self, five=0.5, week=0.43, login=None):
        self.event({"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": five, "resetsAt": NOW + 4 * HOUR},
            "seven_day": {"utilization": week, "resetsAt": NOW + 5 * DAY}}}, login=login)

    def stored(self):
        return accounts.load_usage("claude")["primary"]

    def write_usage(self, data):
        (self.home / "state" / "claude-usage.json").parent.mkdir(parents=True, exist_ok=True)
        (self.home / "state" / "claude-usage.json").write_text(json.dumps(data), encoding="utf-8")

    def reported(self):
        """The windows `agt usage --json` reports for the primary folder, by name."""

        runtimes = {name: RuntimeConfig(name, self.root / name, _found=[]) for name in RUNTIMES}
        runtime = runtimes["claude"]
        runtime.accounts = [Account("primary", self.primary)]
        runtime.primary = self.primary
        report = usage_snapshot.snapshot(Config(runtimes), now=self.clock.return_value)
        claude = next(p for p in report["providers"] if p["runtime"] == "claude")
        return {w["name"]: w for w in claude["accounts"][0]["windows"]}

    # (a)
    def test_a_one_window_event_keeps_the_other_window(self):
        self.both()
        self.at(NOW + 600)
        self.event({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.55, "resetsAt": NOW + 4 * HOUR})
        windows = self.stored()["windows"]
        self.assertEqual(set(windows), {"five_hour", "seven_day"})
        self.assertEqual(windows["seven_day"]["utilization"], 0.43)
        self.assertEqual(windows["seven_day"].get("observed_at"), NOW)
        self.assertEqual((windows["five_hour"]["utilization"], windows["five_hour"].get("observed_at")), (0.55, NOW + 600))

    # (b)
    def test_b_metadata_writes_never_touch_windows_or_their_age(self):
        self.both()
        before = self.stored()["windows"]
        self.at(NOW + HOUR)
        accounts.update_usage("claude", "primary", last_used=NOW + HOUR)
        accounts.update_usage("claude", "primary", status="allowed")
        accounts.set_cooldown("claude", "primary", NOW + 2 * HOUR, "usage limit")
        accounts.set_pool_cooldown("claude", "primary", "opus", NOW + 2 * HOUR, "usage limit")
        accounts.clear_cooldown("claude", "primary")
        self.assertEqual(self.stored()["windows"], before)
        reported = self.reported()
        for name in ("five_hour", "seven_day"):
            self.assertEqual(reported[name]["observed_at"], NOW, name)
            self.assertTrue(reported[name]["stale"], name)

    # (c)
    def test_c_legacy_windows_read_as_old_as_updated_and_are_stamped_once(self):
        updated = NOW - 20 * DAY
        self.write_usage({"primary": {"updated": updated, "windows": {
            "five_hour": {"utilization": 0.5, "resets_at": updated + 2 * HOUR},
            "seven_day": {"utilization": 0.43, "resets_at": updated + 3 * DAY}}}})
        expected = {"observed_at": updated, "stale": True, "reset": True}
        self.assertEqual(set(self.reported()), {"five_hour", "seven_day"})
        for name, window in self.reported().items():
            self.assertEqual({key: window[key] for key in expected}, expected, name)
        accounts.update_usage("claude", "primary", last_used=NOW)  # the first 0.21 write
        self.assertEqual({name: w.get("observed_at") for name, w in self.stored()["windows"].items()},
                         {"five_hour": updated, "seven_day": updated})
        self.at(NOW + 60)
        accounts.update_usage("claude", "primary", last_used=NOW + 60)  # stamped once: a later write keeps it
        self.assertEqual({name: w.get("observed_at") for name, w in self.stored()["windows"].items()},
                         {"five_hour": updated, "seven_day": updated})
        self.assertEqual(set(self.reported()), {"five_hour", "seven_day"})
        for name, window in self.reported().items():
            self.assertEqual({key: window[key] for key in expected}, expected, name)

    def test_an_expired_window_kept_for_display_never_counts_for_routing(self):
        self.write_usage({"primary": {"updated": NOW, "windows": {
            "seven_day": {"utilization": 0.95, "resets_at": NOW - HOUR, "observed_at": NOW - 2 * DAY},
            "five_hour": {"utilization": 0.2, "resets_at": NOW + HOUR, "observed_at": NOW}}}})
        self.assertEqual(accounts.max_utilization(self.stored(), now=NOW), 0.2)
        self.assertIsNone(accounts.max_utilization({"windows": {"seven_day": {
            "utilization": 0.95, "resets_at": NOW - HOUR}}}, now=NOW))

    # (d)
    def expected_merge(self):
        expected = {name: source[name] for name, source in EXPECTED_NAMES.items()}
        # B's and C's seven_day share every key but their reset seconds: the full-content tiebreak picks one.
        tied = [B["seven_day"], C["seven_day"]]
        self.assertEqual(round(tied[0]["resets_at"] / 60), round(tied[1]["resets_at"] / 60))
        return expected, tied

    def test_d_the_total_order_ignores_arrival_order(self):
        from agents_router.window_order import merge_windows

        expected, tied = self.expected_merge()
        results = [merge_windows(*order, now=NOW) for order in itertools.permutations((A, B, C))]
        for result in results:
            self.assertEqual(result, results[0])
            self.assertEqual({name: result[name] for name in expected}, expected)
            self.assertIn(result["seven_day"], tied)
        self.assertEqual(set(results[0]), {"five_hour", "seven_day", "opus_week", "no_reset"})

    def test_d_update_usage_merges_the_same_in_any_order(self):
        expected, tied = self.expected_merge()
        results = []
        for order in itertools.permutations((A, B, C)):
            self.write_usage({})
            for source in order:
                accounts.update_usage("claude", "primary", windows=json.loads(json.dumps(source)))
            results.append(self.stored()["windows"])
        for result in results:
            self.assertEqual(result, results[0])
            self.assertEqual({name: result.get(name) for name in expected}, expected)
            self.assertIn(result.get("seven_day"), tied)

    def test_d_observation_first_merge_in_every_order(self):
        for label, sources in OBSERVATION_CASES:
            for order in itertools.permutations(sources):
                with self.subTest(case=label, order=order):
                    self.assertEqual(window_order.merge_windows(*order, now=NOW), sources[-1])
                    merged = {}
                    for source in order:
                        merged = window_order.merge_windows(merged, source, now=NOW)
                    self.assertEqual(merged, sources[-1])

    def test_d_observation_first_job_updates_in_every_order(self):
        for label, sources in OBSERVATION_CASES:
            for order in itertools.permutations(sources):
                with self.subTest(case=label, order=order):
                    self.write_usage({})
                    for source in order:
                        accounts.update_usage("claude", "primary", windows=json.loads(json.dumps(source)))
                    self.assertEqual(self.stored()["windows"], sources[-1])

    def test_d_observation_first_live_fold_in_every_order(self):
        login = self.login_hash("uuid-order")
        runtime = RuntimeConfig("claude", self.root / "claude", _found=[])
        runtime.accounts = [Account("one", self.primary, login=login),
                            Account("two", self.root / "two", login=login)]
        for account in runtime.accounts:
            self.claude_json(account, "uuid-order")
        for label, sources in OBSERVATION_CASES:
            for order in itertools.permutations(sources):
                with self.subTest(case=label, order=order):
                    # Permute both the job/live roles and the live folders' order.
                    for account in runtime.accounts:
                        accounts._live_path(account.home).unlink(missing_ok=True)
                    for account, source in zip(runtime.accounts, order[1:]):
                        self.live(account, source, login)
                    usage = {"one": {"login": login, "windows": order[0]}}
                    # A live winner keeps its marker; a job winner never gains one.
                    expected = {name: {**window, **({"observed_at_source": usage_tap.OBSERVED_AT_SOURCE}
                                                  if sources[-1] is not order[0] else {})}
                                for name, window in sources[-1].items()}
                    for account in runtime.accounts:
                        record = accounts.account_record(runtime, account, usage, now=NOW)
                        self.assertEqual(record["windows"], expected)

    def test_d_missing_observation_sorts_lowest_among_open_windows(self):
        unknown = {"seven_day": {"utilization": 0.9, "resets_at": NOW + 5 * DAY}}
        known = {"seven_day": {"utilization": 0.2, "resets_at": NOW + 3 * DAY, "observed_at": NOW - 60}}
        for order in itertools.permutations((unknown, known)):
            with self.subTest(order=order):
                self.assertEqual(window_order.merge_windows(*order, now=NOW), known)

    def test_d_one_login_across_folders_uses_the_same_order(self):
        runtime = RuntimeConfig("claude", self.root / "claude", _found=[])
        one, two = Account("one", self.root / "one", login="L"), Account("two", self.root / "two", login="L")
        runtime.accounts = [one, two]
        usage = {"one": {"login": "L", "updated": NOW - 4000, "windows": {
                     "five_hour": {"utilization": 0.9, "resets_at": NOW + HOUR}}},
                 "two": {"login": "L", "windows": {
                     "five_hour": {"utilization": 0.2, "resets_at": NOW + HOUR, "observed_at": NOW - 10}}}}
        for account in (one, two):
            merged = accounts.account_record(runtime, account, usage, now=NOW)
            # The newer reading of the same window wins, even though it is lower (a mid-window limit reset).
            self.assertEqual(merged["windows"]["five_hour"]["utilization"], 0.2)
        usage["two"]["windows"]["five_hour"]["resets_at"] = NOW - 1  # expired: the older open window wins, with its age
        merged = accounts.account_record(runtime, one, usage, now=NOW)
        self.assertEqual(merged["windows"]["five_hour"]["utilization"], 0.9)
        self.assertEqual(merged["windows"]["five_hour"].get("observed_at"), NOW - 4000)

    # (e)
    def test_e_quota_and_room_line_show_the_windows_own_age(self):
        self.both(five=0.4, week=0.2)
        self.at(NOW + 2 * HOUR)
        accounts.update_usage("claude", "primary", last_used=NOW + 2 * HOUR)
        config = load_config()
        account = config.runtime("claude").primary_account()
        quota = usage_view.account_quota("claude", account, self.stored(), NOW + 2 * HOUR)
        self.assertEqual((quota["window"], quota["observed_at"]), ("five_hour", NOW))
        self.assertIn("as of 2 h ago", quota["text"])
        line = usage_view.room_line(config, ["claude"], NOW + 2 * HOUR)
        self.assertIn("60% left", line)
        self.assertIn("as of 2 h ago", line)

    # (f)
    def test_f_another_login_replaces_the_windows(self):
        self.both(login="login-a")
        self.at(NOW + 60)
        self.event({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1, "resetsAt": NOW + 5 * HOUR},
                   login="login-b")
        record = self.stored()
        self.assertEqual(record["login"], "login-b")
        self.assertEqual(set(record["windows"]), {"five_hour"})
        self.assertEqual(record["windows"]["five_hour"]["utilization"], 0.1)
        # The same login merges: B's week reading keeps B's 5h.
        self.at(NOW + 120)
        self.event({"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.3, "resetsAt": NOW + 6 * DAY},
                   login="login-b")
        self.assertEqual({name: w["utilization"] for name, w in self.stored()["windows"].items()},
                         {"five_hour": 0.1, "seven_day": 0.3})


if __name__ == "__main__":
    unittest.main()
