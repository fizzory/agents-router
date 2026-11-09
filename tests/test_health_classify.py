"""CB-S9: classify health without changing task outcomes, retry policy or routing.

All four adapters expose distinct connection diagnostics (Claude APIConnectionError,
Codex stream-disconnected, Antigravity ECONNRESET, Kiro connection-reset wording).
Kiro has no capacity signal. Every subprocess below is one of the fake CLIs.
"""

import errno
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from helpers import TempHome
from agents_router import jobs
from agents_router.config import Account, load_config, save_config
from agents_router.runner import Runner
from agents_router.runtimes.base import AttemptResult, HealthLaunchError
from agents_router.runtimes.claude import ClaudeParser
from agents_router.runtimes.codex import CodexParser
from agents_router.runtimes.antigravity import AntigravityParser
from agents_router.runtimes.kiro import KiroParser


PARSERS = {"claude": ClaudeParser, "codex": CodexParser, "antigravity": AntigravityParser, "kiro": KiroParser}
SCENARIOS = {"claude": "FAKE_CLAUDE_SCENARIO", "codex": "FAKE_CODEX_SCENARIO",
             "antigravity": "FAKE_AGY_SCENARIO", "kiro": "FAKE_KIRO_SCENARIO"}


def error_event(runtime, message):
    return {
        "claude": {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": [message]},
        "codex": {"type": "turn.failed", "error": {"message": message}},
        "antigravity": {"event": "result", "result": {"status": "ERROR", "error": message}},
        "kiro": {"type": "runFinished", "data": {"status": "error", "message": message}},
    }[runtime]


class HealthParserTests(unittest.TestCase):
    def test_CB_S9_health_defaults_to_none_for_all_outcomes(self):
        for outcome in ("success", "error", "usage_limit", "auth", "timeout", "lost", "cancelled", "max_turns", "no_account"):
            self.assertIsNone(AttemptResult(outcome).health_failure)

    def test_CB_S9_rejections_precede_transport_and_missing_result(self):
        diagnostics = (
            "unknown model: x", "model x does not exist", "invalid request: bad input",
            "configuration error: invalid TOML", "failed to load config.toml",
            "permission denied", "command requires approval", "unexpected argument '--json' found",
            "no space left on device", "too many open files", "cannot allocate memory", "exec format error",
        )
        for runtime, factory in PARSERS.items():
            for diagnostic in diagnostics:
                for terminal in (False, True):
                    with self.subTest(runtime=runtime, diagnostic=diagnostic, terminal=terminal):
                        parser = factory()
                        message = diagnostic + ": stream disconnected; connection reset"
                        if terminal:
                            parser.feed(error_event(runtime, message))
                        result = parser.finish(1, "" if terminal else message)
                        self.assertEqual(result.outcome, "error")
                        self.assertIsNone(result.health_failure)

    def test_CB_S9_usage_auth_and_terminal_task_failure_are_neutral(self):
        for runtime, factory in PARSERS.items():
            auth = "Unauthenticated" if runtime == "antigravity" else "not logged in"
            for diagnostic, outcome in (("usage limit reached", "usage_limit"), (auth, "auth")):
                with self.subTest(runtime=runtime, outcome=outcome):
                    result = factory().finish(1, diagnostic + ": stream disconnected; connection reset")
                    self.assertEqual(result.outcome, outcome)
                    self.assertIsNone(result.health_failure)
            parser = factory()
            parser.feed(error_event(runtime, "the task could not be completed"))
            self.assertIsNone(parser.finish(1, "").health_failure)

    def test_CB_S9_cli_argument_and_model_rejections_are_neutral(self):
        diagnostics = (
            "error: unknown option '--effort'",
            "error: unknown flag: --effort",
            "error: unrecognized option '--effort'",
            "error: unrecognized arguments: --effort high",
            "error: unexpected argument '--effort' found",
            "error: invalid value for '--effort': 'ultra'",
            "error: no such option: --effort",
            "error: unknown model: fake-model",
        )
        for runtime, factory in PARSERS.items():
            for diagnostic in diagnostics:
                for message in (diagnostic, diagnostic.upper()):
                    with self.subTest(runtime=runtime, diagnostic=message):
                        # Argument parsing can fail before any stream events exist.
                        result = factory().finish(1, message)
                        self.assertEqual(result.outcome, "error")
                        self.assertIsNone(result.health_failure)

    def test_CB_S9_crashes_and_missing_results_remain_health_failures(self):
        for runtime, factory in PARSERS.items():
            for exit_code, diagnostic in ((0, ""), (1, ""), (-11, "fatal: worker crashed")):
                with self.subTest(runtime=runtime, exit_code=exit_code):
                    result = factory().finish(exit_code, diagnostic)
                    self.assertEqual((result.outcome, result.health_failure), ("error", "no_result"))

    def test_CB_S9_transport_requires_terminal_diagnostics(self):
        # Earlier Codex errors that were followed by a completed turn aren't fatal.
        parser = CodexParser()
        parser.feed({"type": "error", "message": "stream disconnected before completion"})
        parser.feed({"type": "turn.completed"})
        self.assertIsNone(parser.finish(0, "").health_failure)
        self.assertIsNone(parser.finish(1, "unrelated CLI failure").health_failure)
        # Claude's api_retry is likewise progress, not a terminal diagnosis.
        parser = ClaudeParser()
        parser.feed({"type": "system", "subtype": "api_retry", "error": "APIConnectionError"})
        self.assertEqual(parser.finish(1, "").health_failure, "no_result")
        # Model-written words are never evidence of a transport failure.
        for parser, event in (
            (ClaudeParser(), {"type": "assistant", "message": {"content": [{"type": "text", "text": "connection reset"}]}}),
            (CodexParser(), {"type": "item.completed", "item": {"type": "agent_message", "text": "stream disconnected"}}),
            (AntigravityParser(), {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "ECONNRESET"}}),
            (KiroParser(), {"type": "sessionUpdate", "data": {"update": {
                "sessionUpdate": "agent_message_chunk", "content": {"text": "connection was reset"}}}}),
        ):
            parser.feed(event)
            self.assertEqual(parser.finish(1, "").health_failure, "no_result")

    def test_CB_S9_terminal_transport_diagnostics_are_counted(self):
        for runtime, factory in PARSERS.items():
            parser = factory()
            parser.feed(error_event(runtime, "connection reset; stream disconnected"))
            self.assertEqual(parser.finish(1, "").health_failure, "transport")


class HealthRunnerTests(TempHome):
    def setUp(self):
        super().setUp()
        # No real CLI discovery, login, host setup or provider access.
        fake_files = {"CODEX": "fake_codex.py", "AGY": "fake_agy.py", "KIRO": "fake_kiro.py"}
        for runtime, filename in fake_files.items():
            os.environ[f"AGT_{runtime}_COMMAND"] = json.dumps([sys.executable, str(Path(__file__).with_name(filename))])
        for key in list(os.environ):
            if key.startswith(("FAKE_CODEX_", "FAKE_AGY_", "FAKE_KIRO_", "FAKE_CLAUDE_DELAY", "SSH_")):
                os.environ.pop(key)
        os.environ["FAKE_KIRO_PRESET"] = "kiro-3.0.0"
        config = load_config()
        for runtime, settings in config.runtimes.items():
            settings.primary = self.root / runtime
            settings.primary.mkdir()
            settings.when_full = "queue"
            if runtime != "kiro":
                settings.include_primary = False
                settings.accounts = [Account("a", settings.primary / "a")]
        token = self.root / "antigravity" / "a" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("fake", encoding="utf-8")
        save_config(config)

    def runner(self, runtime="codex"):
        record = jobs.create_job(runtime=runtime, prompt="task", cwd=str(self.project), access="read", start=False)
        return Runner(record["job_id"])

    def attempt(self, runner, *, capacity=False):
        lease = runner._wait_for_account(set())
        self.assertIsNotNone(lease)
        try:
            if capacity:
                result = runner._attempt_with_capacity_retries(lease, "task", None, None, "task")
            else:
                result = runner._attempt(lease, "task")
            runner.finish(result.outcome, result.message, result)
            return result
        finally:
            lease.release()

    def managed_home_runner(self, runtime, *, shared_parent=False):
        runner = self.runner(runtime)
        if runtime == "antigravity":
            # Extra accounts without a login stop at auth preflight. The primary
            # uses a managed worker home without requiring a token file there.
            runner.runtime_config.include_primary = True
            runner.runtime_config.accounts = []
            account = runner.runtime_config.primary_account()
            home = runner.runtime.worker_home(runner.runtime_config, account)
        else:
            account = runner.runtime_config.accounts[0]
            if shared_parent:
                account.home = self.root / f"{runtime}-shared" / "a"
            home = account.home
        return runner, home

    def test_CB_S9_real_account_home_files_are_launch_failures(self):
        for runtime in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=runtime):
                runner, home = self.managed_home_runner(runtime)
                home.parent.mkdir(parents=True, exist_ok=True)
                home.write_text("not a directory", encoding="utf-8")
                with patch("agents_router.runner.worker_process") as process:
                    result = self.attempt(runner)
                self.assertEqual((result.outcome, result.health_failure), ("error", "launch"))
                self.assertTrue(result.before_start)
                self.assertTrue(runner.attempts[-1]["before_start"])
                self.assertIn("could not start the worker", result.message)
                self.assertIn(str(home), result.message)
                process.assert_not_called()

    def test_CB_S9_claude_own_home_file_is_a_launch_failure(self):
        runner, home = self.managed_home_runner("claude")
        runner.runtime_config.accounts[0].setup = "own"
        home.write_text("not a directory", encoding="utf-8")
        result = self.attempt(runner)
        self.assertEqual((result.outcome, result.health_failure), ("error", "launch"))
        self.assertTrue(result.before_start)

    def test_CB_S9_antigravity_gemini_root_file_is_a_launch_failure(self):
        runner, home = self.managed_home_runner("antigravity")
        home.mkdir(parents=True)
        (home / ".gemini").write_text("not a directory", encoding="utf-8")
        result = self.attempt(runner)
        self.assertEqual((result.outcome, result.health_failure), ("error", "launch"))
        self.assertTrue(result.before_start)

    def test_CB_S9_missing_managed_homes_are_created_and_succeed(self):
        for runtime in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=runtime):
                runner, home = self.managed_home_runner(runtime)
                self.assertFalse(home.exists())
                result = self.attempt(runner)
                self.assertEqual((result.outcome, result.health_failure), ("success", None))
                self.assertFalse(result.before_start)
                self.assertTrue(home.is_dir())
                if runtime == "antigravity":
                    self.assertTrue((home / ".gemini").is_dir())

    def test_CB_S9_account_root_permission_errors_are_launch_failures(self):
        original = os.scandir
        for runtime in ("claude", "codex", "antigravity"):
            runner, home = self.managed_home_runner(runtime)
            home.mkdir(parents=True)
            for code in (errno.EACCES, errno.EPERM):
                with self.subTest(runtime=runtime, errno=code):
                    def scandir(path):
                        if Path(path) == home:
                            raise OSError(code, os.strerror(code), str(home))
                        return original(path)

                    with patch("agents_router.runtimes.base.os.scandir", side_effect=scandir):
                        result = self.attempt(runner)
                    self.assertEqual((result.outcome, result.health_failure), ("error", "launch"))
                    self.assertTrue(result.before_start)

    def test_CB_S9_shared_parent_permission_errors_are_neutral(self):
        original = Path.mkdir
        for runtime in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=runtime):
                runner, home = self.managed_home_runner(runtime, shared_parent=True)

                def mkdir(path, *args, **kwargs):
                    if path == home.parent:
                        raise PermissionError(errno.EACCES, "shared parent denied", str(path))
                    return original(path, *args, **kwargs)

                with patch.object(Path, "mkdir", autospec=True, side_effect=mkdir):
                    result = self.attempt(runner)
                self.assertIsNone(result.health_failure)
                self.assertTrue(any("shared parent denied" in warning for warning in runner.warnings))

    def test_CB_S9_account_home_resource_errors_are_neutral(self):
        original = Path.mkdir
        for runtime in ("claude", "codex", "antigravity"):
            for code in (errno.ENOSPC, errno.EMFILE):
                with self.subTest(runtime=runtime, errno=code):
                    runner, home = self.managed_home_runner(runtime)
                    if home.exists():
                        shutil.rmtree(home)

                    def mkdir(path, *args, **kwargs):
                        if path == home:
                            raise OSError(code, os.strerror(code), str(path))
                        return original(path, *args, **kwargs)

                    with patch.object(Path, "mkdir", autospec=True, side_effect=mkdir):
                        result = self.attempt(runner)
                    self.assertIsNone(result.health_failure)
                    self.assertTrue(any(os.strerror(code) in warning for warning in runner.warnings))

    def test_CB_S9_uncertain_root_permission_attribution_is_neutral(self):
        original_scandir, original_access = os.scandir, os.access
        for runtime in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=runtime):
                runner, home = self.managed_home_runner(runtime)
                home.mkdir(parents=True)

                def scandir(path):
                    if Path(path) == home:
                        raise PermissionError(errno.EACCES, "root or shared ancestor denied", str(path))
                    return original_scandir(path)

                def access(path, mode):
                    return False if Path(path) == home.parent else original_access(path, mode)

                with patch("agents_router.runtimes.base.os.scandir", side_effect=scandir), patch(
                    "agents_router.runtimes.base.os.access", side_effect=access
                ):
                    result = self.attempt(runner)
                self.assertIsNone(result.health_failure)
                self.assertTrue(any("root or shared ancestor denied" in warning for warning in runner.warnings))

    def test_CB_S9_antigravity_auth_preflight_precedes_home_validation(self):
        runner = self.runner("antigravity")
        home = runner.runtime_config.accounts[0].home
        shutil.rmtree(home)
        home.write_text("not a directory", encoding="utf-8")
        with patch.object(runner.runtime, "prepare", wraps=runner.runtime.prepare) as prepare:
            result = self.attempt(runner)
        self.assertEqual((result.outcome, result.health_failure), ("auth", None))
        self.assertTrue(result.before_start)
        prepare.assert_not_called()

    def test_CB_S9_auth_rejections_after_home_creation_are_neutral(self):
        for runtime, scenario in (("claude", "auth"), ("codex", "success_auth")):
            with self.subTest(runtime=runtime), patch.dict(os.environ, {SCENARIOS[runtime]: scenario}):
                runner, home = self.managed_home_runner(runtime)
                result = self.attempt(runner)
                self.assertEqual((result.outcome, result.health_failure), ("auth", None))
                self.assertTrue(home.is_dir())

    def test_CB_S9_fake_streams_without_terminal_results(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime), patch.dict(os.environ, {SCENARIOS[runtime]: "no_result"}):
                runner = self.runner(runtime)
                result = self.attempt(runner)
                self.assertEqual((result.outcome, result.health_failure), ("error", "no_result"))
                self.assertFalse(list((self.home / "state").glob("*-health.json")))

    def test_CB_S9_codex_topic_4_empty_exit_one_is_no_result(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "empty_exit_1"
        runner = self.runner()
        result = self.attempt(runner)
        self.assertEqual((result.outcome, result.health_failure), ("error", "no_result"))
        self.assertEqual(runner.attempts[-1]["exit_code"], 1)
        self.assertEqual((runner.folder / "events.jsonl").read_bytes(), b"")
        self.assertEqual(result.message, "codex exited with status 1")

    def test_CB_S9_fake_streams_with_distinct_broken_connections(self):
        for runtime in PARSERS:
            scenario = "no-finish" if runtime == "kiro" else "transport"
            with self.subTest(runtime=runtime), patch.dict(os.environ, {SCENARIOS[runtime]: scenario}):
                result = self.attempt(self.runner(runtime))
                self.assertEqual((result.outcome, result.health_failure), ("error", "transport"))

    def test_CB_S9_unknown_model_diagnostics_are_neutral(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime), patch.dict(os.environ, {SCENARIOS[runtime]: "unknown_model"}):
                result = self.attempt(self.runner(runtime))
                self.assertEqual(result.outcome, "error")
                self.assertIsNone(result.health_failure)

    def test_CB_S9_capacity_counts_only_after_retries_end(self):
        for runtime in ("claude", "codex", "antigravity"):
            with self.subTest(runtime=runtime), patch.dict(os.environ, {SCENARIOS[runtime]: "capacity"}):
                runner = self.runner(runtime)
                runner.runtime_config.capacity_retries = 1
                intermediate = []
                original = runner._attempt

                def record(*args):
                    result = original(*args)
                    intermediate.append(result)
                    return result

                with patch.object(runner, "_attempt", side_effect=record), patch.object(runner, "_wait_for_capacity", return_value=None) as wait:
                    result = self.attempt(runner, capacity=True)
                self.assertEqual(wait.call_count, 1)
                self.assertEqual(len(intermediate), 2)
                self.assertTrue(all(r.at_capacity and r.health_failure is None for r in intermediate))
                self.assertEqual((result.outcome, result.health_failure), ("error", "capacity"))
                self.assertIn("model at capacity after 1 retries", result.message)

    def test_CB_S9_cancelled_and_timed_out_capacity_waits_are_neutral(self):
        os.environ["FAKE_CODEX_SCENARIO"] = "capacity"
        for outcome in ("timeout", "cancelled"):
            with self.subTest(outcome=outcome):
                runner = self.runner()
                with patch.object(runner, "_wait_for_capacity", return_value=outcome):
                    result = self.attempt(runner, capacity=True)
                self.assertEqual(result.outcome, outcome)
                self.assertIsNone(result.health_failure)

    def test_CB_S9_success_after_capacity_is_neutral(self):
        os.environ.update(FAKE_CODEX_SCENARIO="capacity", FAKE_CODEX_CAPACITY_FAILURES="1")
        runner = self.runner()
        with patch.object(runner, "_wait_for_capacity", return_value=None):
            result = self.attempt(runner, capacity=True)
        self.assertEqual((result.outcome, result.health_failure), ("success", None))

    def test_CB_S9_unreadable_result_is_parser_failure(self):
        runner = self.runner()
        parser = CodexParser()
        with patch.object(runner.runtime, "new_parser", return_value=parser), patch.object(parser, "finish", side_effect=ValueError("unreadable")):
            result = self.attempt(runner)
        self.assertEqual((result.outcome, result.health_failure), ("error", "parser"))
        self.assertEqual(result.message, "could not read the result: unreadable")

    def test_CB_S9_only_typed_account_local_launch_failures_count(self):
        for stage in ("launch", "process"):
            for error, kind in ((HealthLaunchError("account-local startup failed"), "launch"),
                                (RuntimeError("account-local startup failed"), None)):
                if stage == "process" and not isinstance(error, OSError):
                    error = OSError(str(error))
                with self.subTest(stage=stage, error=type(error).__name__):
                    runner = self.runner()
                    target = patch.object(runner.runtime, "launch", side_effect=error) if stage == "launch" else patch(
                        "agents_router.runner.worker_process", side_effect=error)
                    with target:
                        result = self.attempt(runner)
                    self.assertEqual((result.outcome, result.health_failure), ("error", kind))
                    self.assertTrue(result.before_start)

    def test_CB_S9_machine_wide_process_errors_are_neutral(self):
        for code in (errno.ENOENT, errno.ENOEXEC, errno.EACCES, errno.EMFILE, errno.ENOMEM, errno.EAGAIN):
            with self.subTest(errno=code):
                runner = self.runner()
                with patch("agents_router.runner.worker_process", side_effect=OSError(code, os.strerror(code))):
                    result = self.attempt(runner)
                self.assertIsNone(result.health_failure)
                self.assertTrue(result.before_start)

    def test_CB_S9_missing_binaries_are_neutral(self):
        for runtime in PARSERS:
            with self.subTest(runtime=runtime):
                runner = self.runner(runtime)
                with patch.object(runner.runtime, "find_binary", return_value=None):
                    result = self.attempt(runner)
                self.assertEqual(result.outcome, "error")
                self.assertIsNone(result.health_failure)
                self.assertTrue(result.before_start)

    def test_CB_S9_codex_bad_config_is_neutral(self):
        path = self.root / "codex" / "config.toml"
        path.write_text("[mcp_servers.broken\n", encoding="utf-8")
        result = self.attempt(self.runner())
        self.assertIn("could not read Codex config", result.message)
        self.assertIsNone(result.health_failure)
        self.assertTrue(result.before_start)

    def test_CB_S9_claude_read_guard_refusal_is_neutral(self):
        (self.root / "claude" / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
        result = self.attempt(self.runner("claude"))
        self.assertIn("cannot install the read-only shell guard", result.message)
        self.assertIsNone(result.health_failure)
        self.assertTrue(result.before_start)

    def test_CB_S9_kiro_engine_mismatch_is_neutral(self):
        runner = self.runner("kiro")
        runner.runtime_config.engine = "v3"
        with patch.dict(os.environ, {"FAKE_KIRO_PRESET": "", "FAKE_KIRO_FLAGS": "", "FAKE_KIRO_VERSION": "2.30.0"}):
            result = self.attempt(runner)
        self.assertIn("has no V3 engine", result.message)
        self.assertIsNone(result.health_failure)
        self.assertTrue(result.before_start)

    def test_CB_S9_max_turns_timeout_and_cancelled_are_neutral(self):
        with patch.dict(os.environ, {"FAKE_CLAUDE_SCENARIO": "max_turns"}):
            result = self.attempt(self.runner("claude"))
        self.assertEqual((result.outcome, result.health_failure), ("max_turns", None))
        for outcome in ("timeout", "cancelled"):
            with self.subTest(outcome=outcome):
                runner = self.runner()
                original = runner._pump

                def stop(*args):
                    original(*args)
                    return outcome

                with patch.object(runner, "_pump", side_effect=stop):
                    result = self.attempt(runner)
                self.assertEqual((result.outcome, result.health_failure), (outcome, None))


if __name__ == "__main__":
    unittest.main()
