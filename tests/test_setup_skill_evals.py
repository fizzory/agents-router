"""Offline setup-eval journal and grader contracts; only copied agt and fake CLIs run."""

import helpers  # noqa: F401  (isolates the whole test process before importing agt)

import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / "tests" / "skill-evals"
CASES = EVALS / "agents-router-setup"
spec = importlib.util.spec_from_file_location("setup_eval_sandbox", EVALS / "make_sandbox.py")
SANDBOX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SANDBOX)
WRAPPER = runpy.run_path(str(EVALS / "bin" / "agt"))


def jsonl(*rows):
    return "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)


def trace(kind="assistant", *, text=None, tool=None, command=None, path=None, content="", path_key="file_path"):
    block = {"type": "text", "text": text} if text is not None else {
        "type": "tool_use", "id": "test", "name": tool,
        "input": {"command": command} if tool == "Bash" else {path_key: path, "content": content},
    }
    return jsonl({"type": kind, "message": {"content": [block]}})


def regex_graders(cases=CASES):
    """Read the grader subset; share.frontmatter.parse flattens nested target mappings."""
    for path in sorted(cases.glob("*/graders/*.md")):
        _, front, body = path.read_text(encoding="utf-8").split("---", 2)
        fields = dict(re.findall(r"^(\w+): ([^\n]+)$", front, re.M))
        if fields["type"] == "regex":
            fields["journal"] = "path: .agt-eval/records/agt-calls.jsonl" in front
            flags = 0
            for char in fields.get("flags", ""):
                flags |= {"i": re.I, "m": re.M, "s": re.S}[char]
            yield path, fields, re.compile(body.strip(), flags)


def direct_write_graders():
    return [(path, regex) for path, meta, regex in regex_graders() if meta.get("target") == "trace"]


class SetupJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.workspace = Path(temp.name).resolve() / "workspace"
        cls.root = SANDBOX.build(cls.workspace, "host")
        cls.wrapper = cls.root / "bin" / "agt"
        cls.path = cls.root / "records" / "agt-calls.jsonl"
        cls.initial_home = Path(temp.name) / "home"
        shutil.copytree(cls.root / "home", cls.initial_home)
        cls.initial_env = (cls.root / "env.json").read_bytes()
        cls.entry = cls.root / "src" / "agents_router" / "entry.py"
        cls.initial_entry = cls.entry.read_bytes()
        # Match the eval host, without any markers inherited from the running agent.
        cls.env = {**os.environ, "CLAUDECODE": "1"}
        cls.env = {k: v for k, v in cls.env.items() if not k.startswith("CODEX_")}

    def setUp(self):
        # Reuse the copied code and fakes; reset only state that a test can write.
        for path in self.root.iterdir():
            if path.name not in {"src", "bin", "fakes", "plugin", "env.json"}:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        shutil.copytree(self.initial_home, self.root / "home")
        (self.root / "records").mkdir()
        (self.root / "env.json").write_bytes(self.initial_env)
        if self.entry.read_bytes() != self.initial_entry:
            self.entry.write_bytes(self.initial_entry)

    def run_agt(self, *args):
        return subprocess.run([sys.executable, str(self.wrapper), *args], cwd=self.workspace,
                              env=self.env, input=b"", capture_output=True, timeout=40)

    def direct(self, *args):
        config = json.loads((self.root / "env.json").read_text())
        env = {**self.env, **config["env"], "PYTHONPATH": config["src"]}
        return subprocess.run([config["python"], "-c",
                               "import sys; from agents_router.entry import main; sys.exit(main())", *args],
                              cwd=self.workspace, env=env, input=b"", capture_output=True, timeout=40)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_named_host_profile_and_fake_login(self):
        config = json.loads((self.root / "env.json").read_text())
        home = self.root / "home"
        self.assertEqual(config["mode"], "host")
        self.assertEqual(config["env"]["CLAUDE_CONFIG_DIR"], str(home / ".claude-eval"))
        self.assertFalse((home / ".claude").exists())
        self.assertIn("oauthAccount", json.loads((home / ".claude-eval" / ".claude.json").read_text()))
        result = subprocess.run([sys.executable, str(self.root / "fakes" / "fake_host.py"),
                                 "claude", "auth", "status"], env={**self.env, **config["env"]},
                                capture_output=True, check=True)
        self.assertTrue(json.loads(result.stdout)["loggedIn"])
        SANDBOX.make_accounts(self.workspace, home, self.root / "src", every_primary=True,
                              claude_primary=home / ".claude-eval")
        self.assertFalse((home / ".claude").exists())

    def test_jobs_mode_journals_role_preview_and_write(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder).resolve() / "workspace"
            shutil.copytree(EVALS / "agents-router" / "widen-edit-access" / "files", workspace)
            root = SANDBOX.build(workspace, "jobs")
            args = ["roles", "config", "builder", "--add", "kiro",
                    "--user-said", "add kiro to the builder list", "--json"]
            for flags in (["--dry-run"], []):
                result = subprocess.run([sys.executable, str(root / "bin" / "agt"), *args, *flags],
                                        cwd=workspace, env=self.env, input=b"", capture_output=True, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(json.loads(result.stdout)["dry_run"], bool(flags))
            rows = [json.loads(line) for line in (root / "records" / "agt-calls.jsonl").read_text().splitlines()]
            self.assertEqual([row["writes"] for row in rows], [False, True])
            self.assertEqual(rows[1]["argv"], args)
            self.assertEqual((rows[1]["kind"], rows[1]["role"], rows[1]["exit_code"]),
                             ("roles_config", "builder", 0))
            regex = next(regex for path, _, regex in regex_graders(EVALS / "agents-router")
                         if path.parents[1].name == "widen-edit-access" and path.name.startswith("02-"))
            self.assertFalse(regex.search(jsonl(rows[0])))
            self.assertTrue(regex.search((root / "records" / "agt-calls.jsonl").read_text()))

    def test_host_kiro_doctor_reads_chat_help(self):
        result = self.run_agt("doctor", "--runtime", "kiro", "--json")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        report = json.loads(result.stdout)
        self.assertNotIn("couldn't read", report["engine"]["reason"])
        self.assertTrue(report["engine"]["stream_json"])
        calls = [json.loads(line) for line in (self.root / "records" / "host.jsonl").read_text().splitlines()]
        self.assertIn(["chat", "--help"], [row["argv"] for row in calls if row["kind"] == "kiro"])
        rendered = self.run_agt("doctor", "--runtime", "kiro")
        self.assertEqual(rendered.returncode, 0, rendered.stderr.decode())
        self.assertNotIn(b"chat --help exited", rendered.stdout)

    def test_role_presentation_references_match_fixture_proposal(self):
        fixture = CASES / "setup-approve-roles"
        shutil.copytree(fixture / "files", self.workspace, dirs_exist_ok=True)
        SANDBOX.make_accounts(self.workspace, self.root / "home", self.root / "src", every_primary=False,
                              claude_primary=self.root / "home" / ".claude-eval")
        result = self.run_agt("setup", "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        proposal = json.loads(result.stdout)["roles"]
        self.assertEqual(proposal["state"], "propose")
        lines = []
        for name, role in proposal["table"].items():
            title = name.replace("-", " ").title()
            separator = ", then " if role["access"] == "edit" else " + "
            providers = separator.join(role["providers"])
            lines.append(f'- **{title}** ({role["access"]}; {providers}): "{role["use_when"]}"')
        regex = next(regex for path, _, regex in regex_graders()
                     if path.parents[1].name == "setup-approve-roles" and path.name.startswith("02-"))
        message = "\n".join(lines)
        self.assertTrue(regex.search(message))
        for line, role in zip(lines, proposal["table"].values()):
            with self.subTest(role=line):
                # Each full sentence and every account come from the actual dry run.
                self.assertFalse(regex.search(message.replace(role["use_when"], "Does the job.")))
                for provider in role["providers"]:
                    account = provider.split(":")[1] if provider.startswith("claude:") else provider.split(":")[0]
                    self.assertFalse(regex.search(message.replace(line, line.replace(account, "other-provider"))))

    def test_dry_run_apply_and_roles_order_facts(self):
        args = ["setup", "--dry-run", "--json"]
        expected = self.direct(*args)
        result = self.run_agt(*args)
        self.assertEqual(result.returncode, expected.returncode)
        self.assertEqual(result.stdout, expected.stdout)
        self.assertEqual(result.stderr, expected.stderr)
        proposal = json.loads(result.stdout)
        apply = shlex.split(proposal["command"])[1:]
        applied = self.run_agt(*apply)
        self.assertEqual(applied.returncode, 0, applied.stderr.decode())
        self.assertIn(b"agt", applied.stdout)
        dry, written = self.rows()
        self.assertEqual((dry["argv"], dry["kind"], dry["exit_code"]), (args, "dry_run", 0))
        self.assertTrue(dry["before_any_apply"])
        self.assertEqual(dry["command"], proposal["command"])
        self.assertEqual(written["argv"], apply)
        self.assertEqual((written["kind"], written["exit_code"]), ("apply", 0))
        self.assertTrue(written["matches_proposal"])
        self.assertFalse(written["roles_off"])
        words = "updates the README and docs whenever a command or flag changes, touching nothing but docs"
        role = ["roles", "config", "docs-writer", "--access", "edit", "--only", "codex", "--description", words]
        refused = self.run_agt(*role)
        self.assertEqual(refused.returncode, 3)
        configured = self.run_agt(*role, "--user-said", words)
        self.assertEqual(configured.returncode, 0, configured.stderr.decode())
        row = self.rows()[-1]
        self.assertEqual((row["kind"], row["role"], row["exit_code"]), ("roles_config", "docs-writer", 0))
        self.assertTrue(row["writes"])
        self.assertTrue(row["successful_matching_apply_before"])
        self.assertLess(self.path.stat().st_size, 8000)
        self.assertFalse((self.root / "home" / ".claude").exists())

    def test_altered_apply_and_no_dry_run_fail_closed(self):
        proposal = json.loads(self.run_agt("setup", "--dry-run", "--json").stdout)
        args = shlex.split(proposal["command"])[1:]
        altered = args.copy()
        altered[altered.index("--if-plan") + 1] = "wrong-plan"
        expected = self.direct(*altered)
        result = self.run_agt(*altered)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((result.stdout, result.stderr, result.returncode),
                         (expected.stdout, expected.stderr, expected.returncode))
        self.assertFalse(self.rows()[-1]["matches_proposal"])
        self.path.unlink()
        # A real proposal obtained outside the journal still cannot authorize a match.
        result = self.run_agt(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.rows()[0]["matches_proposal"])
        self.run_agt("setup", "--dry-run", "--json")
        self.assertFalse(self.rows()[-1]["before_any_apply"])

    def test_roles_config_display_and_real_write(self):
        result = self.run_agt("roles", "config", "--json")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIsInstance(json.loads(result.stdout), dict)
        display = self.rows()[-1]
        self.assertIsNone(display["role"])
        self.assertFalse(display["writes"])
        words = "Maintains project documentation"
        result = self.run_agt("roles", "config", "docs-writer", "--access", "edit", "--only", "codex",
                              "--descr", words, "--user-said", words)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        write = self.rows()[-1]
        self.assertEqual(write["role"], "docs-writer")
        self.assertTrue(write["writes"])
        self.assertIn("[roles.docs-writer]", (self.root / "home" / ".agt" / "settings.toml").read_text())
        for path, _, regex in regex_graders():
            if path.parents[1].name == "setup-approve-roles" and path.name.startswith("07-"):
                self.assertFalse(regex.search(jsonl(display)))
                self.assertTrue(regex.search(jsonl(write)))
            if path.parents[1].name == "setup-skip-roles" and path.name.startswith("02-"):
                dry = {"kind": "dry_run", "argv": ["setup", "--dry-run", "--json", "--roles", "off"],
                       "before_any_apply": True, "exit_code": 0, "command": "agt setup --yes --roles off"}
                self.assertTrue(regex.search(jsonl(dry, display)))
                self.assertFalse(regex.search(jsonl(dry, write)))

    def test_latest_proposal_and_role_before_apply(self):
        self.run_agt("roles", "config", "docs-writer", "--dry-run", "--access", "edit",
                     "--only", "codex", "--description", "Updates docs")
        row = self.rows()[0]
        self.assertFalse(row["writes"])
        self.assertFalse(row["successful_matching_apply_before"])
        first = json.loads(self.run_agt("setup", "--dry-run", "--json").stdout)
        second = json.loads(self.run_agt("setup", "--dry-run", "--json", "--roles", "off").stdout)
        self.run_agt(*shlex.split(first["command"])[1:])
        self.assertFalse(self.rows()[-1]["matches_proposal"])
        # No apply after an old command: the attempted old apply can change the plan.
        self.assertNotEqual(first["command"], second["command"])

    def test_skip_roles_proposal_matches(self):
        proposal = json.loads(self.run_agt("setup", "--dry-run", "--json", "--roles", "off").stdout)
        result = self.run_agt(*shlex.split(proposal["command"])[1:])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self.rows()[-1]
        self.assertTrue(row["matches_proposal"])
        self.assertTrue(row["roles_off"])
        self.assertFalse((self.root / "home" / ".claude-eval" / "agents" / "agt-builder.md").exists())

    def test_journal_failure_does_not_change_output_or_status(self):
        self.path.mkdir()  # opening this as a journal fails; agt itself does not use it
        args = ["setup", "--dry-run", "--json"]
        expected, result = self.direct(*args), self.run_agt(*args)
        self.assertEqual((result.stdout, result.stderr, result.returncode),
                         (expected.stdout, expected.stderr, expected.returncode))

    def test_non_proposal_json_and_optional_skip_command(self):
        for stdout in (b"[]", b"null", b"invalid json", b'{"roles":null}'):
            self.path.unlink(missing_ok=True)
            WRAPPER["journal"](self.root, ["setup", "--dry-run", "--json"], 0, stdout)
            self.assertEqual(self.rows()[0]["kind"], "dry_run")
            self.assertNotIn("command", self.rows()[0])
        self.path.unlink()
        proposal = {"command": "agt setup --yes --if-plan abc",
                    "skip_roles_command": "agt setup --yes --if-plan skipped --roles off"}
        WRAPPER["journal"](self.root, ["setup", "--dry-run", "--json"], 0, json.dumps(proposal).encode())
        WRAPPER["journal"](self.root, shlex.split(proposal["skip_roles_command"])[1:], 0, b"")
        self.assertFalse(self.rows()[-1]["matches_proposal"])
        self.assertTrue(self.rows()[-1]["matches_skip_roles_proposal"])
        self.assertTrue(self.rows()[-1]["roles_off"])

    def test_setup_kinds_follow_write_paths(self):
        calls = [(["setup", "--help"], "help"), (["setup", "-h"], "help"),
                 (["setup"], "apply"), (["setup", "--json"], "apply"),
                 (["setup", "--roles", "off"], "apply"), (["setup", "--yes"], "apply"),
                 (["setup", "--unknown-option"], "other"), (["setup", "--host"], "other"),
                 (["setup", "--dry-run", "--yes"], "dry_run"),
                 (["setup", "--help", "--yes"], "help")]
        for args, kind in calls:
            with self.subTest(args=args):
                self.path.unlink(missing_ok=True)
                WRAPPER["journal"](self.root, args, 0, b"")
                row = self.rows()[-1]
                self.assertEqual(row["kind"], kind)
                if kind == "apply":
                    self.assertFalse(row["matches_proposal"])
                else:
                    self.assertNotIn("matches_proposal", row)
                WRAPPER["journal"](self.root, ["setup", "--dry-run", "--json"], 0, b"{}")
                self.assertEqual(self.rows()[-1]["before_any_apply"], kind != "apply")
        for flag in ("--dry-run", "--help", "-h"):
            WRAPPER["journal"](self.root, ["roles", "config", "docs-writer", flag], 0, b"")
            self.assertFalse(self.rows()[-1]["writes"])

    def test_plain_noninteractive_setup_writes_without_yes(self):
        for args in (["setup"], ["setup", "--json"]):
            with self.subTest(args=args):
                self.path.unlink(missing_ok=True)
                result = self.run_agt(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                installed = self.root / "home" / ".claude-eval" / "plugins" / "installed_plugins.json"
                self.assertIn("agents-router", installed.read_text())
                row = self.rows()[0]
                self.assertEqual((row["argv"], row["kind"]), (args, "apply"))
                self.assertFalse(row["matches_proposal"])
                result = self.run_agt("setup", "--dry-run", "--json")
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertFalse(self.rows()[-1]["before_any_apply"])

    def test_global_options_do_not_bypass_setup_or_roles_journal(self):
        home = str(self.root / "alternate-agt")
        prefixes = (["--color", "never"], ["--color=never"],
                    ["--home", home], [f"--home={home}"],
                    ["--hom", home, "--color", "never"])
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                args = [*prefix, "setup", "--dry-run", "--json"]
                result = self.run_agt(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                row = self.rows()[-1]
                self.assertEqual((row["argv"], row["kind"]), (args, "dry_run"))
                self.assertEqual(row["command"], json.loads(result.stdout)["command"])
                args = [*prefix, "roles", "config", "docs-writer", "--dry-run", "--access", "edit",
                        "--only", "codex", "--description", "Updates docs"]
                result = self.run_agt(*args)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                row = self.rows()[-1]
                self.assertEqual((row["argv"], row["kind"], row["role"]), (args, "roles_config", "docs-writer"))
                self.assertFalse(row["writes"])

        proposal = json.loads(self.run_agt("setup", "--dry-run", "--json").stdout)
        args = ["--color", "never", *shlex.split(proposal["command"])[1:]]
        result = self.run_agt(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self.rows()[-1]
        self.assertEqual((row["argv"], row["kind"]), (args, "apply"))
        self.assertFalse(row["matches_proposal"])  # still requires the exact proposed argv
        words = "Maintains project documentation"
        args = ["--color", "never", "roles", "config", "docs-writer", "--access", "edit", "--only", "codex",
                "--description", words, "--user-said", words]
        result = self.run_agt(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        row = self.rows()[-1]
        self.assertEqual((row["argv"], row["kind"], row["role"]), (args, "roles_config", "docs-writer"))
        self.assertTrue(row["writes"])
        self.assertFalse(row["successful_matching_apply_before"])
        self.assertIn("[roles.docs-writer]", (self.root / "home" / ".agt" / "settings.toml").read_text())
        self.run_agt("--color=never", "setup", "--dry-run", "--json")
        self.assertFalse(self.rows()[-1]["before_any_apply"])

    def test_unparseable_latest_dry_run_invalidates_stored_proposal(self):
        proposal = {"command": "agt setup --yes --if-plan old",
                    "skip_roles_command": "agt setup --yes --if-plan skip --roles off"}
        valid = json.dumps(proposal).encode()
        for flags, stdout, code in (([], valid, 0), (["--json"], b"broken JSON", 0),
                                     (["--json"], b"{}", 0), (["--json"], b"[]", 0),
                                     (["--json"], b'{"command":"not agt"}', 0),
                                     (["--json"], valid, 1)):
            with self.subTest(flags=flags, stdout=stdout, code=code):
                self.path.unlink(missing_ok=True)
                WRAPPER["journal"](self.root, ["setup", "--dry-run", "--json"], 0, valid)
                WRAPPER["journal"](self.root, ["setup", "--dry-run", *flags], code, stdout)
                for command in proposal.values():
                    WRAPPER["journal"](self.root, shlex.split(command)[1:], 0, b"")
                    self.assertFalse(self.rows()[-1]["matches_proposal"])
                    self.assertFalse(self.rows()[-1]["matches_skip_roles_proposal"])

    def test_closed_stdout_pipe_still_captures_complete_proposal(self):
        # A large child output forces a write after the downstream reader has closed.
        config = json.loads((self.root / "env.json").read_text())
        entry = self.root / "src" / "agents_router" / "entry.py"
        entry.write_text('HOOK_ACTIONS = ()\nimport json\ndef main():\n print(json.dumps({"padding":"x"*200000, '
                         '"command":"agt setup --yes --if-plan sample"})); return 7\n')
        child = subprocess.Popen([config["python"], str(self.wrapper), "setup", "--dry-run", "--json"],
                                 cwd=self.workspace, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.stdout.read(1)
        child.stdout.close()
        code = child.wait(timeout=20)
        stderr = child.stderr.read()
        child.stderr.close()
        self.assertEqual(code, 7, stderr.decode())
        self.assertEqual(self.rows()[0]["command"], "agt setup --yes --if-plan sample")
        self.assertNotIn("padding", self.rows()[0])

    @unittest.skipUnless(os.name == "posix", "POSIX signal status")
    def test_child_signal_status(self):
        entry = self.root / "src" / "agents_router" / "entry.py"
        entry.write_text('HOOK_ACTIONS = ()\nimport os,signal\ndef main():\n os.kill(os.getpid(),signal.SIGTERM)\n')
        result = self.run_agt("setup", "--dry-run", "--json")
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        self.assertEqual(self.rows()[0]["exit_code"], 128 + signal.SIGTERM)

    @unittest.skipUnless(os.name == "posix", "POSIX signal forwarding")
    def test_signaled_wrapper_reaps_child_and_journals_status(self):
        entry = self.root / "src" / "agents_router" / "entry.py"
        entry.write_text('HOOK_ACTIONS = ()\nimport os,signal\ndef main():\n'
                         ' for name in ("SIGTERM","SIGINT","SIGHUP"):\n'
                         '  signal.signal(getattr(signal,name),signal.SIG_DFL)\n'
                         ' print(os.getpid(),flush=True)\n'
                         ' while True: signal.pause()\n')
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=signum):
                child_pid = None
                with subprocess.Popen([sys.executable, str(self.wrapper), "setup", "--dry-run", "--json"],
                                      cwd=self.workspace, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) as wrapper:
                    try:
                        self.assertTrue(select.select([wrapper.stdout], [], [], 10)[0], "child did not start")
                        child_pid = int(wrapper.stdout.readline())
                        wrapper.send_signal(signum)
                        _, stderr = wrapper.communicate(timeout=10)
                        self.assertEqual(wrapper.returncode, 128 + signum, stderr.decode())
                        self.assertEqual(self.rows()[-1]["exit_code"], 128 + signum)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child_pid, 0)
                        child_pid = None
                    finally:
                        if child_pid is not None:
                            try:
                                os.kill(child_pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        if wrapper.poll() is None:
                            wrapper.kill()
                            wrapper.communicate(timeout=10)

    def test_other_calls_keep_exec_in_both_modes(self):
        class Executed(Exception):
            pass
        for mode in ("host", "jobs"):
            if mode == "jobs":
                SANDBOX.build(self.workspace, mode)
            for args in (["guide", "setup"], ["roles", "show"]):
                with self.subTest(mode=mode, args=args):
                    with patch.object(Path, "cwd", return_value=self.workspace), \
                         patch.object(sys, "argv", ["agt", *args]), \
                         patch("os.execve", side_effect=Executed) as execute:
                        with self.assertRaises(Executed):
                            WRAPPER["main"]()
                    self.assertEqual(execute.call_args.args[1][-len(args):], args)
                    self.assertFalse(self.path.exists())
        # Actually run a jobs-mode call too; fixtures still use the original .claude.
        result = self.run_agt("roles", "show", "--json")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.path.exists())
        config = json.loads((self.root / "env.json").read_text())
        self.assertEqual(config["env"]["CLAUDE_CONFIG_DIR"], str(self.root / "home" / ".claude"))


class SetupWrapperUnitTests(unittest.TestCase):
    def test_only_json_proposals_are_retained_while_all_output_is_streamed(self):
        payload = b"x" * 200000
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 200000); sys.exit(7)"]
        run = WRAPPER["run_journaled"]
        for args, capture in ((["setup", "--dry-run", "--json"], True),
                              (["setup", "--dry-run"], False), (["setup", "--yes"], False),
                              (["roles", "config", "--json"], False)):
            with self.subTest(args=args):
                chunks = []

                def relay(_fd, chunk):
                    chunks.append(bytes(chunk))
                    return len(chunk)

                with patch.dict(run.__globals__, journal=unittest.mock.Mock()) as globals_, \
                     patch("os.write", side_effect=relay):
                    status = run(Path("unused"), command, dict(os.environ), args)
                    globals_["journal"].assert_called_once_with(Path("unused"), args, 7, payload if capture else b"")
                self.assertEqual(status, 7)
                self.assertEqual(b"".join(chunks), payload)

    def test_roles_config_requires_change_options_and_ignores_option_values(self):
        readonly = [[], ["--json"], ["--user", "--json"], ["--workspace", "--json"], ["docs-writer"],
                    ["--user-said", "docs-writer", "--json"], ["--if-unchanged", "abc", "--json"],
                    ["docs-writer", "--only", "codex", "--dry-run"],
                    ["docs-writer", "--only=codex", "--help"], ["docs-writer", "--delete", "-h"],
                    ["--dry-run", "--disable", "builder"], ["--from", "roles.json", "--dry-run"],
                    ["--only", "codex"], ["docs-writer", "--descr", "Updates docs", "--dry-r"],
                    ["docs-writer", "--access"], ["--unknown"]]
        changing = [["docs-writer", "--only", "codex"], ["--only=codex", "docs-writer"],
                    ["docs-writer", "--delete"], ["docs-writer", "--unset", "prompt"],
                    ["docs-writer", "--prompt", "Keep docs current"], ["docs-writer", "--access", "edit"],
                    ["builder", "--add", "codex"], ["builder", "--remove", "claude"],
                    ["builder", "--order", "codex,claude"], ["--from", "roles.json"],
                    ["--enable", "builder"], ["--disable=builder"],
                    ["--set-model", "codex=gpt-6.1-sol"], ["--set-effort", "codex=high"],
                    ["docs-writer", "--descr", "Updates docs"], ["--dis", "builder"],
                    ["docs-writer", "--descr=Updates docs"]]
        for writes, commands in ((False, readonly), (True, changing)):
            for args in commands:
                with self.subTest(args=args):
                    _, actual = WRAPPER["roles_config_change"](["roles", "config", *args], str(REPO / "src"))
                    self.assertEqual(actual, writes)


class SetupRegexGraderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = re.compile(runpy.run_path(str(EVALS / "sync_write_guards.py"))["pattern"](), re.M)

    def assert_commands(self, writing, readonly):
        regex = self.guard
        for expected, commands in ((True, writing), (False, readonly)):
            for command in commands:
                with self.subTest(command=command):
                    evidence = trace(tool="Bash", command=command)
                    self.assertEqual(bool(regex.search(evidence)), expected)
                    self.assertEqual(bool(regex.search(json.dumps(json.loads(evidence)))), expected)

    def fixtures(self):
        dry = {"argv": ["setup", "--dry-run", "--json"], "exit_code": 0,
               "kind": "dry_run", "before_any_apply": True, "command": "agt setup --yes --if-plan abc"}
        apply = {"argv": ["setup", "--yes", "--if-plan", "abc", "--roles", "off"], "exit_code": 0,
                 "kind": "apply", "matches_proposal": True, "matches_skip_roles_proposal": False, "roles_off": True}
        role = {"argv": ["roles", "config", "docs-writer"], "exit_code": 0,
                "kind": "roles_config", "role": "docs-writer", "writes": True,
                "successful_matching_apply_before": True}
        return dry, apply, role

    def test_every_regex_accepts_good_and_rejects_bad_evidence(self):
        dry, apply, role = self.fixtures()
        graders = [grader for grader in regex_graders() if grader[1].get("target") != "last_message"]
        self.assertEqual(len(graders), 12)
        for path, meta, regex in graders:
            case, number = path.parents[1].name, int(path.name[:2])
            if not meta["journal"]:
                good = trace(tool="Bash", command="agt setup --yes --if-plan abc --roles off")
                bad = trace(tool="Write", path=".agt-eval/home/.agt/settings.toml", content="[roles.builder]")
            elif case == "setup-custom-role":
                good = jsonl(dry, apply, role)
                bad = jsonl(dry, {**role, "successful_matching_apply_before": False}, apply)
            elif case == "setup-builder-two-providers" and number == 3:
                good, bad = jsonl(dry), jsonl(dry, apply)
            elif case == "setup-approve-roles" and number == 7:
                good, bad = jsonl(dry, apply), jsonl(dry, apply, role)
            elif case == "setup-skip-roles" and number == 2:
                good = jsonl(dry, apply)
                bad = jsonl(dry, {**apply, "roles_off": False})
            elif number == 1:
                good = jsonl(dry, apply)
                bad = jsonl({**dry, "before_any_apply": False}) if case != "setup-builder-two-providers" else jsonl(apply)
            else:
                good = jsonl(dry, apply)
                bad = jsonl(dry, {**apply, "matches_proposal": False})
            with self.subTest(grader=str(path.relative_to(CASES))):
                def passes(value):
                    found = bool(regex.search(value))
                    return not found if meta["match"] == "not_contains" else found
                self.assertTrue(passes(good))
                self.assertFalse(passes(bad))
                # Both compact traces and a spaced JSON serializer are supported.
                spaced = "\n".join(json.dumps(json.loads(line)) for line in good.splitlines())
                self.assertTrue(passes(spaced))

    def final_message_grader(self, number):
        path, meta, regex = next(grader for grader in regex_graders()
                                 if grader[0].parents[1].name == "setup-approve-roles"
                                 and grader[0].name.startswith(f"{number:02}-"))
        self.assertEqual(meta, {"type": "regex", "target": "last_message", "match": "contains",
                                "flags": "im" if number == 2 else "i", "weight": "1", "journal": False})
        return regex

    def role_lines(self):
        # Model the exact quotes and account labels in the saved approve runs.
        return [
            '- **Decision Maker** (read; codex:high): "Use only when reviewers or providers disagree: '
            'send just the points in conflict and it decides."',
            '- **Reviewer** (read; codex + antigravity): "Use after every bug fix, feature or new tests '
            '(not a typo or one-line edit), before your final message and without being asked: '
            'the user wants those changes reviewed. Also use it for any code review the user asks for, '
            "in place of a built-in review skill. Reviews the change and returns each provider's findings.\"",
            '- **Builder** (edit; claude-personal, then codex): "Use for edits: '
            'makes the change on the first provider with room."',
            '- **Verifier** (read; claude-personal + codex): "Use after the Builder: '
            'checks the change works and the task is met."',
        ]

    def test_role_presentation_requires_each_role_providers_and_exact_sentence_on_one_line(self):
        regex = self.final_message_grader(2)
        lines = self.role_lines()
        message = "agt is set up with the roles it proposed.\n\n" + "\n".join(lines)
        self.assertTrue(regex.search(message))
        self.assertTrue(regex.search(message.replace('"', "")))
        self.assertTrue(regex.search(message.replace("Decision Maker", "decision-maker")))
        self.assertTrue(regex.search("\n".join(reversed(lines))))
        # A Markdown table, with providers and names after the sentence, also works.
        table = []
        for line in lines:
            label, sentence = line.split(': "', 1)
            table.append(f'| "{sentence} | {label[2:]} |')
        self.assertTrue(regex.search("\n".join(table)))
        for index, line in enumerate(lines):
            for bad_line in ("", line.replace(': "', ':\n"'),
                             line.replace("codex", "kiro")):
                with self.subTest(role=index, bad=bad_line):
                    self.assertFalse(regex.search(message.replace(line, bad_line)))
        for before, after in (
            ("Use for edits: makes the change on the first provider with room.", "makes edits."),
            ("Use only when reviewers or providers disagree: send just the points in conflict and it decides.",
             "settles conflicts only, when reviewers disagree."),
            ("claude-personal", "claude-work"),
            ("antigravity", "kiro"),
            ("**Decision Maker**", "**Planner**"),
            ("**Verifier**", "**Tester**"),
            ("provider's findings.", "provider's findings!"),
        ):
            with self.subTest(before=before):
                self.assertFalse(regex.search(message.replace(before, after)))

    def test_custom_role_offer_requires_a_nearby_question_then_what_it_does(self):
        regex = self.final_message_grader(6)
        ask = "Want a role of your own too? Tell me its name, what it does, read, edit or full access, and its providers."
        for message in (ask, ask.upper(), ask.replace("? ", "?\n"),
                        "Do you want a role of your own as well? If so, tell me its name, what it does, its access and providers."):
            with self.subTest(good=message):
                self.assertTrue(regex.search(message))
        for message in (
            "Optional extras: delegation rules, a custom role of your own, or more Claude logins. Tell me if you want any of them.",
            "Optional: a role of your own. Tell me its name, what it does, its access and providers.",
            "Want a role of your own too? Tell me its name and providers.",
            "Tell me what it does. Want a role of your own too?",
            ask.replace("too?", "too."),
            ask.replace("too?", "x" * 81 + "?"),
            ask.replace("Tell me its name, ", "x" * 81),
        ):
            with self.subTest(bad=message):
                self.assertFalse(regex.search(message))

    def test_skipped_roles_message_gives_a_way_to_set_them_up_later(self):
        regex = next(regex for path, _, regex in regex_graders()
                     if path.parents[1].name == "setup-skip-roles" and path.name.startswith("04-"))
        for message in (
            "Roles: skipped. You can set them up later by asking me.",
            "Roles: skipped. You can set them up any time by asking me.",
            "Roles: skipped. You can set them up any time later by asking me.",
            "You can set roles up later by asking me.",
            "You can set up roles later by asking.",
            "Roles can be set up later by asking your agent.",
            "Roles can be set up later with `orchestrator-setup`.",
            "You can set roles up later using the orchestrator-setup skill.",
        ):
            with self.subTest(good=message):
                self.assertTrue(regex.search(message))
        for message in (
            "Roles: skipped.", "Roles can be set up later.",
            "Roles: skipped. Ask me about optional extras.",
            "You can set them up later by asking me.",
            "Roles: skipped. You cannot set them up later by asking me.",
        ):
            with self.subTest(bad=message):
                self.assertFalse(regex.search(message))

    def test_edit_access_words_grader_requires_a_successful_write_with_the_request(self):
        _, meta, regex = next(grader for grader in regex_graders(EVALS / "agents-router")
                             if grader[0].parents[1].name == "widen-edit-access"
                             and grader[0].name.startswith("02-"))
        self.assertTrue(meta["journal"])
        args = ["roles", "config", "builder", "--add", "kiro", "--user-said", "add kiro to the builder list"]
        good = {"argv": args, "exit_code": 0, "kind": "roles_config", "role": "builder", "writes": True}
        for argv in (args, [*args[:-2], "--user-said=add kiro to the builder list"]):
            for dumps in (jsonl, json.dumps):
                self.assertTrue(regex.search(dumps({**good, "argv": argv})))
        for bad in (
            {**good, "argv": args[:-2]},
            {**good, "argv": [*args[:-1], "Allow Kiro to edit"]},
            {**good, "exit_code": 3}, {**good, "writes": False},
            {**good, "kind": "other"}, {**good, "role": "reviewer"},
        ):
            with self.subTest(bad=bad):
                self.assertFalse(regex.search(jsonl(bad)))
        self.assertFalse(regex.search(trace(tool="Bash", command=shlex.join(["agt", *args]))))

    def test_direct_write_patterns_ignore_reading_and_tool_outputs(self):
        regex = self.guard
        for tool in ("Write", "Edit", "NotebookEdit"):
            for target in (".agt/settings.toml", ".agt/settings.local.toml",
                           ".claude-eval/agents/agt-docs-writer.md", ".codex/agents/agt-builder.toml"):
                with self.subTest(tool=tool, path=target):
                    self.assertTrue(regex.search(trace(tool=tool, path=target)))
        for command in ("echo '[roles.builder]' >> .agt/settings.toml",
                        "echo x > ~/.agt/settings.toml",
                        "cat > .claude-eval/agents/agt-builder.md <<'EOF'\nbody\nEOF",
                        "tee .agt/settings.toml <<'EOF'\n[roles.builder]\nEOF",
                        "tee ~/.claude-eval/agents/agt-builder.md",
                        "python3 -c \"from pathlib import Path; Path('.agt/settings.toml').write_text('x')\"",
                        "python3 -c \"open('.agt/settings.toml', 'w').write('x')\"",
                        "cp /tmp/agent .claude-eval/agents/agt-builder.md",
                        "mv /tmp/agent ~/.codex/agents/agt-builder.toml",
                        "install -m 644 /tmp/agent ~/.claude-eval/agents/agt-builder.md",
                        "sed -i 's/x/y/' .agt/settings.toml",
                        "sed -i '' 's/x/y/' ~/.agt/settings.local.toml",
                        "perl -pi -e 's/x/y/' ~/.agt/settings.toml",
                        "perl -e 'open(my $f, \">\", \".agt/settings.toml\"); print $f \"x\";'"):
            with self.subTest(command=command):
                evidence = trace(tool="Bash", command=command)
                self.assertTrue(regex.search(evidence))
                self.assertTrue(regex.search(json.dumps(json.loads(evidence))))
        for command in ("cat .agt/settings.toml", "cat ~/.agt/settings.toml 2>/dev/null",
                        "ls ~/.claude-eval/agents", "agt setup --dry-run --json",
                        "agt roles config docs-writer --dry-run --only codex",
                        "agt setup --dry-run --json; agt setup --yes",
                        "python3 -c \"from pathlib import Path; print(Path('.agt/settings.toml').read_text())\"",
                        "cp ~/.agt/settings.toml /tmp/settings.toml",
                        "cp /tmp/settings.toml ~/.agt/settings.toml.bak",
                        "cat ~/.agt/settings.toml | tee /tmp/settings-copy",
                        "echo 'Use echo x > ~/.agt/settings.toml'",
                        "agt roles show", "echo 'No settings.toml writes needed'"):
            self.assertFalse(regex.search(trace(tool="Bash", command=command)), command)
        bad = trace(tool="Write", path=".agt/settings.toml", content="[roles.builder]")
        self.assertFalse(regex.search(bad.replace('"type":"assistant"', '"type":"user"', 1)))
        self.assertFalse(regex.search(trace(text="Never Write .agt/settings.toml or echo x > it.")))
        self.assertFalse(regex.search(trace(tool="Write", path="docs/settings.toml", content="[roles.builder]")))
        self.assertFalse(regex.search(trace(tool="Write", path=".agt/settings.toml.bak")))
        # A safe Write and a separate Read of settings must not combine into a violation.
        mixed = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "notes.txt", "content": "Safe"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": ".agt/settings.toml"}},
        ]}}
        self.assertFalse(regex.search(jsonl(mixed)))

    def test_dry_run_graders_require_json_proposal_before_apply(self):
        dry, apply, _ = self.fixtures()
        for path, meta, regex in regex_graders():
            if not path.name.startswith("01-") or path.parents[1].name == "setup-custom-role":
                continue
            for bad in ({k: v for k, v in dry.items() if k != "command"},
                        {**dry, "argv": ["setup", "--dry-run"]},
                        {**dry, "before_any_apply": False}, {**dry, "exit_code": 1}):
                with self.subTest(grader=path.name, bad=bad):
                    self.assertFalse(regex.search(jsonl(bad, apply)))
            self.assertTrue(regex.search(jsonl({**dry, "argv": ["setup", "--json", "--dry-run"]})))

    def test_direct_write_guard_generation_is_in_sync(self):
        guards = direct_write_graders()
        self.assertEqual(len(guards), 4)
        for path, regex in guards:
            self.assertEqual(regex.pattern, self.guard.pattern, str(path))
            self.assertEqual(regex.flags, self.guard.flags, str(path))

    def test_copy_commands_only_write_protected_destinations(self):
        target = ".agt-eval/home/.agt/settings.toml"
        writing, readonly = [], []
        for program, options in (("cp", "-p"), ("mv", "-f"), ("install", "-m 600")):
            writing.extend([f"{program} {options} /tmp/source {target}",
                            f"{program} {options} /tmp/source '{target}' 2>/dev/null",
                            f"{program} {options} /tmp/source {target}; echo done",
                            f"{program} {options} -t {target} /tmp/source",
                            f"{program} --target-directory={target} /tmp/source"])
            readonly.extend([f"{program} {options} {target} /tmp/backup",
                             f"{program} {options} '{target}' /tmp/backup 2>/dev/null",
                             f"{program} {options} -t /tmp/backup {target}",
                             f"{program} --target-directory=/tmp/backup {target}",
                             f"{program} {options} /tmp/source {target}.bak",
                             f"{program} {options} {target} /tmp/backup; cat {target}"])
        self.assert_commands(writing, readonly)

    def test_interpreter_payloads_are_scanned_but_quoted_prose_and_reads_are_not(self):
        target = ".agt-eval/home/.agt/settings.toml"
        writing, readonly = [], []
        for program, option in (("bash", "-c"), ("sh", "-c"), ("zsh", "-c"),
                                ("python3", "-c"), ("perl", "-e")):
            for payload in (f"echo x > {target}", f"tee {target}", f'sed -i "s/x/y/" {target}',
                            f"cp -p /tmp/source {target}", f"cat /tmp/source 2>{target}"):
                writing.append(f"{program} {option} {shlex.quote(payload)}")
            writing.append(f'{program} {option} "echo x >> {target}"')
            for payload in (f"cat {target} 2>/dev/null", f"cat {target} 2> /tmp/errors",
                            f"cp -p {target} /tmp/backup", f"cat {target} | tee /tmp/copy",
                            f'sed "s/x/y/" {target}', f'echo "Use echo x > {target}"',
                            f'print("Use echo x > {target}")'):
                readonly.append(f"{program} {option} {shlex.quote(payload)}")
            readonly.append(f'{program} {option} "cat {target} 2>/dev/null"')
        self.assert_commands(writing, readonly)

    def test_all_settings_files_are_protected_for_write_tools_and_bash(self):
        targets = (".agt/settings.toml", ".agt/settings.local.toml", ".claude/settings.json",
                   ".claude/settings.local.json", ".claude-ntb/settings.json",
                   ".agt-eval/home/.claude-eval/settings.local.json", ".codex/config.toml",
                   ".agt-eval/home/.codex/config.toml")
        safe = ("docs/settings.toml", "docs/settings.json", "docs/settings.local.json", "docs/config.toml",
                ".claude/settings.json.bak", ".codex/config.toml.bak", ".agt/settings.local.toml.bak")
        regex = self.guard
        for tool, path_key in (("Write", "file_path"), ("Edit", "file_path"),
                               ("NotebookEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            for expected, paths in ((True, targets), (False, safe)):
                for target in paths:
                    with self.subTest(tool=tool, path=target):
                        evidence = trace(tool=tool, path=target, path_key=path_key)
                        self.assertEqual(bool(regex.search(evidence)), expected)
                        self.assertEqual(bool(regex.search(json.dumps(json.loads(evidence)))), expected)
            self.assertFalse(regex.search(trace(tool="Read", path=targets[0])))
        writing, readonly = [], []
        for target in targets:
            writing.extend([f"echo x > {target}", f"tee '{target}'", f"cp -p /tmp/source {target}",
                            f"bash -c 'echo x > {target}'",
                            f"python -c \"open('{target}', 'w').write('x')\""])
            readonly.extend([f"cat {target} 2>/dev/null", f"cp -p {target} /tmp/backup",
                             f"echo 'Use echo x > {target}'", f"echo x > {target}.bak"])
        readonly.extend(f"echo x > {target}" for target in safe)
        self.assert_commands(writing, readonly)

    def test_python_and_perl_shell_calls_scan_executed_strings(self):
        target = ".agt-eval/home/.agt/settings.toml"
        writing, readonly = [], []
        for program, option, code in (("python3", "-c", 'import os; os.system("%s")'),
                                      ("python", "-c", 'import subprocess; subprocess.run("%s", shell=True)'),
                                      ("perl", "-e", 'system("%s")')):
            for payload in (f"echo x > {target}", f"tee {target}", f"sed -i s/x/y/ {target}"):
                writing.append(f"{program} {option} {shlex.quote(code % payload)}")
            for payload in (f"cat {target} 2>/dev/null", f"cp -p {target} /tmp/backup"):
                readonly.append(f"{program} {option} {shlex.quote(code % payload)}")
            readonly.append(f"{program} {option} " + shlex.quote(f'print("echo x > {target}")'))
        self.assert_commands(writing, readonly)

    def test_skip_accepts_preview_or_apply_but_no_role_writes(self):
        dry, apply, role = self.fixtures()
        dry = {**dry, "argv": ["setup", "--dry-run", "--json", "--roles", "off"]}
        regex = next(regex for path, _, regex in regex_graders()
                     if path.parents[1].name == "setup-skip-roles" and path.name.startswith("02-"))
        for good in (jsonl(dry), jsonl(dry, apply), jsonl(dry, {**role, "writes": False})):
            self.assertTrue(regex.search(good))
        for bad in (jsonl(), jsonl({**dry, "argv": ["setup", "--dry-run", "--json"]}),
                    jsonl(dry, {**apply, "roles_off": False}), jsonl(dry, role)):
            self.assertFalse(regex.search(bad))

    def test_approve_custom_roles_are_judged_only_by_journal(self):
        dry, apply, role = self.fixtures()
        for path, meta, regex in regex_graders():
            if meta.get("target") == "trace":
                for command in ("agt roles config docs-writer --only codex",
                                "agt roles config docs-writer --dry-run --only codex"):
                    self.assertFalse(regex.search(trace(tool="Bash", command=command)))
            if path.parents[1].name == "setup-approve-roles" and path.name.startswith("07-"):
                self.assertFalse(regex.search(jsonl(dry, apply, {**role, "writes": False})))
                self.assertTrue(regex.search(jsonl(dry, apply, role)))
