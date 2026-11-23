"""Popover probes use a free fake app-server independently of local usage reads."""

import contextlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from helpers import TempHome
from generate_codex_probe_fixture import FIXTURE, fixture_text

from agents_router import accounts, cli, usage_view
from agents_router.config import load_config


class CodexPopoverProbeTests(TempHome):
    def setUp(self):
        super().setUp()
        self.codex = self.root / "codex"
        self.codex.mkdir()
        (self.home / "settings.toml").write_text(f'[codex]\nprimary = {json.dumps(str(self.codex))}\n')
        self.now = time.time()
        self.requests = self.root / "requests"
        self.enterContext(patch.dict(os.environ, {
            "AGT_CODEX_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
            "FAKE_CODEX_APP_SERVER": json.dumps({"rateLimits": {"rateLimitReachedType": None, "primary": {
                "usedPercent": 23, "windowDurationMins": 300, "resetsAt": self.now + 3600}}}),
            "FAKE_CODEX_REQUESTS": str(self.requests),
        }))

    def seed(self, age=600, **metadata):
        return accounts.update_usage("codex", "primary", windows={
            "five_hour": {"utilization": .1, "resets_at": self.now + 3600}},
            observed_at=self.now - age, **metadata)

    def command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                code = cli.main(["usage", "--probe-codex", "--json"])
            except SystemExit as error:
                code = error.code
            self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_probes_below_ceiling_at_600_seconds_and_skips_fresh_result(self):
        self.seed()
        with patch("time.time", return_value=self.now):
            self.assertEqual(self.command(), {"schema_version": 1, "probed": ["primary"], "skipped": []})
            self.assertEqual(self.command(), {"schema_version": 1, "probed": [], "skipped": ["primary"]})
        self.assertEqual(accounts.load_usage("codex")["primary"]["windows"]["five_hour"]["utilization"], .23)
        requests = [json.loads(line)["method"] for line in self.requests.read_text().splitlines()]
        self.assertEqual(requests, ["initialize", "initialized", "account/rateLimits/read"])

    def test_newest_window_or_probe_controls_freshness_not_metadata(self):
        for field, age in [("observed_at", 599), ("probed_at", 599)]:
            record = {"updated": self.now, "probed_at": self.now - 800,
                      "windows": {"five_hour": {"observed_at": self.now - 900},
                                  "seven_day": {"observed_at": self.now - 1000}}}
            if field == "observed_at":
                record["windows"]["seven_day"][field] = self.now - age
            else:
                record[field] = self.now - age
            self.assertFalse(usage_view.codex_live_due(record, self.now))
        self.assertTrue(usage_view.codex_live_due({"updated": self.now}, self.now))
        self.seed(age=599)
        with patch("time.time", return_value=self.now):
            self.assertEqual(self.command()["skipped"], ["primary"])
        self.assertFalse(self.requests.exists())

    def test_failed_attempt_is_reserved_and_does_not_repeat(self):
        self.seed()
        os.environ["FAKE_CODEX_APP_SERVER"] = "eof"
        self.assertEqual(self.command()["skipped"], ["primary"])
        self.assertGreaterEqual(accounts.load_usage("codex")["primary"]["probed_at"], self.now)
        os.environ["FAKE_CODEX_APP_SERVER"] = "garbage"
        self.assertEqual(self.command()["skipped"], ["primary"])
        probes = [r for r in self.records() if r["argv"][:1] == ["app-server"]]
        self.assertEqual(len(probes), 1)

    def test_due_rechecked_under_per_account_lock(self):
        self.seed()
        from agents_router.fsutil import file_lock

        original = file_lock

        @contextlib.contextmanager
        def acquired(*args, **kwargs):
            with original(*args, **kwargs):
                accounts.update_usage("codex", "primary", probed_at=self.now)
                yield

        with patch.object(usage_view, "file_lock", acquired), patch("time.time", return_value=self.now):
            self.assertEqual(self.command()["skipped"], ["primary"])
        self.assertFalse(self.requests.exists())

    def test_two_processes_make_one_call_and_read_completes_while_probe_is_blocked(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("POSIX FIFO")
        self.seed()
        gate, ready = self.root / "gate", self.root / "ready"
        os.mkfifo(gate)
        os.mkfifo(ready)
        gate_fd = os.open(gate, os.O_RDWR)
        self.addCleanup(os.close, gate_fd)
        ready_fd = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, ready_fd)
        os.environ.update(FAKE_CODEX_PROBE_GATE=str(gate), FAKE_CODEX_PROBE_ENTERED=str(ready))
        argv = [sys.executable, "-m", "agents_router", "usage", "--probe-codex", "--json"]
        first = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(ready_fd, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=15), "fake reached rateLimits/read")
            self.assertEqual(os.read(ready_fd, 100), b"ready\n")
            second = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["skipped"], ["primary"])
            read = subprocess.run([sys.executable, "-m", "agents_router", "usage", "--json"],
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertIn("providers", json.loads(read.stdout))
            self.assertIsNone(first.poll(), "first probe is still held on FIFO")
            calls = [json.loads(line) for line in self.requests.read_text().splitlines()]
            self.assertEqual(sum(r["method"] == "account/rateLimits/read" for r in calls), 1)
            os.write(gate_fd, b"continue\n")
            stdout, stderr = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["probed"], ["primary"])
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=15)

    def test_fixture_matches_the_real_cli_producer(self):
        self.assertEqual(FIXTURE.read_text(), fixture_text())

    def test_command_waits_for_timed_out_probe_to_kill_its_child(self):
        if os.name == "nt":
            self.skipTest("POSIX process-group cleanup")
        from agents_router.fsutil import worker_process
        from agents_router.runtimes import codex

        self.seed()
        os.environ["FAKE_CODEX_APP_SERVER"] = "hang"
        spawned, release = threading.Event(), threading.Event()
        children = []
        killpg = os.killpg

        @contextlib.contextmanager
        def tracked_process(*args, **kwargs):
            with worker_process(*args, **kwargs) as child:
                children.append(child)
                spawned.set()
                yield child

        def delayed_kill(pid, sig):
            # Widen the real race: the deadline expired, but the probe's finally has not killed its child yet.
            if not release.wait(15):
                raise AssertionError("cleanup grace never joined the probe")
            killpg(pid, sig)

        class CleanupThread(threading.Thread):
            joins = 0

            def start(self):
                super().start()
                if not spawned.wait(15):
                    raise AssertionError("probe child did not start")

            def join(self, timeout=None):
                self.joins += 1
                if self.joins == 2:
                    release.set()  # only the command's cleanup join releases the delayed finally
                super().join(timeout)

        try:
            with patch.object(codex, "worker_process", tracked_process), \
                    patch.object(codex.os, "killpg", delayed_kill), \
                    patch.object(usage_view, "Thread", CleanupThread), \
                    patch.object(usage_view, "CODEX_PROBE_TIMEOUT_SECONDS", 0.3):
                self.assertEqual(self.command()["skipped"], ["primary"])
                self.assertEqual(len(children), 1)
                pid = children[0].pid
                deadline = time.monotonic() + 2
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("probe command returned with a live child")
                    threading.Event().wait(0.01)
        finally:
            release.set()
            for child in children:
                if child.poll() is None:
                    try:
                        killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=15)

    def test_admission_still_only_probes_held_back_accounts(self):
        self.seed()
        config = load_config()
        with patch("agents_router.runtimes.codex.probe_rate_limits") as probe:
            usage_view.refresh_codex(config, live=True)
        probe.assert_not_called()
