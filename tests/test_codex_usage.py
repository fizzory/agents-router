import json
from datetime import datetime, timezone
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from helpers import TempHome

from agents_router import accounts, fsutil
from agents_router.runtimes.codex import CodexParser, find_rollout, last_rate_limits, usage_windows

THREAD = "01a141dd-7dbd-73d2-bc93-bd302ca72586"


def limits(used5, used7, reset5=1792106822):
    return {
        "limit_id": "codex",
        "primary": {"used_percent": used5, "window_minutes": 300, "resets_at": reset5},
        "secondary": {"used_percent": used7, "window_minutes": 10080, "resets_at": 1792675611},
    }


class RolloutTests(TempHome):
    def rollout(self, *snapshots, thread=THREAD):
        folder = self.primary / "sessions" / "2026" / "10" / "16"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-16T01-19-57-{thread}.jsonl"
        lines = [json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "private text"}})]
        for snapshot in snapshots:
            lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": snapshot}}))
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "x"}}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_last_snapshot_and_windows(self):
        path = self.rollout(limits(10.0, 5.0), limits(92.0, 27.0))
        self.assertEqual(find_rollout(self.primary, THREAD), path)
        self.assertIsNone(find_rollout(self.primary, "other"))
        found, observed = last_rate_limits(path)
        self.assertEqual(observed, path.stat().st_mtime)
        windows = usage_windows(found)
        self.assertEqual(windows["five_hour"], {"utilization": 0.92, "resets_at": 1792106822.0})
        self.assertAlmostEqual(windows["seven_day"]["utilization"], 0.27)

    def test_parser_records_usage_for_ranking(self):
        self.rollout(limits(92.0, 27.0))
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(parser.finish(0, "").outcome, "success")
        record = accounts.load_usage("codex")["c1"]
        self.assertEqual(record["windows"]["five_hour"]["utilization"], 0.92)
        # Pinned before the fixture's resets: an expired window never counts for ranking.
        self.assertEqual(accounts.max_utilization(record, now=1792100000), 0.92)

    def test_snapshot_uses_its_event_timestamp(self):
        path = self.rollout(limits(95, 0))
        event = {"timestamp": "2026-11-09T04:02:32.342Z", "payload": {"rate_limits": limits(10, 0)}}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        found, observed = last_rate_limits(path)
        self.assertEqual(found, limits(10, 0))
        self.assertEqual(observed, datetime(2026, 11, 9, 4, 2, 32, 342000, timezone.utc).timestamp())

    def test_limit_uses_snapshot_reset_time(self):
        reset = time.time() + 3600
        self.rollout(limits(100.0, 40.0, reset5=reset))
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        result = parser.finish(1, "")
        self.assertEqual(result.outcome, "usage_limit")
        self.assertEqual(result.resets_at, reset)

    def test_missing_rollout_is_harmless(self):
        parser = CodexParser("c1", self.primary)
        parser.feed({"type": "thread.started", "thread_id": THREAD})
        parser.feed({"type": "turn.completed", "usage": {}})
        self.assertEqual(parser.finish(0, "").outcome, "success")
        self.assertNotIn("c1", accounts.load_usage("codex"))


class UsageWriteTests(TempHome):
    def test_repeated_observations_leave_the_usage_file_untouched(self):
        windows = usage_windows(limits(45, 21))
        stored = accounts.update_usage("codex", "c1", windows=windows, observed_at=100)
        path = accounts._usage_path("codex")
        before = fsutil.stat_signature(path), path.read_bytes()
        for stamp in (100, 99):
            with self.subTest(stamp=stamp):
                returned = accounts.update_usage("codex", "c1", windows=windows, observed_at=stamp)
                self.assertEqual((fsutil.stat_signature(path), path.read_bytes()), before)
                self.assertEqual(returned, stored)

        with patch.object(accounts, "write_json", wraps=fsutil.write_json) as write:
            newer = accounts.update_usage("codex", "c1", windows=windows, observed_at=101)
            self.assertEqual(newer["observed_at"], 101)
            self.assertEqual(accounts.load_usage("codex")["c1"], newer)
            write.assert_called_once()
            write.reset_mock()
            reserved = accounts.update_usage("codex", "c1", probed_at=102)
            self.assertEqual(reserved["probed_at"], 102)
            self.assertEqual(accounts.load_usage("codex")["c1"], reserved)
            write.assert_called_once()
            first = accounts.update_usage("codex", "c1", cooldown_until=130)
            second = accounts.update_usage("codex", "c1", cooldown_until=130)
            self.assertNotEqual(first["cooldown_revision"], second["cooldown_revision"])
            self.assertEqual(accounts.load_usage("codex")["c1"], second)

    def test_legacy_observation_timestamps_are_written_once(self):
        for runtime in ("codex", "claude"):
            with self.subTest(runtime=runtime):
                path = accounts._usage_path(runtime)
                fsutil.write_json(path, {"c1": {"windows": usage_windows(limits(45, 21)), "updated": 100}})
                with patch.object(accounts, "write_json", wraps=fsutil.write_json) as write:
                    record = accounts.update_usage(runtime, "c1")
                    if runtime == "codex":
                        self.assertEqual(record["observed_at"], 100)
                    else:
                        self.assertEqual({win["observed_at"] for win in record["windows"].values()}, {100})
                    self.assertEqual(accounts.load_usage(runtime)["c1"], record)
                    self.assertEqual(accounts.update_usage(runtime, "c1"), record)
                    write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
