"""A role job that hits a usage limit moves to the role's next provider, and a clean exit that did no work is `blocked`.

Two cases replay real streams (scrubbed) from the 2026-11-20 mentor run under `tests/fixtures/replay/`:
w1, a Kiro job that hit its monthly limit, and w5, a Codex job that stopped on an unanswered question.
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from helpers import TempHome
from test_kiro_runtime import runner_line
from test_role_run import FRAME_RE, result_body

from agents_router import cli, jobs, messages, role_run
from agents_router.runtimes import base, get_runtime

TESTS = Path(__file__).resolve().parent
REPLAY = TESTS / "fixtures" / "replay"
FAKE = {name: json.dumps([sys.executable, str(TESTS / f"fake_{name}.py")]) for name in ("codex", "kiro")}


class ReplayTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.pop("FAKE_CLAUDE_DELAY", None)
        for key in list(os.environ):
            if key.startswith(("FAKE_CODEX_", "FAKE_KIRO_")):
                self.enterContext(patch.dict(os.environ, {key: ""}))
                os.environ.pop(key)
        self.enterContext(patch.dict(os.environ, {"AGT_CODEX_COMMAND": FAKE["codex"], "AGT_KIRO_COMMAND": FAKE["kiro"],
                                                  "FAKE_KIRO_PRESET": "kiro-3.0.0"}))
        # The fake CLIs have no login check of their own; these cases are about the run, not auth.
        original = role_run.logged_in
        self.enterContext(patch.object(role_run, "logged_in", side_effect=lambda runtime, account:
                                       True if runtime.runtime in ("codex", "kiro") else original(runtime, account)))

    def settings(self, roles=""):
        for runtime in ("codex", "kiro"):
            (self.root / f"{runtime}-primary").mkdir(exist_ok=True)
        self.write_config(accounts=())
        with open(self.home / "settings.toml", "a", encoding="utf-8") as handle:
            handle.write(f'[codex]\nprimary = {json.dumps(str(self.root / "codex-primary"))}\ninclude_primary = true\n'
                         f'[kiro]\nprimary = {json.dumps(str(self.root / "kiro-primary"))}\n{roles}\n')

    def agt(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.stdin", io.StringIO("")):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_role(self, role, task):
        return self.agt("run", "--follow", "--role", role, "--cwd", str(self.project), task)

    def frame(self, text):
        lines = text.splitlines()
        group, _ = FRAME_RE.search(lines[0]).groups()
        return group, lines

    def job_ids(self):
        folder = self.home / "jobs"
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    def warnings(self, job_id):
        return jobs.read_status(job_id).get("warnings") or []

    # (a) usage limit → the role's next provider, same job ----------------------------------

    def test_w1_kiro_usage_limit_moves_the_same_job_to_the_next_provider(self):
        """w1: Kiro's monthly limit ended a researcher job with exit 1 and a false failover warning."""

        self.settings('[roles.researcher]\naccess = "edit"\nproviders = ["kiro", "codex"]')
        with patch.dict(os.environ, {"FAKE_KIRO_SCENARIO": "replay",
                                     "FAKE_KIRO_REPLAY": str(REPLAY / "w1-kiro-usage-limit")}):
            code, text, err = self.run_role("researcher", task="write the review fragments")
        self.assertEqual(code, 0, text + err)
        group_id, lines = self.frame(text)
        self.assertEqual(len(self.job_ids()), 1, "the move stays inside the one job")
        group = jobs.read_group(group_id)
        job_id = group["jobs"][0]["job_id"]
        self.assertEqual((group["jobs"][0]["provider"], group["jobs"][0]["position"]), ("codex", 2))
        status = jobs.read_status(job_id)
        self.assertEqual((status["outcome"], status["runtime"]), ("success", "codex"))
        self.assertEqual(jobs.read_spec(job_id).runtime, "codex")
        self.assertEqual([(move["from"], move["to"], move["position"], move["of"]) for move in status["provider_moves"]],
                         [("kiro", "codex", 2, 2)])
        self.assertIn("monthly usage limit", status["provider_moves"][0]["reason"])
        self.assertIn("moved to provider 2 of 2: codex after usage_limit on kiro", text)
        self.assertTrue(lines[-1].startswith("[agt] DONE · success"), lines[-1])
        self.assertEqual(result_body(text), "codex done on codex-primary")
        warnings = self.warnings(job_id)
        self.assertIn("kiro hit its usage limit; moved to provider 2 of 2: codex", warnings)
        self.assertFalse([item for item in warnings if "continuing on another account" in item], warnings)

    def test_usage_limit_on_the_last_provider_ends_the_job_without_a_failover_warning(self):
        self.settings('[roles.researcher]\naccess = "edit"\nproviders = ["kiro"]')
        with patch.dict(os.environ, {"FAKE_KIRO_SCENARIO": "replay",
                                     "FAKE_KIRO_REPLAY": str(REPLAY / "w1-kiro-usage-limit")}):
            code, text, err = self.run_role("researcher", task="write the review fragments")
        self.assertEqual(code, 1, text + err)
        self.frame(text)
        job_id = self.job_ids()[0]
        self.assertEqual(jobs.read_status(job_id)["outcome"], "usage_limit")
        self.assertNotIn("moved to provider", text)
        self.assertFalse([item for item in self.warnings(job_id) if "continuing on another account" in item])

    def test_a_read_role_never_moves_providers(self):
        """A read role already runs every provider in parallel; a limit on one is its own FAILED section."""

        self.settings('[roles.reviewer]\naccess = "read"\nproviders = ["kiro", "codex"]')
        with patch.dict(os.environ, {"FAKE_KIRO_SCENARIO": "replay",
                                     "FAKE_KIRO_REPLAY": str(REPLAY / "w1-kiro-usage-limit")}):
            code, text, err = self.run_role("reviewer", task="review the change")
        self.assertEqual(code, 0, text + err)
        self.assertEqual(len(self.job_ids()), 2)
        self.assertNotIn("moved to provider", text)
        runtimes = sorted(jobs.read_status(job)["runtime"] for job in self.job_ids())
        self.assertEqual(runtimes, ["codex", "kiro"])

    # (b) a clean exit that did not do the task is `blocked` ---------------------------------

    def test_w5_codex_unanswered_question_is_blocked_not_success(self):
        """w5: Codex asked for file access, nobody answered, and it ended "0/12 files written" as a success."""

        self.settings()
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "replay",
                                     "FAKE_CODEX_REPLAY": str(REPLAY / "w5-codex-ask-timeout.jsonl")}):
            code, text, err = self.agt("run", "--follow", "--runtime", "codex", "--access", "edit",
                                       "--cwd", str(self.project), "write the review fragments")
        self.assertEqual(code, 1, text + err)
        job_id = self.job_ids()[0]
        status = jobs.read_status(job_id)
        self.assertEqual(status["outcome"], "blocked")
        self.assertIn("stopped on a question nobody answered", status["message"])
        self.assertTrue(messages.open_questions(job_id))
        last = text.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("[agt] FAILED · blocked · the worker stopped on a question nobody answered"), last)

    def test_a_success_with_no_answer_is_blocked(self):
        """Claude can end a turn with a success result and no text: nothing for the caller to use."""

        self.settings()
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "empty_result"}):
            code, text, err = self.agt("run", "--follow", "--runtime", "claude", "--cwd", str(self.project), "do it")
        self.assertEqual(code, 1, text + err)
        status = jobs.read_status(self.job_ids()[0])
        self.assertEqual((status["outcome"], status["message"]), ("blocked", "the worker finished without an answer"))

    def test_a_question_after_real_work_stays_success(self):
        """Only a worker that did nothing but wait is blocked; w5 with one real command in it succeeded."""

        self.settings()
        lines = (REPLAY / "w5-codex-ask-timeout.jsonl").read_text(encoding="utf-8").splitlines()
        command = {"id": "item_99", "type": "command_execution", "command": "ls", "status": "in_progress"}
        lines.insert(-2, json.dumps({"type": "item.started", "item": command}))
        lines.insert(-2, json.dumps({"type": "item.completed", "item": {**command, "status": "completed",
                                                                        "exit_code": 0, "aggregated_output": ""}}))
        worked = self.root / "w5-worked.jsonl"
        worked.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_CODEX_SCENARIO": "replay", "FAKE_CODEX_REPLAY": str(worked)}):
            code, text, err = self.agt("run", "--follow", "--runtime", "codex", "--access", "edit",
                                       "--cwd", str(self.project), "write the review fragments")
        self.assertEqual(code, 0, text + err)
        job_id = self.job_ids()[0]
        self.assertTrue(messages.open_questions(job_id), "the question is still open")
        self.assertEqual(jobs.read_status(job_id)["outcome"], "success")


class KiroRunErrorTests(TempHome):
    def test_w1_message_is_kiros_run_error_not_the_node_warning(self):
        parser = get_runtime("kiro").new_parser()
        for line in (REPLAY / "w1-kiro-usage-limit.jsonl").read_text(encoding="utf-8").splitlines():
            runner_line(parser, line)
        result = parser.finish(1, (REPLAY / "w1-kiro-usage-limit.stderr").read_text(encoding="utf-8"))
        self.assertEqual(result.outcome, base.USAGE_LIMIT)
        self.assertTrue(result.message.startswith("You've reached your monthly usage limit."), result.message)
        self.assertNotIn("ExperimentalWarning", result.message)
        self.assertNotIn("(code", result.message)
