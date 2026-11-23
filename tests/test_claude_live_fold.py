"""Every usage reader folds the status line tap's live Claude figures into each account (S1, S2, S4)."""

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from claude_live_helpers import ClaudeLiveFixture, LIVE_SOURCE

from agents_router import accounts, cli, limits, usage_snapshot, usage_view
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig
from agents_router.fsutil import write_json

NOW = 1_800_000_000.0  # on a whole minute
HOUR = 3600
DAY = 86400


def listing(root):
    """Every path under `root` with its mode, mtime and bytes, the root included."""

    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return result


def live_windows(five=0.62, week=0.43, observed=NOW - 30, five_resets=NOW + 2 * HOUR):
    return {"five_hour": {"utilization": five, "resets_at": five_resets, "observed_at": observed,
                           "observed_at_source": LIVE_SOURCE},
            "seven_day": {"utilization": week, "resets_at": NOW + 3 * DAY, "observed_at": observed,
                           "observed_at_source": LIVE_SOURCE}}


class LiveFoldTests(ClaudeLiveFixture):
    def setUp(self):
        super().setUp()
        getattr(accounts, "_LIVE_CACHE", {}).clear()
        clock = patch("time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.login = self.login_hash("uuid-x")
        self.other_login = self.login_hash("uuid-other")
        # Two folders logged in to one account: X (the person's own profile) and Y.
        self.x = Account("claude-x", self.root / "claude-x", login=self.login)
        self.y = Account("claude-y", self.root / "claude-y", login=self.login)
        for account in (self.x, self.y):
            account.home.mkdir()
            self.claude_json(account, "uuid-x")
        self.runtimes = {name: RuntimeConfig(name, self.root / name, _found=[]) for name in RUNTIMES}
        self.runtime = self.runtimes["claude"]
        self.runtime.accounts = [self.x, self.y]
        self.runtime.primary = self.x.home
        self.config = Config(self.runtimes)
        # The same two folders in settings, for the real `agt usage --json` (no patched load_config: a module first
        # imported under that patch would keep the mock for later tests).
        (self.home / "settings.toml").write_text("\n".join([
            "[claude]", f"primary = {json.dumps(str(self.x.home))}",
            *(line for account in (self.x, self.y) for line in (
                "[[claude.accounts]]", f"name = {json.dumps(account.name)}", f"home = {json.dumps(str(account.home))}",
                'setup = "own"'))]) + "\n", encoding="utf-8")

    def live(self, account, windows=None, login=None):
        return super().live(account, live_windows() if windows is None else windows, login)

    def usage(self, data):
        write_json(self.home / "state" / "claude-usage.json", data)

    def job_record(self, five=0.2, observed=NOW - 600, five_resets=NOW + 2 * HOUR + 10):
        """What a job's rate_limit_event left: one 5-hour window, stamped when it was seen."""

        return {"login": self.login, "updated": observed, "windows": {
            "five_hour": {"utilization": five, "resets_at": five_resets, "observed_at": observed}}}

    def report(self):
        """`agt usage --json`, through the CLI and the settings file."""

        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.main(["usage", "--json"]), 0)
        return json.loads(stream.getvalue())

    def entry(self, report, name):
        return next(a for p in report["providers"] if p["runtime"] == "claude" for a in p["accounts"]
                    if a["name"] == name)

    def windows(self, name):
        account = self.entry(self.report(), name)
        self.assertNotIn("problem", account)
        return {w["name"]: w for w in account["windows"]}

    def percents(self, name):
        return {key: window["used_percent"] for key, window in self.windows(name).items()}

    def test_usage_json_reports_the_live_figures_and_reads_without_writing(self):
        # S1: the person's own profile, job figures from 20 days ago (both windows reset since), live figures now.
        self.usage({self.x.name: {"login": self.login, "updated": NOW - 20 * DAY, "windows": {
            "five_hour": {"utilization": 0.5, "resets_at": NOW - 19 * DAY},
            "seven_day": {"utilization": 0.43, "resets_at": NOW - 14 * DAY}}}})
        self.live(self.x)
        before = listing(self.home)
        for _ in range(2):  # the second read comes from the cache
            windows = self.windows(self.x.name)
            self.assertEqual({name: w["used_percent"] for name, w in windows.items()},
                             {"five_hour": 62, "seven_day": 43})
            for window in windows.values():
                self.assertEqual((window["stale"], window["reset"], window["observed_at"]), (False, False, NOW - 30))
        self.assertEqual(limits.utilization(accounts.account_record(self.runtime, self.x, accounts.load_usage("claude")),
                                            NOW), 0.62)
        self.assertEqual(listing(self.home), before)
        # The room line reads through account_record too (its health summary takes a lock file, as it always has).
        self.assertIn("claude-x 38% left (5h", usage_view.room_line(self.config, ["claude"], NOW))
        # An account with no job record at all still reports the live figures.
        self.usage({})
        self.assertEqual(self.percents(self.x.name), {"five_hour": 62, "seven_day": 43})

    def test_a_same_login_folder_sees_the_newest_observation_not_the_busiest(self):
        # Y's job saw 90% three hours ago; X's status line saw 30% a minute ago, in the same 5-hour window
        # (a mid-window limit reset). Both folders share one account's limits, so both show the newer 30%.
        self.usage({self.y.name: self.job_record(five=0.9, observed=NOW - 3 * HOUR)})
        self.live(self.x, {"five_hour": {"utilization": 0.3, "resets_at": NOW + 2 * HOUR, "observed_at": NOW - 60}})
        for account in (self.x, self.y):
            with self.subTest(account=account.name):
                record = accounts.account_record(self.runtime, account, accounts.load_usage("claude"), now=NOW)
                self.assertEqual(record["windows"]["five_hour"]["utilization"], 0.3)
                self.assertEqual(self.percents(account.name), {"five_hour": 30})
        # Y's own live file, older and busier, doesn't win either: the newest observation does.
        self.live(self.y, {"five_hour": {"utilization": 0.95, "resets_at": NOW + 2 * HOUR, "observed_at": NOW - HOUR}})
        self.assertEqual(self.percents(self.y.name), {"five_hour": 30})
        # The stored records are never changed by a read.
        self.assertEqual(accounts.load_usage("claude")[self.y.name]["windows"]["five_hour"]["utilization"], 0.9)

    def test_corrupt_or_unreadable_live_file_keeps_the_job_figures(self):
        self.usage({self.x.name: self.job_record()})
        self.live(self.x)
        self.assertEqual(self.percents(self.x.name), {"five_hour": 62, "seven_day": 43})  # a good file folds
        nan_window = (b'{"login": "%s", "windows": {"five_hour": {"utilization": NaN, "resets_at": 1, '
                      b'"observed_at": 1, "observed_at_source": "transcript_assistant_v1"}}}')
        bad = {
            "truncated": b'{"login": "',
            "not utf-8": b"\xff\xfe\x00",
            "deeply nested": b"[" * 100_000 + b"]" * 100_000,
            "not an object": b"[1, 2]",
            "NaN": nan_window % self.login.encode(),
            "windows not an object": {"login": self.login, "windows": [1]},
            "bad windows": {"login": self.login, "windows": {
                "five_hour": {"utilization": True, "resets_at": NOW + HOUR, "observed_at": NOW,
                              "observed_at_source": LIVE_SOURCE},
                "seven_day": {"utilization": -0.1, "resets_at": NOW + DAY, "observed_at": NOW,
                              "observed_at_source": LIVE_SOURCE},
                "opus": {"utilization": 0.5, "resets_at": "soon", "observed_at": NOW,
                         "observed_at_source": LIVE_SOURCE},
                "sonnet": {"utilization": 0.5, "resets_at": NOW + DAY, "observed_at_source": LIVE_SOURCE},
                "other": "62%"}},
        }
        for label, content in bad.items():
            with self.subTest(label):
                self.put_live(self.x, content)
                self.assertEqual(self.percents(self.x.name), {"five_hour": 20})
        path = self.live_path(self.x)
        path.unlink()
        path.mkdir()
        with self.subTest("a folder"):
            self.assertEqual(self.percents(self.x.name), {"five_hour": 20})
        path.rmdir()
        if hasattr(os, "mkfifo"):
            os.mkfifo(path)
            with self.subTest("a FIFO, which must not hang the read"):
                self.assertEqual(self.percents(self.x.name), {"five_hour": 20})
            path.unlink()
        # One bad window is skipped; the good one beside it still folds.
        windows = live_windows()
        windows["five_hour"]["observed_at"] = None
        self.live(self.x, windows)
        self.assertEqual(self.percents(self.x.name), {"five_hour": 20, "seven_day": 43})

    def test_a_live_file_for_another_login_is_ignored(self):
        self.usage({self.x.name: self.job_record()})
        self.live(self.x, login=self.login)
        self.assertEqual(self.percents(self.x.name), {"five_hour": 62, "seven_day": 43})  # its own login folds
        self.live(self.x, login=self.other_login)
        self.assertEqual(self.percents(self.x.name), {"five_hour": 20})
        # A file from before the tap tagged logins names none: no login, no trust.
        self.put_live(self.x, {"limits": True, "windows": live_windows()})
        self.assertEqual(self.percents(self.x.name), {"five_hour": 20})
        # The folder now holds login B while the file names A: A's figures never show under B.
        self.live(self.x, login=self.login)
        self.claude_json(self.x, "uuid-other")
        self.claude_json(self.y, "uuid-other")
        self.usage({})
        self.assertEqual(self.percents(self.x.name), {})
        # An account whose login is unknown (none set, and none in its folder's .claude.json) trusts no live file.
        (self.x.home / ".claude.json").unlink()
        unknown = Account(self.x.name, self.x.home, login=None)
        self.assertIsNone(accounts.account_record(self.runtime, unknown, {}, now=NOW))
        record = accounts.account_record(self.runtime, self.x, {}, now=NOW)
        self.assertEqual(record["windows"]["five_hour"]["utilization"], 0.62)  # the same file, the matching login

    def test_legacy_or_unknown_observation_sources_leave_job_figures_or_nothing(self):
        for marker in (None, "unverified", 7):
            with self.subTest(marker=marker):
                windows = live_windows(observed=NOW)
                for window in windows.values():
                    window.pop("observed_at_source")
                    if marker is not None:
                        window["observed_at_source"] = marker
                self.put_live(self.x, {"login": self.login, "limits": True, "windows": windows})
                job = self.job_record()
                record = accounts.account_record(self.runtime, self.x, {self.x.name: job}, now=NOW)
                self.assertEqual(record, job)
                self.assertIsNone(accounts.account_record(self.runtime, self.x, {}, now=NOW))
                self.assertEqual(job, self.job_record())  # job records gain no live metadata

    def test_marked_winning_windows_keep_metadata_through_folds_and_out_of_public_usage(self):
        x_windows = live_windows()
        for window in x_windows.values():
            window.update(session_id="s" * 128, claude_version="v" * 64)
        self.put_live(self.x, {"login": self.login, "windows": x_windows})
        y_windows = live_windows(five=0.7, observed=NOW - 10)
        y_windows.pop("seven_day")
        y_windows["five_hour"].update(session_id="other-session", claude_version="2.1.81")
        self.put_live(self.y, {"login": self.login, "windows": y_windows})
        expected = {"five_hour": y_windows["five_hour"], "seven_day": x_windows["seven_day"]}
        for usage in ({}, {self.x.name: self.job_record()}):
            for account in (self.x, self.y):
                with self.subTest(job=bool(usage), account=account.name):
                    record = accounts.account_record(self.runtime, account, usage, now=NOW)
                    self.assertEqual(record["windows"], expected)
        public = json.dumps(self.report())
        for key in ("observed_at_source", "session_id", "claude_version"):
            self.assertNotIn(key, public)

    def test_live_reader_revalidates_metadata_in_hand_written_files(self):
        for key, other, bound in (("session_id", "claude_version", 128), ("claude_version", "session_id", 64)):
            for value in ("x" * (bound + 1), "bad\nvalue", "bad\x7fvalue", "caf\u00e9", 7, True, None, []):
                with self.subTest(key=key, value=value):
                    window = {**live_windows()["five_hour"], key: value, other: "valid",
                              "transcript_path": "must-not-survive", "content": "must-not-survive"}
                    expected = {**live_windows()["five_hour"], other: "valid"}
                    self.assertEqual(accounts._live_window(window), expected)
                    self.put_live(self.x, {"login": self.login, "windows": {"five_hour": window}})
                    record = accounts.account_record(self.runtime, self.x, {}, now=NOW)
                    self.assertEqual(record["windows"], {"five_hour": expected})
        for session, version in (("s" * 128, "v" * 64), (" ~", " ~"), ("", "")):
            with self.subTest(session=session, version=version):
                window = {**live_windows()["five_hour"], "session_id": session, "claude_version": version}
                self.assertEqual(accounts._live_window(window), window)

    def test_settings_listed_accounts_and_primary_take_their_folders_login(self):
        """Accounts from settings carry no login in load_config: admission and the room line read the folder's own,
        as `agt usage --json` does, so all three show the same live figures."""

        from agents_router.config import load_config

        self.live(self.x)
        runtime = load_config().runtime("claude")
        listed = {account.name: account for account in runtime.all_accounts()}
        self.assertEqual((listed["claude-x"].login, listed["claude-y"].login), (None, None))
        usage = accounts.load_usage("claude")
        for name in ("claude-x", "claude-y"):  # Y shares X's login, so X's live figures hold for it too
            with self.subTest(account=name):
                self.assertEqual(self.five(listed[name], usage, runtime), 0.62)
        self.assertEqual(self.percents("claude-x"), {"five_hour": 62, "seven_day": 43})
        line = usage_view.room_line(load_config(), ["claude"], NOW)
        self.assertIn("claude-x 38% left (5h", line)
        self.assertIn("claude-y 38% left (5h", line)
        # The stand-in `primary` (a primary folder agt doesn't list by another name) reads its folder's login too.
        primary = self.root / "primary-folder"
        primary.mkdir()
        (primary / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-p"}}),
                                              encoding="utf-8")
        (self.home / "settings.toml").write_text(f"[claude]\nprimary = {json.dumps(str(primary))}\n", encoding="utf-8")
        stand_in = load_config().runtime("claude").primary_account()
        self.assertEqual((stand_in.name, stand_in.login), ("primary", None))
        self.live(Account("p", primary), login=self.login_hash("uuid-p"))
        self.assertEqual(self.five(stand_in, {}, load_config().runtime("claude")), 0.62)
        # A folder now logged in to another account ignores the live file of the old one.
        (primary / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-other-p"}}),
                                              encoding="utf-8")
        self.assertIsNone(self.five(stand_in, {}, load_config().runtime("claude")))

    def test_historical_grouping_cannot_authorize_a_live_file_without_a_current_login(self):
        self.usage({self.x.name: self.job_record(),
                    self.y.name: self.job_record(five=0.4, observed=NOW - 300)})
        self.live(self.x)
        for account in (self.x, self.y):
            (account.home / ".claude.json").unlink()
        before = listing(self.home)
        for account in (self.x, self.y):
            with self.subTest(account=account.name):
                # The historical login still groups job figures, but cannot authorize X's live 62/43%.
                self.assertEqual(self.percents(account.name), {"five_hour": 40})
        self.assertEqual(listing(self.home), before)

    def test_login_since_is_private_to_the_live_file(self):
        self.put_live(self.x, {"login": self.login, "login_since": NOW - 60, "limits": True,
                               "windows": live_windows()})
        self.assertEqual(self.five(self.x, {}), 0.62)
        report = self.report()
        self.assertEqual(self.percents(self.x.name), {"five_hour": 62, "seven_day": 43})
        self.assertNotIn("login_since", json.dumps(report))

    def five(self, account, usage, runtime=None):
        """The 5-hour utilization `account_record` gives the account, or None."""

        record = accounts.account_record(runtime or self.runtime, account, usage, now=NOW) or {}
        return ((record.get("windows") or {}).get("five_hour") or {}).get("utilization")

    def test_a_changed_live_file_is_read_again_and_an_unchanged_one_is_not(self):
        self.live(self.x)
        usage = {}
        with patch("os.read", wraps=os.read) as read:
            self.assertEqual(self.five(self.x, usage), 0.62)
            reads = read.call_count
            self.assertGreater(reads, 0)
            self.assertEqual(self.five(self.x, usage), 0.62)
            self.assertEqual(read.call_count, reads)  # parsed once per file version
        self.live(self.x, live_windows(five=0.7))
        self.assertEqual(self.five(self.x, usage), 0.7)
        self.assertEqual(usage, {})

    def test_only_claude_folds_live_files(self):
        self.live(self.x)
        self.assertEqual(self.five(self.x, {}), 0.62)
        codex = self.runtimes["codex"]
        account = Account(self.x.name, self.x.home, login=self.login)
        codex.accounts = [account]
        self.assertIsNone(self.five(account, {}, codex))

if __name__ == "__main__":
    unittest.main()
