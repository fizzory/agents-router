"""The app's restart after an in-app update, half (a): a running job outlives it.

Install and Restart ends the app's process and its process group. A job
started through fsutil.spawn_detached runs in its own session, so that end
never reaches its runner, which finishes the job. Half (b), the relaunched
app's runtime cleanup, is install.rs's
restart_cleanup_keeps_a_live_jobs_pinned_runtime_and_removes_the_rest.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

from test_runner_robustness import HERE, RobustnessBase
from agents_router import jobs
from agents_router.fsutil import process_start_time, same_process
from agents_router.runtimes.base import SUCCESS

# The process that starts the job and is then ended, like the app on restart.
PARENT = """
import json, os, sys, time
from agents_router import jobs
status = jobs.create_job(runtime="codex", prompt="task", cwd=sys.argv[1], account="a1", access="read")
with open(sys.argv[2] + ".tmp", "w", encoding="utf-8") as f:
    json.dump({"job_id": status["job_id"], "runner_pid": status["runner_pid"]}, f)
os.replace(sys.argv[2] + ".tmp", sys.argv[2])
time.sleep(600)
"""


@unittest.skipIf(os.name == "nt", "POSIX sessions and process groups")
class RestartTests(RobustnessBase):
    def stop(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)

    def test_a_running_job_outlives_the_process_group_that_started_it_and_finishes(self):
        gate = self.root / "release"
        started = self.root / "started.json"
        os.environ.update(
            AGT_CODEX_COMMAND=json.dumps([sys.executable, str(HERE / "fake_robust_worker.py")]),
            FAKE_ROBUST_MODE="stream", FAKE_ROBUST_RELEASE=str(gate),
        )
        parent = subprocess.Popen([sys.executable, "-c", PARENT, str(self.project), str(started)],
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.stop, parent)
        self.addCleanup(gate.touch)  # runs first: a failed test never leaves the worker waiting
        self.wait_until(started.exists, timeout=30)
        record = json.loads(started.read_text(encoding="utf-8"))
        job_id, runner = record["job_id"], int(record["runner_pid"])
        stamp = process_start_time(runner)
        events = jobs.job_dir(job_id) / "events.jsonl"
        self.wait_until(lambda: events.exists() and events.stat().st_size > 0, timeout=30)  # the worker is mid-turn

        # The restart: SIGTERM to the starting process's whole group, then wait for it.
        os.killpg(parent.pid, signal.SIGTERM)
        self.assertEqual(parent.wait(timeout=30), -signal.SIGTERM)
        try:
            group = os.getpgid(runner)
        except ProcessLookupError:
            self.fail("the runner ended with the process group that started it")
        self.assertEqual(group, runner, "the runner shares the ended process group")
        self.assertTrue(same_process(runner, stamp))
        self.assertEqual(jobs.read_status(job_id)["state"], "running")

        gate.touch()
        final = jobs.wait_for(job_id, 60)
        self.assertEqual(final["outcome"], SUCCESS, final)
        self.assertEqual(final["runner_pid"], runner)
        self.wait_until(lambda: not same_process(runner, stamp), timeout=30)


if __name__ == "__main__":
    unittest.main()
