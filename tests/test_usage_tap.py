"""The status line tap (`agt hook statusline`) records live Claude figures in `state/claude-live/` (S1, S2, S5)."""

import ast
import contextlib
from datetime import datetime, timezone
import io
import itertools
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import set_times

from agents_router import accounts, cli, entry, hooks, profiles, usage_tap, window_order
from agents_router.share import triggers

SRC = Path(__file__).resolve().parents[1] / "src"
TAP = SRC / "agents_router" / "usage_tap.py"
NOW = 1_800_000_000.0  # on a whole minute
HOUR = 3600
DAY = 86400
FIVE_RESET = NOW + 3 * HOUR
WEEK_RESET = NOW + 4 * DAY
TRANSCRIPT_SCAN = 1 << 20
LIVE_METADATA = {"observed_at_source": "transcript_assistant_v1", "session_id": "s1"}


def payload(five=62, week=41, **extra):
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": FIVE_RESET}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": WEEK_RESET}
    return {"session_id": "s1", "model": {"id": "claude-x"}, "rate_limits": limits, **extra}


def raw(value):
    return value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")


def assistant_entry(observed):
    timestamp = datetime.fromtimestamp(observed, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"type": "assistant", "timestamp": timestamp}


def tree(root):
    """Every entry under `root` with its size and modification time, folders included (a temporary file that came
    and went still moves its folder's mtime)."""

    found = {}
    for folder, names, files in os.walk(root):
        for name in [*names, *files, ""]:
            path = os.path.join(folder, name) if name else folder
            info = os.lstat(path)
            found[os.path.relpath(path, root)] = (info.st_size if name in files else None, info.st_mtime_ns)
    return found


class TapBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.agt = self.root / "agt"
        self.user = self.root / "user"
        self.profile = self.user / ".claude-work"
        for folder in (self.agt, self.profile):
            folder.mkdir(parents=True)
        env = patch.dict(os.environ, {"AGT_HOME": str(self.agt), "HOME": str(self.user), "USERPROFILE": str(self.user),
                                      "CLAUDE_CONFIG_DIR": str(self.profile)})
        env.start()
        self.addCleanup(env.stop)
        self.login = self.login_as("uuid-work")
        self.login_as("uuid-default", self.user / ".claude")

    def login_as(self, account, folder=None):
        """Log `folder` (the profile) in to `account`: a fake `.claude.json`, `~/.claude.json` for `~/.claude`.
        Returns the login hash `profiles.account_info` gives it."""

        folder = folder or self.profile
        path = self.user / ".claude.json" if folder == self.user / ".claude" else folder / ".claude.json"
        path.write_text(json.dumps({"numStartups": 3, "userID": "not-a-login", "oauthAccount": {
            "accountUuid": account, "organizationUuid": "org-1", "organizationType": "claude_max",
            "emailAddress": "person@example.com"}}))
        return profiles.account_info(folder)["login"]

    def live(self, folder=None, home=None):
        return usage_tap.live_path(home or self.agt, accounts._home_key(folder or self.profile))

    def stored(self, folder=None, home=None):
        return json.loads(self.live(folder, home).read_text(encoding="utf-8"))

    def transcript(self, observed=NOW, *, entries=None):
        """A real JSONL reply at `observed`, or the supplied entries, in this test's temporary folder."""

        count = getattr(self, "transcript_count", 0) + 1
        self.transcript_count = count
        path = self.root / f"session-{count}.jsonl"
        path.write_bytes(b"".join(raw(entry) + b"\n" for entry in
                                 ([assistant_entry(observed)] if entries is None else entries)))
        return path

    def with_transcript(self, value, observed=None):
        """Give a payload its last reply, keeping explicitly supplied paths (including invalid ones)."""

        if isinstance(value, bytes):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        if isinstance(value, dict) and "transcript_path" not in value:
            value = {**value, "transcript_path": str(self.transcript(time.time() if observed is None else observed))}
        return value

    def tap(self, value, now=NOW):
        return usage_tap.tap(raw(self.with_transcript(value, now)), now)

    def run_main(self, value):
        """The tap as Claude Code's wrapper runs it in-process: returns (exit code, stdout + stderr)."""

        out, err = io.StringIO(), io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(raw(self.with_transcript(value))))
        with patch("sys.stdin", stdin), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = usage_tap.main([])
        return code, out.getvalue() + err.getvalue()

    def run_isolated(self, value, env=None):
        """The tap as the launcher runs it: `python -I usage_tap.py`, the rest of agt out of reach."""

        return subprocess.run([sys.executable, "-I", str(TAP)], input=raw(self.with_transcript(value)),
                              capture_output=True, timeout=60,
                              env={**os.environ, **(env or {})})


class RecordingTests(TapBase):
    def test_used_percentage_becomes_utilization_with_its_reset_and_observation(self):
        self.assertTrue(self.tap(payload()))
        self.assertEqual(self.stored(), {"login": self.login, "limits": True, "windows": {
            "five_hour": {"utilization": 0.62, "resets_at": FIVE_RESET, "observed_at": NOW, **LIVE_METADATA},
            "seven_day": {"utilization": 0.41, "resets_at": WEEK_RESET, "observed_at": NOW, **LIVE_METADATA}}})

    def test_no_rate_limits_is_no_news(self):
        # Absent (an API-key login, or before the first reply), empty (every window just reset), null, or only a
        # gateway spend limit: never 0%, never a file.
        for value in ({"session_id": "s1"}, {"rate_limits": {}}, {"rate_limits": None},
                      {"rate_limits": {"spend_limit": {"used_percentage": 10, "resets_at": FIVE_RESET}}}):
            with self.subTest(value=value):
                self.assertFalse(self.tap(value))
                self.assertEqual(os.listdir(self.agt), [])

    def test_observed_at_is_the_last_reply_even_when_the_transcript_was_touched_now(self):
        expected = NOW - 4 * DAY
        transcript = self.transcript(expected)
        for mtime in (NOW, NOW + 1000):
            with self.subTest(mtime=mtime):
                os.utime(transcript, (mtime, mtime))
                self.live().unlink(missing_ok=True)
                self.assertEqual(usage_tap.observed_at({"transcript_path": str(transcript)}, NOW), expected)
                self.assertTrue(self.tap(payload(transcript_path=str(transcript))))
                self.assertEqual(self.stored()["windows"]["five_hour"]["observed_at"], expected)

    def test_a_payload_with_one_window_keeps_the_stored_other(self):
        self.tap(payload())
        self.assertTrue(self.tap(payload(five=70, week=None), now=NOW + 60))
        windows = self.stored()["windows"]
        self.assertEqual(windows["five_hour"], {"utilization": 0.7, "resets_at": FIVE_RESET,
                                               "observed_at": NOW + 60, **LIVE_METADATA})
        self.assertEqual(windows["seven_day"], {"utilization": 0.41, "resets_at": WEEK_RESET,
                                               "observed_at": NOW, **LIVE_METADATA})

    def test_keys_it_does_not_know_are_kept(self):
        # For the same login; another login's file is replaced whole (LoginTests).
        self.live().parent.mkdir(parents=True)
        self.live().write_text(json.dumps({"windows": {}, "login": self.login, "limits": True, "note": "kept"}))
        self.tap(payload())
        self.assertEqual({key: value for key, value in self.stored().items() if key != "windows"},
                         {"login": self.login, "limits": True, "note": "kept"})

    def test_a_corrupt_live_file_is_replaced(self):
        self.live().parent.mkdir(parents=True)
        for text in ("{not json", "[1, 2]", '{"windows": [1]}'):
            with self.subTest(text=text):
                self.live().write_text(text)
                self.assertTrue(self.tap(payload()))
                self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.62)

    def test_an_older_reading_never_replaces_a_newer_one(self):
        self.tap(payload(five=50), now=NOW)
        self.assertFalse(self.tap(payload(five=80), now=NOW - 600))  # a session whose last reply came earlier
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.5)

    def test_a_newer_reply_with_an_earlier_reset_wins_in_both_arrival_orders(self):
        stale = payload(five=None, week=90, transcript_path=str(self.transcript(NOW - 4 * DAY)))
        stale["rate_limits"]["seven_day"]["resets_at"] = WEEK_RESET + DAY
        current = payload(five=None, week=20, transcript_path=str(self.transcript(NOW - 60)))
        expected = {"seven_day": {"utilization": 0.2, "resets_at": WEEK_RESET, "observed_at": NOW - 60, **LIVE_METADATA}}
        for first, second in itertools.permutations((stale, current)):
            with self.subTest(first=first):
                self.live().unlink(missing_ok=True)
                self.assertTrue(self.tap(first, now=NOW))
                before = self.live().read_bytes()
                self.assertEqual(self.tap(second, now=NOW + 1), second is current)
                self.assertEqual(self.stored()["windows"], expected)
                if second is stale:
                    self.assertEqual(self.live().read_bytes(), before)

    def test_a_new_window_after_a_reset_replaces_the_old_one(self):
        self.tap(payload(five=90), now=NOW)
        later = FIVE_RESET + 60
        self.assertTrue(self.tap({"rate_limits": {"five_hour": {"used_percentage": 3, "resets_at": later + 5 * HOUR}}},
                                 now=later))
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.03)


class ProvenanceTests(TapBase):
    def test_written_windows_identify_the_transcript_observation_and_bounded_metadata(self):
        for session, version in (("session-1", "2.1.80"), ("s" * 128, "v" * 64), (" ~", " ~"), ("", "")):
            with self.subTest(session=session, version=version):
                self.live().unlink(missing_ok=True)
                self.assertTrue(self.tap(payload(session_id=session, version=version)))
                for window in self.stored()["windows"].values():
                    self.assertEqual(window.get("observed_at_source"), "transcript_assistant_v1")
                    self.assertEqual(window["session_id"], session)
                    self.assertEqual(window["claude_version"], version)
                    self.assertEqual(set(window), {"utilization", "resets_at", "observed_at",
                                                   "observed_at_source", "session_id", "claude_version"})

    def test_invalid_metadata_is_omitted_without_dropping_the_window_or_valid_other_field(self):
        for field, stored_key, bound, other_field, other_key in (
                ("session_id", "session_id", 128, "version", "claude_version"),
                ("version", "claude_version", 64, "session_id", "session_id")):
            for value in ("x" * (bound + 1), "bad\nvalue", "bad\x7fvalue", "caf\u00e9", 7, True, None, []):
                with self.subTest(field=field, value=value):
                    self.live().unlink(missing_ok=True)
                    self.assertTrue(self.tap(payload(**{field: value, other_field: "valid"})))
                    for window in self.stored()["windows"].values():
                        self.assertNotIn(stored_key, window)
                        self.assertEqual(window.get(other_key), "valid")
                        self.assertEqual(window["observed_at_source"], usage_tap.OBSERVED_AT_SOURCE)

    def test_metadata_only_changes_keep_the_file_bytes_and_state_untouched(self):
        self.assertTrue(self.tap(payload(session_id="s1", version="2.1.80")))
        for window in self.stored()["windows"].values():
            self.assertEqual({key: window.get(key) for key in (*LIVE_METADATA, "claude_version")},
                             {**LIVE_METADATA, "claude_version": "2.1.80"})
        before, state = self.live().read_bytes(), tree(self.agt)
        for metadata in ({"session_id": "s2", "version": "2.1.81"},
                         {"session_id": None, "version": None}):
            with self.subTest(metadata=metadata), patch.object(usage_tap, "read_login") as read:
                self.assertFalse(self.tap(payload(**metadata)))
                read.assert_not_called()
                self.assertEqual(self.live().read_bytes(), before)
                self.assertEqual(tree(self.agt), state)

    def test_metadata_and_marker_alone_are_not_news_for_equal_figures_and_observation(self):
        window = usage_tap.windows_from(payload(version="2.1.80"), NOW)["five_hour"]
        self.assertEqual({key: window.get(key) for key in (*LIVE_METADATA, "claude_version")},
                         {**LIVE_METADATA, "claude_version": "2.1.80"})
        for metadata in ({"session_id": "s2"}, {"claude_version": "2.1.81"},
                         {"observed_at_source": "another-source"}):
            other = {**window, **metadata}
            with self.subTest(metadata=metadata):
                self.assertFalse(usage_tap._news(window, other, NOW))
                self.assertFalse(usage_tap._news(other, window, NOW))

    def test_a_verified_reply_replaces_newer_legacy_windows_and_drops_other_legacy_names(self):
        legacy = {"utilization": 0.62, "resets_at": FIVE_RESET, "observed_at": NOW + HOUR}
        for marker in (None, "unverified"):
            with self.subTest(marker=marker):
                old = {**legacy, **({"observed_at_source": marker} if marker is not None else {})}
                stored = {"login": self.login, "limits": True,
                          "windows": {"five_hour": old, "seven_day": old, "old_model": old}}
                self.live().parent.mkdir(parents=True, exist_ok=True)
                self.live().write_text(json.dumps(stored))
                incoming = usage_tap.windows_from(payload(week=None), NOW)
                self.assertTrue(usage_tap.might_write(stored, incoming, NOW))
                self.assertEqual(usage_tap.due(stored, incoming, NOW, self.login), incoming)
                self.assertTrue(self.tap(payload(week=None)))
                self.assertEqual(self.stored(), {"login": self.login, "limits": True, "windows": incoming})
                self.assertNotIn("login_since", self.stored())  # migration is not a login switch

    def test_legacy_windows_still_supply_limits_history_and_login_switch_safeguards(self):
        legacy = {"five_hour": {"utilization": 0.62, "resets_at": FIVE_RESET, "observed_at": NOW + HOUR}}
        self.live().parent.mkdir(parents=True)
        for limits in ({"limits": True}, {}):
            with self.subTest(limits=limits):
                self.live().write_text(json.dumps({"login": self.login, "windows": legacy, **limits}))
                before = self.live().read_bytes()
                self.assertFalse(self.tap(NO_LIMITS))
                self.assertEqual(self.live().read_bytes(), before)
        login_b = self.login_as("uuid-b")
        # Equal figures, despite a verified observation, still belong to the superseded login.
        self.assertTrue(self.tap(payload(week=None), now=NOW + 60))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60, "windows": {},
                                         "superseded": {"five_hour": A_SUPERSEDED["five_hour"]}})
        self.assertFalse(self.tap(payload(60, None), now=NOW + 120))
        before = self.live().read_bytes()
        delayed = payload(96, None, transcript_path=str(self.transcript(NOW + 20)))
        self.assertFalse(self.tap(delayed, now=NOW + 120))  # login_since is still enforced
        self.assertEqual(self.live().read_bytes(), before)


class ObservationTests(TapBase):
    def assert_no_observation(self, value):
        self.assertIsNone(usage_tap.observed_at(value, NOW))
        before = tree(self.agt)
        with patch.object(usage_tap, "windows_from", wraps=usage_tap.windows_from) as windows:
            self.assertFalse(usage_tap.tap(raw(value), NOW))
        windows.assert_not_called()
        self.assertEqual(tree(self.agt), before)

    def test_missing_or_invalid_transcript_paths_write_nothing(self):
        self.assert_no_observation(payload())
        for path in (str(self.root / "gone.jsonl"), "", None, 7, [], "bad\x00path"):
            with self.subTest(path=path):
                self.assert_no_observation(payload(transcript_path=path))

    def test_empty_and_startup_transcripts_with_current_mtimes_write_nothing(self):
        startup = [{"type": kind, "timestamp": assistant_entry(NOW - DAY)["timestamp"]}
                   for kind in ("attachment", "user", "system", "api-request")]
        for entries in ([], startup):
            with self.subTest(entries=entries):
                transcript = self.transcript(entries=entries)
                os.utime(transcript, (NOW, NOW))
                self.assert_no_observation(payload(transcript_path=str(transcript)))
                self.assertFalse((self.agt / "state").exists())

    def test_payloads_before_a_transcript_and_before_its_first_reply_preserve_newer_live_bytes(self):
        self.assertTrue(self.tap(payload(), now=NOW + 60))
        before = self.live().read_bytes()
        before_tree = tree(self.agt)
        transcript = self.root / "starting-session.jsonl"
        value = payload(five=90, week=80, transcript_path=str(transcript))
        self.assertFalse(self.tap(value, now=NOW + 120))
        self.assertEqual(self.live().read_bytes(), before)
        self.assertEqual(tree(self.agt), before_tree)
        transcript.write_bytes(raw({"type": "system", "timestamp": assistant_entry(NOW - DAY)["timestamp"]}) + b"\n")
        os.utime(transcript, (NOW + 180, NOW + 180))
        self.assertFalse(self.tap(value, now=NOW + 180))
        self.assertEqual(self.live().read_bytes(), before)
        self.assertEqual(tree(self.agt), before_tree)

    def test_unreadable_or_nonregular_transcripts_write_nothing(self):
        self.assert_no_observation(payload(transcript_path=str(self.root)))
        transcript = self.transcript()
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            transcript.chmod(0)
            try:
                self.assert_no_observation(payload(transcript_path=str(transcript)))
            finally:
                transcript.chmod(0o600)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "pipe.jsonl"
            os.mkfifo(fifo)
            done = self.run_isolated(payload(transcript_path=str(fifo)))
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
            self.assertEqual(os.listdir(self.agt), [])

    def test_requests_users_and_system_entries_are_not_replies(self):
        transcript = self.transcript(entries=[{"type": kind, "timestamp": assistant_entry(NOW)["timestamp"],
                                              "message": {"type": "assistant"}}
                                             for kind in ("api-request", "user", "system", "summary")])
        self.assert_no_observation(payload(transcript_path=str(transcript)))

    def test_no_assistant_within_the_scan_bound_writes_nothing(self):
        transcript = self.transcript(NOW - 4 * DAY)
        other = raw({"type": "api-request", "padding": "x" * 1024}) + b"\n"
        with transcript.open("ab") as handle:
            handle.write(other * (TRANSCRIPT_SCAN // len(other) + 2))
        self.assert_no_observation(payload(transcript_path=str(transcript)))

    def test_a_reply_in_a_large_transcripts_tail_is_used(self):
        expected = NOW - HOUR
        transcript = self.transcript(NOW - DAY)
        with transcript.open("ab") as handle:
            handle.write(raw({"type": "system", "padding": "x" * TRANSCRIPT_SCAN}) + b"\n")
            handle.write(raw(assistant_entry(expected)) + b"\n")
            handle.write(raw({"type": "api-request", "timestamp": assistant_entry(NOW)["timestamp"]}) + b"\n")
        self.assertTrue(self.tap(payload(transcript_path=str(transcript))))
        self.assertEqual(self.stored()["windows"]["five_hour"]["observed_at"], expected)

    def test_a_complete_reply_exactly_at_the_scan_boundary_is_used(self):
        expected = NOW - HOUR
        reply = raw(assistant_entry(expected)) + b"\n"
        overhead = len(raw({"type": "system", "padding": ""})) + 1
        rest = raw({"type": "system", "padding": "x" * (TRANSCRIPT_SCAN - len(reply) - overhead)}) + b"\n"
        transcript = self.transcript(entries=[])
        transcript.write_bytes(raw({"type": "system"}) + b"\n" + reply + rest)
        self.assertGreater(transcript.stat().st_size, TRANSCRIPT_SCAN)
        self.assertEqual(len(reply + rest), TRANSCRIPT_SCAN)
        self.assertEqual(usage_tap.observed_at({"transcript_path": str(transcript)}, NOW), expected)

    def test_the_newest_reply_straddling_chunk_boundaries_is_reassembled(self):
        chunk = 1 << 16
        overhead = len(raw({"type": "system", "padding": ""})) + 1
        suffix = raw({"type": "system", "padding": "x" * (chunk - 32 - overhead)}) + b"\n"
        for padding in (0, 2 * chunk):
            with self.subTest(padding=padding):
                expected = NOW - HOUR
                transcript = self.transcript(NOW - DAY)
                reply = raw({**assistant_entry(expected), "padding": "x" * padding}) + b"\n"
                with transcript.open("ab") as handle:
                    handle.write(reply + suffix)
                self.assertLess(len(suffix), chunk)
                self.assertGreater(len(reply + suffix), chunk)
                self.assertEqual(usage_tap.observed_at({"transcript_path": str(transcript)}, NOW), expected)

    def test_a_reply_at_the_end_reads_at_most_two_chunks(self):
        transcript = self.transcript(entries=[{"type": "system", "padding": "x" * TRANSCRIPT_SCAN},
                                              assistant_entry(NOW - HOUR)])
        reads = []
        fdopen = os.fdopen

        def counted_open(*args, **kwargs):
            handle = fdopen(*args, **kwargs)
            read = handle.read

            def counted_read(size=-1):
                data = read(size)
                reads.append(len(data))
                return data

            counter = patch.object(handle, "read", side_effect=counted_read)
            counter.start()
            self.addCleanup(counter.stop)
            return handle

        with patch.object(usage_tap.os, "fdopen", side_effect=counted_open):
            self.assertEqual(usage_tap.observed_at({"transcript_path": str(transcript)}, NOW), NOW - HOUR)
        self.assertTrue(reads)
        self.assertLessEqual(sum(reads), 2 * (1 << 16))

    def test_payloads_without_usable_limits_never_read_the_transcript_or_login(self):
        for value in ({}, {"session_id": "startup"}, payload(None, None),
                      payload(rate_limits=None), payload(rate_limits=[]),
                      payload(rate_limits={"five_hour": {"used_percentage": "bad"}})):
            with self.subTest(value=value), \
                    patch.object(usage_tap, "observed_at", side_effect=AssertionError("opened transcript")), \
                    patch.object(usage_tap, "read_login", side_effect=AssertionError("read login")):
                self.assertFalse(usage_tap.tap(raw(value), NOW))

    def test_the_first_boundary_fragment_is_skipped_even_if_it_looks_like_a_reply(self):
        fragment = raw(assistant_entry(NOW)) + b"\n"
        overhead = len(raw({"type": "system", "padding": ""})) + 1
        rest = raw({"type": "system", "padding": "x" * (TRANSCRIPT_SCAN - len(fragment) - overhead)}) + b"\n"
        # A partial line at the scan boundary can itself be valid JSON; it still isn't a complete entry.
        tail = fragment + rest
        self.assertEqual(len(tail), TRANSCRIPT_SCAN)
        transcript = self.transcript(entries=[])
        transcript.write_bytes(b"prefix of an unfinished line " + tail)
        self.assert_no_observation(payload(transcript_path=str(transcript)))

    def test_the_newest_assistant_with_an_invalid_or_future_timestamp_blocks_older_replies(self):
        for timestamp in (assistant_entry(NOW + 1)["timestamp"], "not a timestamp", "2026-99-99T00:00:00Z",
                          "2026-01-01T00:00:00", "", None, 7):
            with self.subTest(timestamp=timestamp):
                transcript = self.transcript(entries=[assistant_entry(NOW - HOUR),
                                                     {"type": "assistant", "timestamp": timestamp}])
                self.assert_no_observation(payload(transcript_path=str(transcript)))
        transcript = self.transcript(entries=[assistant_entry(NOW - HOUR), {"type": "assistant"}])
        self.assert_no_observation(payload(transcript_path=str(transcript)))

    def test_half_written_last_lines_are_skipped(self):
        for ending in (raw(assistant_entry(NOW)), b'{"type":"assistant","timestamp":',
                       b'{"type":"assistant", broken JSON}\n'):
            with self.subTest(ending=ending):
                transcript = self.transcript(NOW - HOUR)
                with transcript.open("ab") as handle:
                    handle.write(ending)
                self.live().unlink(missing_ok=True)
                self.assertTrue(self.tap(payload(transcript_path=str(transcript))))
                self.assertEqual(self.stored()["windows"]["five_hour"]["observed_at"], NOW - HOUR)

    def test_malformed_middle_lines_are_skipped_and_the_last_complete_reply_wins(self):
        expected = NOW - 123.125
        transcript = self.transcript(entries=[assistant_entry(NOW - HOUR), assistant_entry(expected)])
        with transcript.open("ab") as handle:
            handle.write(b'{"type":"assistant", broken JSON}\n')
            handle.write(b'\xff"assistant"\n')
            handle.write(raw({"type": "api-request", "timestamp": assistant_entry(NOW)["timestamp"]}) + b"\n")
        self.assertTrue(self.tap(payload(transcript_path=str(transcript))))
        self.assertEqual(self.stored()["windows"]["seven_day"]["observed_at"], expected)

    def test_reply_order_comes_from_entries_rather_than_the_greatest_timestamp(self):
        transcript = self.transcript(entries=[assistant_entry(NOW - 1), assistant_entry(NOW - HOUR)])
        self.assertEqual(usage_tap.observed_at({"transcript_path": str(transcript)}, NOW), NOW - HOUR)

    def test_idle_statusline_rerenders_never_refresh_the_observation(self):
        transcript = self.transcript(NOW)
        value = payload(transcript_path=str(transcript))
        self.assertTrue(self.tap(value))
        before = tree(self.agt)
        os.utime(transcript, (NOW + DAY, NOW + DAY))
        self.assertFalse(self.tap(value, now=NOW + DAY))
        self.assertEqual(tree(self.agt), before)
        self.assertEqual(self.stored()["windows"]["seven_day"]["observed_at"], NOW)

    def test_the_first_complete_reply_sets_the_timestamp_and_later_rerenders_keep_it(self):
        expected = NOW - 123.125
        transcript = self.transcript(entries=[{"type": "attachment", "timestamp": assistant_entry(NOW - DAY)["timestamp"]}])
        value = payload(transcript_path=str(transcript))
        os.utime(transcript, (NOW, NOW))
        self.assertFalse(self.tap(value))
        self.assertFalse((self.agt / "state").exists())
        with transcript.open("ab") as handle:
            handle.write(raw(assistant_entry(expected)))  # the first reply isn't complete yet
        os.utime(transcript, (NOW, NOW))
        self.assertFalse(self.tap(value))
        self.assertFalse((self.agt / "state").exists())
        with transcript.open("ab") as handle:
            handle.write(b"\n")
        os.utime(transcript, (NOW, NOW))
        self.assertTrue(self.tap(value))
        self.assertEqual({name: window["observed_at"] for name, window in self.stored()["windows"].items()},
                         {"five_hour": expected, "seven_day": expected})
        before = self.live().read_bytes()
        before_tree = tree(self.agt)
        for later in (NOW + usage_tap.REFRESH_SECONDS, NOW + DAY):
            with self.subTest(later=later):
                os.utime(transcript, (later, later))
                self.assertFalse(self.tap(value, now=later))
                self.assertEqual(self.live().read_bytes(), before)
                self.assertEqual(tree(self.agt), before_tree)

    def test_no_limits_without_an_observation_never_reads_the_login_or_creates_state(self):
        paths = [self.root / "missing.jsonl", self.transcript(entries=[]),
                 self.transcript(entries=[{"type": "api-request", "timestamp": assistant_entry(NOW - DAY)["timestamp"]}]),
                 self.transcript(entries=[{"type": "assistant", "timestamp": "bad timestamp"}]),
                 self.transcript(NOW + 1)]
        values = [NO_LIMITS, *({**NO_LIMITS, "transcript_path": str(path)} for path in paths)]
        with patch.object(usage_tap, "read_login", side_effect=AssertionError("no observation must not read .claude.json")) as read:
            for value in values:
                with self.subTest(value=value):
                    self.assert_no_observation(value)
                    self.assertFalse((self.agt / "state").exists())
                    self.assertEqual(os.listdir(self.agt), [])
            read.assert_not_called()

    def test_no_limits_needs_an_observation_too(self):
        self.assert_no_observation(NO_LIMITS)
        transcript = self.transcript(entries=[{"type": "api-request"}])
        self.assert_no_observation({**NO_LIMITS, "transcript_path": str(transcript)})
        self.assertTrue(self.tap({**NO_LIMITS, "transcript_path": str(self.transcript(NOW - HOUR))}))
        self.assertEqual(self.stored(), {"login": self.login, "limits": False, "windows": {}})
        before = tree(self.agt)
        self.login_as("another-login")
        self.assert_no_observation(NO_LIMITS)
        self.assertEqual(tree(self.agt), before)


class WriteRuleTests(TapBase):
    def test_an_unchanged_payload_within_five_minutes_touches_nothing_under_state(self):
        observed = NOW - 600
        self.tap(payload(transcript_path=str(self.transcript(observed))), now=NOW)
        before = tree(self.agt)
        for advance in (1, usage_tap.REFRESH_SECONDS - 1):
            with self.subTest(advance=advance):
                value = payload(transcript_path=str(self.transcript(observed + advance)))
                self.assertFalse(self.tap(value, now=NOW + 600 + advance))
                self.assertEqual(tree(self.agt), before)
        value = payload(transcript_path=str(self.transcript(observed + usage_tap.REFRESH_SECONDS)))
        self.assertTrue(self.tap(value, now=NOW + 900))
        self.assertEqual({window["observed_at"] for window in self.stored()["windows"].values()},
                         {observed + usage_tap.REFRESH_SECONDS})

    def test_the_no_change_path_never_creates_the_lock_file(self):
        self.live().parent.mkdir(parents=True)
        self.live().write_text(json.dumps({"windows": {
            "five_hour": {"utilization": 0.62, "resets_at": FIVE_RESET, "observed_at": NOW, **LIVE_METADATA},
            "seven_day": {"utilization": 0.41, "resets_at": WEEK_RESET, "observed_at": NOW, **LIVE_METADATA}}}))
        set_times(self.live(), NOW)
        set_times(self.live().parent, NOW)
        before = tree(self.agt)
        self.assertFalse(self.tap(payload(), now=NOW + 10))
        self.assertEqual(tree(self.agt), before)
        self.assertFalse(usage_tap.lock_path(self.live()).exists())

    def test_a_reset_time_within_the_same_minute_is_no_change(self):
        self.tap(payload(), now=NOW)
        before = tree(self.agt)
        jittered = payload()
        jittered["rate_limits"]["five_hour"]["resets_at"] = FIVE_RESET + 20
        self.assertFalse(self.tap(jittered, now=NOW + 30))
        self.assertEqual(tree(self.agt), before)

    def test_it_writes_when_a_figure_changes_or_five_minutes_pass(self):
        self.tap(payload(), now=NOW)
        self.assertTrue(self.tap(payload(five=63), now=NOW + 1))
        self.assertEqual(self.stored()["windows"]["five_hour"]["observed_at"], NOW + 1)
        self.assertEqual(self.stored()["windows"]["seven_day"]["observed_at"], NOW)  # unchanged, not restamped
        self.assertTrue(self.tap(payload(five=63), now=NOW + usage_tap.REFRESH_SECONDS))
        self.assertEqual(self.stored()["windows"]["seven_day"]["observed_at"], NOW + usage_tap.REFRESH_SECONDS)

    @unittest.skipIf(usage_tap.fcntl is None, "the tap writes only where flock exists")
    def test_a_held_lock_makes_the_tap_give_up_and_write_nothing(self):
        import fcntl

        lock = usage_tap.lock_path(self.live())
        lock.parent.mkdir(parents=True)
        with lock.open("a+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            done = self.run_isolated(payload())  # returns once the tap has exited
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
            self.assertFalse(self.live().exists())
        done = self.run_isolated(payload())
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.62)

    def test_two_sessions_finishing_in_reverse_order_keep_each_newest_window(self):
        # Session A read five_hour last; session B read seven_day last. A decides to write, then B finishes first.
        a = {"five_hour": {"utilization": 0.7, "resets_at": FIVE_RESET, "observed_at": NOW, **LIVE_METADATA},
             "seven_day": {"utilization": 0.4, "resets_at": WEEK_RESET, "observed_at": NOW - 900, **LIVE_METADATA}}
        b = {"five_hour": {"utilization": 0.6, "resets_at": FIVE_RESET, "observed_at": NOW - 900, **LIVE_METADATA},
             "seven_day": {"utilization": 0.45, "resets_at": WEEK_RESET, "observed_at": NOW, **LIVE_METADATA}}
        live, login = self.live(), self.login
        self.assertEqual(usage_tap.due(usage_tap.read_live(live), a, NOW, login), a)  # A's check, before any lock
        self.assertTrue(usage_tap.record(live, b, NOW, login))  # B runs start to finish meanwhile
        self.assertTrue(usage_tap.commit(live, a, NOW, login))  # A writes: it re-reads under the lock
        self.assertEqual(self.stored()["windows"], {"five_hour": a["five_hour"], "seven_day": b["seven_day"]})
        for first, second in ((a, b), (b, a)):
            live.unlink()
            usage_tap.record(live, first, NOW, login)
            usage_tap.record(live, second, NOW, login)
            self.assertEqual(self.stored()["windows"], {"five_hour": a["five_hour"], "seven_day": b["seven_day"]})


class InputTests(TapBase):
    def test_bad_input_exits_0_silently_and_writes_nothing(self):
        def week(**window):
            return {"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": FIVE_RESET}, "seven_day": window}}

        bad = [b"", b"{not json", b"\xff\xfe", b"null", b"[1]", b'"text"', b"1e400",
               {"rate_limits": []}, {"rate_limits": "x"}, {"rate_limits": {"five_hour": None}},
               {"rate_limits": {"five_hour": [62]}},
               # one bad window spoils the payload, even beside a good one
               week(), week(used_percentage=None, resets_at=WEEK_RESET), week(used_percentage=True, resets_at=WEEK_RESET),
               week(used_percentage="41", resets_at=WEEK_RESET), week(used_percentage=-1, resets_at=WEEK_RESET),
               week(used_percentage=41, resets_at=None), week(used_percentage=41, resets_at=False),
               week(used_percentage=41, resets_at="soon"), week(used_percentage=41, resets_at=-5),
               week(used_percentage=41), week(resets_at=WEEK_RESET),
               b'{"rate_limits": {"five_hour": {"used_percentage": NaN, "resets_at": 1800010800}}}',
               b'{"rate_limits": {"five_hour": {"used_percentage": Infinity, "resets_at": 1800010800}}}',
               b'{"rate_limits": {"five_hour": {"used_percentage": 1e400, "resets_at": 1800010800}}}',
               b'{"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": -Infinity}}}',
               ('{"rate_limits": {"five_hour": {"used_percentage": 1' + "0" * 400 + ', "resets_at": 1}}}').encode()]
        for value in bad:
            with self.subTest(value=value):
                self.assertEqual(self.run_main(value), (0, ""))
                self.assertEqual(os.listdir(self.agt), [])
        done = self.run_isolated(bad[1])
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(os.listdir(self.agt), [])

    def test_a_good_payload_prints_nothing_either(self):
        self.assertEqual(self.run_main(payload()), (0, ""))
        self.assertTrue(self.live().exists())

    def test_an_input_larger_than_a_status_line_is_ignored(self):
        self.assertFalse(usage_tap.tap(raw(payload(pad="x" * usage_tap.MAX_BYTES)), NOW))
        self.assertEqual(os.listdir(self.agt), [])


class FolderTests(TapBase):
    def test_the_key_is_accounts_home_key(self):
        link = self.user / "linked"
        link.symlink_to(self.profile, target_is_directory=True)
        cases = {"a symlinked folder": str(link), "a trailing slash": str(self.profile) + os.sep,
                 "a path from ~": "~/.claude-work"}
        for label, configured in cases.items():
            with self.subTest(label), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": configured}):
                self.assertEqual(usage_tap.home_key(usage_tap.profile_folder()), accounts._home_key(Path(configured)))
                self.assertEqual(usage_tap.home_key(usage_tap.profile_folder()), accounts._home_key(self.profile))
                self.assertTrue(self.tap(payload()))
                self.assertTrue(self.live().exists())
                self.live().unlink()

    def test_with_no_claude_config_dir_it_is_the_default_folder(self):
        default = self.user / ".claude"
        default.mkdir()
        for unset in (None, ""):
            with self.subTest(unset=unset), patch.dict(os.environ):
                os.environ.pop("CLAUDE_CONFIG_DIR")
                if unset is not None:
                    os.environ["CLAUDE_CONFIG_DIR"] = unset
                self.assertTrue(self.tap(payload()))
                self.assertTrue(self.live(Path.home() / ".claude").exists())
                self.live(default).unlink()

    def test_agts_worker_homes_are_skipped(self):
        worker = self.agt / "homes" / "claude" / "w1"
        worker.mkdir(parents=True)
        link = self.user / "worker-link"
        link.symlink_to(worker, target_is_directory=True)
        for folder in (worker, link, self.agt / "homes"):
            with self.subTest(folder=folder), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(folder)}):
                self.assertFalse(self.tap(payload()))
                self.assertFalse((self.agt / "state").exists())
        beside = self.agt / "homes-other"  # a name that only starts like it
        beside.mkdir()
        self.login_as("uuid-beside", beside)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(beside)}):
            self.assertTrue(self.tap(payload()))

    def test_it_writes_under_the_agt_home_it_was_given(self):
        other = self.root / "other-agt"
        with patch.dict(os.environ, {"AGT_HOME": str(other)}):
            self.assertTrue(self.tap(payload()))
        self.assertTrue(self.live(home=other).exists())
        self.assertFalse((self.agt / "state").exists())
        third = self.root / "third-agt"
        stdin = io.TextIOWrapper(io.BytesIO(raw(self.with_transcript(payload()))))
        with patch("sys.stdin", stdin):
            self.assertEqual(entry.main(["--home", str(third), "hook", "statusline"]), 0)
        self.assertTrue(self.live(home=third).exists())


A_SUPERSEDED = {"five_hour": {"utilization": 0.62, "resets_at": FIVE_RESET},  # payload()'s figures, values only
                "seven_day": {"utilization": 0.41, "resets_at": WEEK_RESET}}


def b_reply(five=5, five_reset=FIVE_RESET - HOUR, **windows):
    """Another login's reply: by default only a five-hour window, lower than A's and resetting an hour earlier."""

    limits = {"five_hour": {"used_percentage": five, "resets_at": five_reset}}
    for name, (used, resets) in windows.items():
        limits[name] = {"used_percentage": used, "resets_at": resets}
    return {"session_id": "s1", "rate_limits": limits}


class LoginTests(TapBase):
    """Live figures are bound to the folder's login (S3, S4)."""

    def use(self, configured):
        """Run the next taps for `configured` (a CLAUDE_CONFIG_DIR value; None: unset)."""

        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR")
        if configured is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = configured

    def test_its_login_is_profiles_account_info(self):
        default = self.user / ".claude"
        default.mkdir()
        folders = {}
        for name, content in (("big", {"projects": {"p": {"history": "x" * (2 * usage_tap.MAX_BYTES)}},
                                       "oauthAccount": {"accountUuid": "uuid-big"}}),
                              ("numeric", {"oauthAccount": {"accountUuid": 12345}})):
            folders[name] = self.user / name
            folders[name].mkdir()
            (folders[name] / ".claude.json").write_text(json.dumps(content))
        linked = self.user / "linked-json"
        linked.mkdir()
        (linked / ".claude.json").symlink_to(self.profile / ".claude.json")
        cases = {"a named folder": (str(self.profile), self.profile),
                 "the default folder": (None, default),
                 "the default folder named by CLAUDE_CONFIG_DIR": ("~/.claude", default),
                 "a .claude.json larger than a status line": (str(folders["big"]), folders["big"]),
                 "an account id that isn't a string": (str(folders["numeric"]), folders["numeric"]),
                 "a linked .claude.json": (str(linked), linked)}
        seen = set()
        for label, (configured, folder) in cases.items():
            with self.subTest(label), patch.dict(os.environ):
                os.environ.pop("CLAUDE_CONFIG_DIR")
                if configured is not None:
                    os.environ["CLAUDE_CONFIG_DIR"] = configured
                expected = profiles.account_info(folder)["login"]
                self.assertIsNotNone(expected)
                self.assertEqual(usage_tap.read_login(usage_tap.profile_folder()), expected)
                self.live(folder).unlink(missing_ok=True)
                self.assertTrue(self.tap(payload()))
                self.assertEqual(self.stored(folder)["login"], expected)
                seen.add(expected)
        # The named folders never read ~/.claude.json, and the default folder reads it first.
        self.assertEqual(self.stored(self.profile)["login"], profiles._hash("uuid-work"))
        self.assertEqual(self.stored(default)["login"], profiles._hash("uuid-default"))
        self.assertEqual(len(seen), 4)  # two spellings of the default folder; the linked file is the named one's

    def test_the_default_folder_falls_back_to_its_own_claude_json_like_account_info(self):
        default = self.user / ".claude"
        default.mkdir()
        (default / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-inside"}}))
        self.use(None)
        for outer in ('{"numStartups": 1}', "{not json", None):
            with self.subTest(outer=outer):
                (self.user / ".claude.json").unlink(missing_ok=True)
                if outer is not None:
                    (self.user / ".claude.json").write_text(outer)
                self.assertEqual(profiles.account_info(default)["login"], profiles._hash("uuid-inside"))
                self.assertEqual(usage_tap.read_login(usage_tap.profile_folder()), profiles._hash("uuid-inside"))

    def test_an_unreadable_login_writes_nothing(self):
        claude_json = self.profile / ".claude.json"
        cases = {"no .claude.json (~/.claude.json is another folder's)": None, "not JSON": b"{not json",
                 "not UTF-8": b'\xff\xfe{"oauthAccount": {"accountUuid": "uuid-work"}}', "not an object": b"[1]",
                 "no oauthAccount": {"numStartups": 1}, "a null oauthAccount": {"oauthAccount": None},
                 "an oauthAccount that isn't an object": {"oauthAccount": "uuid-work"},
                 "no accountUuid": {"oauthAccount": {"organizationUuid": "org-1"}},
                 "an empty accountUuid": {"oauthAccount": {"accountUuid": ""}},
                 "a null accountUuid": {"oauthAccount": {"accountUuid": None}}}
        for label, content in cases.items():
            with self.subTest(label):
                claude_json.unlink(missing_ok=True)
                if content is not None:
                    claude_json.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
                self.assertIsNone(profiles.account_info(self.profile)["login"])
                self.assertFalse(self.tap(payload()))
                self.assertFalse(self.tap({"session_id": "s1", "prompt_cache": {"read": 1}}))  # nor `limits: false`
                self.assertEqual(os.listdir(self.agt), [])
        claude_json.unlink()
        claude_json.mkdir()  # not a file
        self.assertFalse(self.tap(payload()))
        self.assertEqual(os.listdir(self.agt), [])
        claude_json.rmdir()
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            claude_json.write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-work"}}))
            claude_json.chmod(0)
            self.addCleanup(claude_json.chmod, 0o600)
            self.assertIsNone(profiles.account_info(self.profile)["login"])
            self.assertFalse(self.tap(payload()))
            self.assertEqual(os.listdir(self.agt), [])
            claude_json.chmod(0o600)
            claude_json.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(claude_json)  # nobody writes it: reading it would block the tap forever
            done = self.run_isolated(payload())  # returns once the tap has exited
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
            self.assertEqual(os.listdir(self.agt), [])

    def test_after_a_login_change_the_old_figures_are_dropped_and_the_new_ones_written(self):
        self.assertTrue(self.tap(payload(), now=NOW))
        login_b = self.login_as("uuid-other")  # /login to another account
        self.assertNotEqual(login_b, self.login)
        before = tree(self.agt)
        with patch.object(usage_tap, "read_login", wraps=usage_tap.read_login) as read:
            # The session's figures are still A's until B's first reply. Within the refresh: no read, no write.
            self.assertFalse(self.tap(payload(), now=NOW + 10))
            self.assertEqual(tree(self.agt), before)
            self.assertEqual(read.call_count, 0)
            # Past it: the login is read, and the file now names B, with no window, and A's figures superseded.
            self.assertTrue(self.tap(payload(), now=NOW + usage_tap.REFRESH_SECONDS + 100))
            self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + usage_tap.REFRESH_SECONDS + 100,
                                             "windows": {}, "superseded": A_SUPERSEDED})
            self.assertEqual(read.call_count, 1)
            before = tree(self.agt)
            self.assertFalse(self.tap(payload(), now=NOW + 2 * usage_tap.REFRESH_SECONDS + 100))
            self.assertEqual(tree(self.agt), before)
            self.assertEqual(read.call_count, 1)  # superseded figures are dropped without reading .claude.json
        # B's first reply ranks below A's figures (an earlier reset, lower) and has no weekly window yet: written,
        # and the file holds B's alone.
        self.assertTrue(self.tap(b_reply(), now=NOW + 1000))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + usage_tap.REFRESH_SECONDS + 100,
                                         "limits": True, "superseded": A_SUPERSEDED, "windows": {
            "five_hour": {"utilization": 0.05, "resets_at": FIVE_RESET - HOUR,
                           "observed_at": NOW + 1000, **LIVE_METADATA}}})
        # B's next replies merge as usual.
        self.assertTrue(self.tap(b_reply(6, seven_day=(12, WEEK_RESET + DAY)), now=NOW + 1100))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + usage_tap.REFRESH_SECONDS + 100,
                                         "limits": True, "superseded": A_SUPERSEDED, "windows": {
            "five_hour": {"utilization": 0.06, "resets_at": FIVE_RESET - HOUR, "observed_at": NOW + 1100, **LIVE_METADATA},
            "seven_day": {"utilization": 0.12, "resets_at": WEEK_RESET + DAY, "observed_at": NOW + 1100, **LIVE_METADATA}}})

    def test_a_stale_session_after_the_switch_never_writes_the_old_logins_figures(self):
        self.assertTrue(self.tap(payload(), now=NOW))  # sessions 1 and 2, both on login A
        login_b = self.login_as("uuid-other")  # session 1 runs /login
        self.assertTrue(self.tap(b_reply(5, seven_day=(12, WEEK_RESET - DAY)), now=NOW + 60))  # session 1, as B
        expected = {"login": login_b, "login_since": NOW + 60, "limits": True, "superseded": A_SUPERSEDED, "windows": {
            "five_hour": {"utilization": 0.05, "resets_at": FIVE_RESET - HOUR, "observed_at": NOW + 60, **LIVE_METADATA},
            "seven_day": {"utilization": 0.12, "resets_at": WEEK_RESET - DAY, "observed_at": NOW + 60, **LIVE_METADATA}}}
        self.assertEqual(self.stored(), expected)
        before = tree(self.agt)
        with patch.object(usage_tap, "read_login", wraps=usage_tap.read_login) as read:
            # Session 2 still shows A's figures, whose later resets would win the total order under B.
            for later in (NOW + 120, NOW + 2000):
                with self.subTest(later=later):
                    self.assertFalse(self.tap(payload(), now=later))
                    self.assertEqual(tree(self.agt), before)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(self.stored(), expected)
        # Session 2 decided to write just before session 1's switch landed: under the lock it re-reads, and drops them.
        stale = usage_tap.windows_from(payload(), NOW + 2000)
        self.assertEqual(usage_tap.due(usage_tap.read_live(self.live()), stale, NOW + 2000, login_b), {})
        self.assertFalse(usage_tap.commit(self.live(), stale, NOW + 2000, login_b))
        self.assertEqual(self.stored(), expected)

    def test_superseded_figures_are_pruned_once_their_window_resets(self):
        self.tap(payload(), now=NOW)
        self.login_as("uuid-other")
        self.tap(b_reply(), now=NOW + 60)
        later = FIVE_RESET + 60  # A's five-hour window has reset; its weekly one hasn't
        self.assertTrue(self.tap(b_reply(9, five_reset=later + 5 * HOUR), now=later))
        self.assertEqual(self.stored()["superseded"], {"seven_day": A_SUPERSEDED["seven_day"]})
        later = WEEK_RESET + 60
        self.assertTrue(self.tap(b_reply(1, five_reset=later + 5 * HOUR), now=later))
        self.assertNotIn("superseded", self.stored())

    def test_a_window_still_holding_the_old_logins_figures_is_left_out(self):
        # B's own five-hour window; the weekly one is still A's window, at or below A's last figure.
        for weekly in (41, 39):
            with self.subTest(weekly=weekly):
                self.live().unlink(missing_ok=True)
                self.login_as("uuid-work")
                self.tap(payload(), now=NOW)
                login_b = self.login_as("uuid-other")
                self.assertTrue(self.tap(b_reply(3, seven_day=(weekly, WEEK_RESET)), now=NOW + 60))
                self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60,
                                                 "limits": True, "superseded": A_SUPERSEDED,
                                                 "windows": {"five_hour": {"utilization": 0.03,
                                                                           "resets_at": FIVE_RESET - HOUR,
                                                                           "observed_at": NOW + 60, **LIVE_METADATA}}})

    def test_another_session_of_the_old_login_never_leaks_its_older_figures(self):
        # Session 2 last replied as A at 60/40; session 1 later at 62/41, which is what the file holds.
        self.assertTrue(self.tap(payload(60, 40), now=NOW))
        self.assertTrue(self.tap(payload(), now=NOW + 600))
        login_b = self.login_as("uuid-other")  # session 1 runs /login, and replies as B
        self.assertTrue(self.tap(b_reply(5, seven_day=(12, WEEK_RESET - DAY)), now=NOW + 700))
        expected = {"login": login_b, "login_since": NOW + 700, "limits": True, "superseded": A_SUPERSEDED, "windows": {
            "five_hour": {"utilization": 0.05, "resets_at": FIVE_RESET - HOUR, "observed_at": NOW + 700, **LIVE_METADATA},
            "seven_day": {"utilization": 0.12, "resets_at": WEEK_RESET - DAY, "observed_at": NOW + 700, **LIVE_METADATA}}}
        self.assertEqual(self.stored(), expected)
        before = tree(self.agt)
        with patch.object(usage_tap, "read_login", wraps=usage_tap.read_login) as read:
            for later in (NOW + 800, NOW + 2000):  # session 2 re-renders its A figures: below A's last, later resets
                with self.subTest(later=later):
                    self.assertFalse(self.tap(payload(60, 40), now=later))
                    self.assertEqual(tree(self.agt), before)
        self.assertEqual(read.call_count, 0)
        stale = usage_tap.windows_from(payload(60, 40), NOW + 2000)  # and under the lock, had it got that far
        self.assertEqual(usage_tap.due(usage_tap.read_live(self.live()), stale, NOW + 2000, login_b), {})
        self.assertFalse(usage_tap.commit(self.live(), stale, NOW + 2000, login_b))
        self.assertEqual(self.stored(), expected)

    def test_a_figure_above_the_old_logins_last_one_is_not_the_old_logins(self):
        self.tap(payload(), now=NOW)
        login_b = self.login_as("uuid-other")
        self.assertTrue(self.tap(payload(63, None), now=NOW + 60))  # the same window minute, but above A's 62%
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60,
                                         "limits": True, "superseded": A_SUPERSEDED, "windows": {
            "five_hour": {"utilization": 0.63, "resets_at": FIVE_RESET, "observed_at": NOW + 60, **LIVE_METADATA}}})

    def test_a_login_change_seen_first_through_another_sessions_older_figures_writes_no_windows(self):
        self.tap(payload(), now=NOW)  # A's last figures: 62/41
        login_b = self.login_as("uuid-other")  # /login in session 1; session 2 (60/40) renders first
        self.assertTrue(self.tap(payload(60, 40), now=NOW + 60))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60,
                                         "windows": {}, "superseded": A_SUPERSEDED})
        self.assertTrue(self.tap(b_reply(), now=NOW + 120))  # B's own first reply
        self.assertEqual(self.stored()["windows"], {
            "five_hour": {"utilization": 0.05, "resets_at": FIVE_RESET - HOUR, "observed_at": NOW + 120, **LIVE_METADATA}})

    def test_a_file_from_before_logins_is_replaced(self):
        self.live().parent.mkdir(parents=True)
        self.live().write_text(json.dumps({"windows": {
            "five_hour": {"utilization": 0.9, "resets_at": FIVE_RESET + HOUR, "observed_at": NOW, **LIVE_METADATA}}}))
        self.assertTrue(self.tap(payload(), now=NOW + 60))
        self.assertEqual(self.stored()["login"], self.login)
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.62)

    def test_a_delayed_higher_figure_from_before_the_switch_never_becomes_bs(self):
        transcript = self.transcript(NOW + 20)
        os.utime(transcript, (NOW + 120, NOW + 120))
        delayed = payload(96, None, transcript_path=str(transcript))
        self.assertTrue(self.tap(payload(80, None), now=NOW))
        login_b = self.login_as("uuid-b")
        self.assertTrue(self.tap(b_reply(), now=NOW + 60))
        before = tree(self.agt)
        self.assertFalse(self.tap(delayed, now=NOW + 120))
        self.assertEqual(tree(self.agt), before)
        self.assertEqual(self.stored()["login"], login_b)
        self.assertEqual(self.stored()["login_since"], NOW + 60)
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.05)
        stale = usage_tap.windows_from(delayed, NOW + 20)
        self.assertEqual(usage_tap.due(self.stored(), stale, NOW + 120, login_b), {})
        self.assertFalse(usage_tap.commit(self.live(), stale, NOW + 120, login_b))
        self.assertEqual(tree(self.agt), before)

    def test_a_stale_a_session_writes_nothing_after_a_to_b_to_c(self):
        transcript = self.transcript(NOW + 20)
        os.utime(transcript, (NOW + 180, NOW + 180))
        self.assertTrue(self.tap(payload(80, None), now=NOW))
        self.login_as("uuid-b")
        self.assertTrue(self.tap(b_reply(), now=NOW + 60))
        login_c = self.login_as("uuid-c")
        self.assertTrue(self.tap(b_reply(7, five_reset=FIVE_RESET - 2 * HOUR), now=NOW + 120))
        before = tree(self.agt)
        delayed = payload(96, None, transcript_path=str(transcript))
        self.assertFalse(self.tap(delayed, now=NOW + 180))
        self.assertEqual(tree(self.agt), before)
        self.assertEqual(self.stored()["login"], login_c)
        self.assertEqual(self.stored()["login_since"], NOW + 120)
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.07)

    def test_a_switch_first_seen_through_a_delayed_higher_figure_records_no_windows(self):
        transcript = self.transcript(NOW + 20)
        os.utime(transcript, (NOW + 60, NOW + 60))
        self.assertTrue(self.tap(payload(80, None), now=NOW))
        login_b = self.login_as("uuid-b")
        self.assertTrue(self.tap(payload(96, None, transcript_path=str(transcript)), now=NOW + 60))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60, "windows": {},
                                         "superseded": {"five_hour": {"utilization": 0.8, "resets_at": FIVE_RESET}}})

    def test_claude_json_is_read_once_per_write_and_never_on_the_unchanged_path(self):
        with patch.object(usage_tap, "_read_regular", wraps=usage_tap._read_regular) as read:
            self.assertTrue(self.tap(payload(), now=NOW))
            self.assertEqual(read.call_count, 1)
            for value, later in ((payload(), NOW + 10), ({"rate_limits": {}}, NOW + 20), ({"session_id": "s1"}, NOW + 30),
                                 (payload(five=None), NOW + 40)):
                with self.subTest(value=value):
                    self.assertFalse(self.tap(value, now=later))
            self.assertEqual(self.run_main(b"{not json"), (0, ""))
            self.assertEqual(read.call_count, 1)
            self.assertTrue(self.tap(payload(five=63), now=NOW + 60))
            self.assertEqual(read.call_count, 2)

    def test_without_a_login_the_writers_write_nothing(self):
        windows = {"five_hour": {"utilization": 0.5, "resets_at": FIVE_RESET, "observed_at": NOW, **LIVE_METADATA}}
        self.assertEqual(usage_tap.due({}, windows, NOW), {})
        self.assertFalse(usage_tap.record(self.live(), windows, NOW))
        self.assertFalse(usage_tap.commit(self.live(), windows, NOW))
        self.assertFalse(usage_tap.record(self.live(), {}, NOW, no_limits=True))
        self.assertEqual(os.listdir(self.agt), [])


NO_LIMITS = {"session_id": "s1", "prompt_cache": {"read_input_tokens": 1200}}  # a reply came back, without limits


class LimitsTests(TapBase):
    """`limits: false` is said only when it's true, and sticks to false only for a login never seen with windows."""

    def test_a_reply_without_rate_limits_writes_limits_false_for_a_login_never_seen_with_windows(self):
        self.assertTrue(self.tap(NO_LIMITS))
        self.assertEqual(self.stored(), {"login": self.login, "limits": False, "windows": {}})
        before = tree(self.agt)
        with patch.object(usage_tap, "read_login", wraps=usage_tap.read_login) as read:
            self.assertFalse(self.tap(NO_LIMITS, now=NOW + 600))  # it says so already: no write
        self.assertEqual(read.call_count, 1)  # the login is checked: a file naming another login is replaced
        self.assertEqual(tree(self.agt), before)
        self.assertTrue(self.tap(payload(), now=NOW + 700))  # the login has limits after all
        self.assertEqual(self.stored()["limits"], True)
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.62)

    def test_a_login_that_has_had_windows_keeps_limits_true(self):
        # A Max login whose windows all reset: Claude Code drops them, so `rate_limits` is absent or empty.
        self.tap(payload(), now=NOW)
        before = tree(self.agt)
        for value in (NO_LIMITS, {**NO_LIMITS, "rate_limits": {}}):
            with self.subTest(value=value):
                self.assertFalse(self.tap(value, now=FIVE_RESET + 60))
                self.assertEqual(tree(self.agt), before)
        self.assertIs(self.stored()["limits"], True)

    def test_limits_false_needs_rate_limits_absent_and_a_reply(self):
        for value in ({"session_id": "s1"}, {"session_id": "s1", "prompt_cache": None},  # before the first reply
                      {**NO_LIMITS, "rate_limits": None}, {**NO_LIMITS, "rate_limits": {}},
                      {**NO_LIMITS, "rate_limits": {"spend_limit": {"used_percentage": 10, "resets_at": FIVE_RESET}}}):
            with self.subTest(value=value):
                self.assertFalse(self.tap(value))
                self.assertEqual(os.listdir(self.agt), [])

    def test_another_login_without_limits_replaces_a_login_with_windows(self):
        self.tap(payload(), now=NOW)
        login_b = self.login_as("uuid-team")
        self.assertTrue(self.tap(NO_LIMITS, now=NOW + 60))
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60,
                                         "limits": False, "windows": {}, "superseded": A_SUPERSEDED})

    def test_the_old_logins_figures_never_give_a_login_without_limits_limits(self):
        self.tap(payload(), now=NOW)
        login_b = self.login_as("uuid-team")
        self.assertTrue(self.tap(NO_LIMITS, now=NOW + 60))
        expected = {"login": login_b, "login_since": NOW + 60,
                    "limits": False, "windows": {}, "superseded": A_SUPERSEDED}
        self.assertEqual(self.stored(), expected)
        before = tree(self.agt)
        for value, later in ((payload(60, 40), NOW + 120), (payload(), NOW + 180), (payload(62, None), NOW + 900)):
            with self.subTest(value=value):  # another session still on A: older, equal, one window
                self.assertFalse(self.tap(value, now=later))
                self.assertEqual(tree(self.agt), before)
        self.assertEqual(self.stored(), expected)

    def test_two_logins_without_limits_one_after_the_other(self):
        self.assertTrue(self.tap(NO_LIMITS, now=NOW))
        self.assertEqual(self.stored(), {"login": self.login, "limits": False, "windows": {}})
        login_b = self.login_as("uuid-team-2")
        self.assertTrue(self.tap(NO_LIMITS, now=NOW + 60))  # the file names the new login, never the earlier one
        self.assertEqual(self.stored(), {"login": login_b, "login_since": NOW + 60, "limits": False, "windows": {}})
        before = tree(self.agt)
        self.assertFalse(self.tap(NO_LIMITS, now=NOW + 120))
        self.assertEqual(tree(self.agt), before)


class ShapeTests(unittest.TestCase):
    def test_it_imports_nothing_from_agents_router(self):
        # Like share/hook_run.py: the launcher runs it with `python -I`, where the package isn't importable.
        names = set()
        for node in ast.walk(ast.parse(TAP.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names.add((node.module or "").split(".")[0])
        self.assertLessEqual(names - {"__future__"}, set(sys.stdlib_module_names))

    def test_its_window_key_is_window_orders(self):
        windows = [{}, {"utilization": 0.5}, {"resets_at": NOW + 60}, {"resets_at": NOW - 60, "utilization": 0.9},
                   {"resets_at": NOW + 30, "observed_at": NOW - 5, "utilization": 0.2},
                   {"resets_at": NOW + 89, "observed_at": NOW - 5, "utilization": 0.2},
                   {"resets_at": NOW, "observed_at": float("nan"), "utilization": True},
                   {"resets_at": "soon", "observed_at": None, "utilization": "x"},
                   {"resets_at": 10 ** 400, "observed_at": float("inf"), "utilization": -1}]
        for window in windows:
            with self.subTest(window=window):
                self.assertEqual(usage_tap.window_key(window, NOW), window_order.window_key(window, NOW))

    def test_its_login_hash_is_profiles(self):
        for value in ("uuid-work", 12345, "", None, 0, {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(usage_tap._hash(value), profiles._hash(value))

    def test_its_home_key_is_accounts(self):
        for folder in (Path("/"), Path("relative/x"), Path("~/.claude"), Path(tempfile.gettempdir())):
            with self.subTest(folder=folder):
                self.assertEqual(usage_tap.home_key(folder), accounts._home_key(folder))


class RouteTests(TapBase):
    def test_hook_statusline_takes_the_fast_route_and_the_full_cli_accepts_it(self):
        self.assertEqual(entry.quick(["hook", "statusline"]), (None, "statusline", []))
        self.assertEqual(entry.quick(["--home", str(self.agt), "hook", "statusline"]), (str(self.agt), "statusline", []))
        with patch.object(usage_tap, "main", return_value=0) as tap, patch.object(hooks, "main") as run_hook:
            self.assertEqual(entry.main(["hook", "statusline"]), 0)
            self.assertEqual(cli.main(["hook", "statusline"]), 0)
        self.assertEqual(tap.call_count, 2)
        run_hook.assert_not_called()

    def test_the_entry_point_loads_nothing_else_from_agt(self):
        probe = ("import json, sys\nsys.argv = ['agt', 'hook', 'statusline']\n"
                 "from agents_router.entry import main\ncode = main()\n"
                 "print(json.dumps([code, sorted(m for m in sys.modules if m.startswith('agents_router'))]), file=sys.stderr)\n")
        done = subprocess.run([sys.executable, "-c", probe], input=raw(self.with_transcript(payload())),
                              capture_output=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(SRC)})
        code, loaded = json.loads(done.stderr.decode().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(loaded, ["agents_router", "agents_router.entry", "agents_router.usage_tap"])
        self.assertEqual(done.stdout, b"")
        self.assertTrue(self.live().exists())


@unittest.skipIf(os.name == "nt", "the launcher is a /bin/sh script")
class LauncherTests(TapBase):
    def write(self, record):
        path = self.agt / "bin" / "agt-run"
        path.parent.mkdir()
        path.write_text(triggers.launcher_text({"home": str(self.agt), "config_dir": str(self.root / "settings"),
                                                "source": "agents-router", **record}))
        return path

    def run_launcher(self, path, value=None):
        return subprocess.run(["/bin/sh", str(path), "hook", "statusline"], input=raw(self.with_transcript(value or payload())),
                              capture_output=True, timeout=60,
                              env={"HOME": str(self.user), "PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(self.profile)})

    def test_the_launcher_runs_the_lean_tap_under_the_agt_home_it_exports(self):
        launcher = self.write({"python": sys.executable, "package": str(SRC)})
        self.assertIn('-I "$PKG/agents_router/usage_tap.py"', launcher.read_text())
        done = self.run_launcher(launcher)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(self.stored()["windows"]["five_hour"]["utilization"], 0.62)

    def test_a_launcher_whose_python_is_gone_stays_silent_and_writes_nothing(self):
        launcher = self.write({"python": str(self.root / "gone" / "python"), "package": str(SRC)})
        before = tree(self.agt)
        done = self.run_launcher(launcher)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(tree(self.agt), before)  # no state/share/launcher.log line per message

    def test_the_app_launcher_reaches_the_tap_through_agts_entry_point(self):
        stable = self.root / "App Support" / "bin" / "agt"
        stable.parent.mkdir(parents=True)
        stable.write_text(f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(SRC))} exec {shlex.quote(sys.executable)} -s -m agents_router \"$@\"\n")
        stable.chmod(0o755)
        record = {"launcher": str(stable), "app_support": str(stable.parent.parent), "bin_dir": str(self.root / "bin")}
        launcher = self.write(record)
        done = self.run_launcher(launcher)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(self.stored()["windows"]["seven_day"]["utilization"], 0.41)
        self.live().unlink()
        stable.unlink()
        before = tree(self.agt)
        done = self.run_launcher(launcher)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        self.assertEqual(tree(self.agt), before)


if __name__ == "__main__":
    unittest.main()
