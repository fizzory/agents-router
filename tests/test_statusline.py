"""agt's usage tap in front of a profile's status line: wrap, restore and refresh leave yours as it was (S5, S6)."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

from helpers import ShareHome

from agents_router import statusline
from agents_router.config import load_config
from agents_router.share import settings as share_settings, triggers

ORIGINAL = "bash ~/.claude/statusline.sh"
# Not what json.dumps writes (4 spaces, a compact member, escapes, 1e3), so byte-equality proves agt edits the text
# in place instead of writing the JSON again.
SETTINGS = (
    '{\n'
    '    "model":"opus",\n'
    '    "env": {"GREETING": "caf\\u00e9 \\/ ok"},\n'
    '    "statusLine": {\n'
    '        "type": "command",\n'
    '        "command": "bash ~/.claude/statusline.sh",\n'
    '        "padding": 0,\n'
    '        "refreshInterval": 5\n'
    '    },\n'
    '    "cleanupPeriodDays": 1e3\n'
    '}\n'
)
NO_STATUS_LINE = '{\r\n\t"model": "opus",\r\n\t"env": {"A": "caf\\u00e9"}\r\n}\r\n'


def wait_for(check, what, deadline=30.0):
    """Wait, with a generous deadline, for a state the test depends on (never a fixed sleep)."""

    end = time.monotonic() + deadline
    while not check():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


class StatuslineBase(ShareHome):
    def setUp(self):
        super().setUp()
        self.claude = self.folder(".claude")
        self.settings = self.claude / "settings.json"

    def launcher(self):
        return str(triggers.launcher_path())

    def write(self, text, path=None):
        (path or self.settings).write_bytes(text.encode("utf-8"))

    def data(self, path=None):
        return json.loads((path or self.settings).read_bytes().decode("utf-8"))

    def command(self, path=None):
        return self.data(path)["statusLine"]["command"]

    def wrap(self, *folders):
        return statusline.refresh(None, True, profiles=list(folders) or [self.claude])

    def unwrap(self, *folders):
        return statusline.refresh(None, False, profiles=list(folders) or [self.claude])


class WrapTests(StatuslineBase):
    def test_wrap_then_unwrap_gives_back_the_same_bytes(self):
        self.write(SETTINGS)
        result = self.wrap()
        self.assertTrue(result.changed)
        self.assertEqual(result.problems, [])
        self.assertIn("claude: the status line records usage for agt", result.lines[0])
        wrapped = self.data()
        self.assertEqual(statusline.parse(wrapped["statusLine"]["command"]), (ORIGINAL, self.launcher()))
        before = json.loads(SETTINGS)
        self.assertEqual({**wrapped, "statusLine": None}, {**before, "statusLine": None})  # nothing else moved
        self.assertEqual({key: value for key, value in wrapped["statusLine"].items() if key != "command"},
                         {"type": "command", "padding": 0, "refreshInterval": 5})
        self.assertIn('    "cleanupPeriodDays": 1e3\n', self.settings.read_text())  # the rest keeps its bytes
        self.assertEqual(statusline.state(self.claude), "live")
        self.assertEqual(len(statusline.recorded()), 1)

        result = self.unwrap()
        self.assertTrue(result.changed)
        self.assertEqual(self.settings.read_bytes(), SETTINGS.encode("utf-8"))
        self.assertEqual(statusline.recorded(), [])
        self.assertEqual(statusline.state(self.claude), "missing")

    def test_no_status_line_gets_one_that_goes_again(self):
        self.write(NO_STATUS_LINE)  # tabs and CRLF
        self.wrap()
        line = self.data()["statusLine"]
        self.assertEqual(line, {"type": "command", "command": statusline.wrapped_command("", self.launcher())})
        text = self.settings.read_bytes().decode("utf-8")
        self.assertEqual(text.count("\n"), text.count("\r\n"))  # the file's own line ends and indent
        self.assertIn('\r\n\t"statusLine": {\r\n\t\t"type"', text)
        self.unwrap()
        self.assertEqual(self.settings.read_bytes(), NO_STATUS_LINE.encode("utf-8"))

        for text in ("{}\n", '{"model": "opus"}'):  # an empty object; a file on one line
            with self.subTest(text=text):
                self.write(text)
                self.wrap()
                self.assertEqual(statusline.parse(self.command()), ("", self.launcher()))
                self.unwrap()
                self.assertEqual(self.settings.read_bytes(), text.encode("utf-8"))

    def test_a_null_status_line_is_taken_over_and_removed(self):
        self.write('{\n  "statusLine": null,\n  "model": "opus"\n}\n')
        self.wrap()
        text = self.settings.read_text()
        self.assertEqual(text.count('"statusLine"'), 1)
        self.assertEqual(statusline.parse(self.command()), ("", self.launcher()))
        result = self.unwrap()
        self.assertEqual(result.problems, [])
        self.assertEqual(self.data(), {"model": "opus"})  # null and no key mean the same to Claude Code

    def test_a_settings_file_agt_made_goes_again(self):
        self.assertFalse(self.settings.exists())
        self.wrap()
        self.assertEqual(statusline.parse(self.command()), ("", self.launcher()))
        self.unwrap()
        self.assertFalse(self.settings.exists())

    def test_a_change_you_made_meanwhile_survives_unwrap(self):
        self.write(SETTINGS)
        self.wrap()
        data = self.data()
        data["model"] = "sonnet"  # Claude Code writes the file again in its own layout
        data["theme"] = "light"
        self.write(json.dumps(data, indent=2) + "\n")
        self.unwrap()
        restored = self.data()
        self.assertEqual(restored["model"], "sonnet")
        self.assertEqual(restored["theme"], "light")
        self.assertEqual(restored["statusLine"],
                         {"type": "command", "command": ORIGINAL, "padding": 0, "refreshInterval": 5})

    def test_an_edit_inside_the_wrapper_survives_unwrap(self):
        self.write(SETTINGS)
        self.wrap()
        edited = self.command().replace("statusline.sh", "status-v2.sh --compact")
        data = self.data()
        data["statusLine"]["command"] = edited
        self.write(json.dumps(data, indent=2) + "\n")
        self.assertEqual(statusline.state(self.claude), "live")
        self.unwrap()
        self.assertEqual(self.command(), "bash ~/.claude/status-v2.sh --compact")

    def test_a_linked_settings_file_stays_a_link(self):
        dotfiles = self.root / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "claude-settings.json"
        target.write_bytes(SETTINGS.encode("utf-8"))
        self.settings.symlink_to(target)
        self.wrap()
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(os.readlink(self.settings), str(target))
        self.assertEqual(statusline.parse(self.command(target)), (ORIGINAL, self.launcher()))
        self.unwrap()
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(os.readlink(self.settings), str(target))
        self.assertEqual(target.read_bytes(), SETTINGS.encode("utf-8"))

    def test_refresh_writes_the_launcher_with_sharing_off(self):
        self.assertFalse(share_settings.load(share_settings.path()).enabled)
        launcher = Path(self.launcher())
        self.assertFalse(launcher.exists())
        self.write(SETTINGS)
        self.wrap()
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(statusline.parse(self.command())[1], str(launcher))

    def test_a_wrapper_with_an_old_launcher_is_rewritten(self):
        old = statusline.wrapped_command(ORIGINAL, self.root / "old home" / "bin" / "agt-run")
        self.write(json.dumps({"statusLine": {"type": "command", "command": old, "padding": 1}}, indent=2) + "\n")
        self.assertEqual(statusline.state(self.claude), "missing")
        result = self.wrap()
        self.assertTrue(result.changed)
        self.assertIn("now runs", result.lines[0])
        self.assertEqual(statusline.parse(self.command()), (ORIGINAL, self.launcher()))
        self.assertEqual(self.data()["statusLine"]["padding"], 1)
        self.assertEqual(statusline.state(self.claude), "live")

    def test_a_second_wrap_changes_nothing(self):
        self.write(SETTINGS)
        self.wrap()
        record = statusline.record_path()
        before = (self.settings.read_bytes(), os.stat(self.settings).st_mtime_ns, record.read_bytes(),
                  os.stat(record).st_mtime_ns, sorted(os.listdir(statusline.backups_dir())))
        result = self.wrap()
        self.assertFalse(result.changed)
        self.assertEqual(result.lines, [])
        after = (self.settings.read_bytes(), os.stat(self.settings).st_mtime_ns, record.read_bytes(),
                 os.stat(record).st_mtime_ns, sorted(os.listdir(statusline.backups_dir())))
        self.assertEqual(after, before)
        self.assertEqual(statusline.parse(self.command()), (ORIGINAL, self.launcher()))  # never wrapped twice

    def test_every_edit_is_backed_up_and_the_first_copy_kept(self):
        self.write(SETTINGS)
        self.wrap()
        self.unwrap()  # within the same second, usually
        copies = sorted(statusline.backups_dir().iterdir())
        self.assertEqual(len(copies), 2)
        self.assertIn(SETTINGS.encode("utf-8"), [copy.read_bytes() for copy in copies])

    def test_a_concurrent_settings_edit_survives_wrap_unwrap_and_created_file_removal(self):
        backup = statusline._backup
        for operation in ("wrap", "unwrap", "remove"):
            with self.subTest(operation=operation):
                self.write(SETTINGS if operation != "remove" else "{}\n")
                if operation != "wrap":
                    self.wrap()
                edited = self.settings.read_bytes().replace(b"{", b'{"theme": "light",', 1)

                def edit_after_backup(*args):
                    result = backup(*args)
                    self.settings.write_bytes(edited)
                    return result

                with patch.object(statusline, "_backup", side_effect=edit_after_backup):
                    if operation == "wrap":
                        result = statusline.wrap_file(self.settings, "claude", self.launcher())
                    else:
                        result = statusline.unwrap_file(self.settings, "claude", created=operation == "remove")
                self.assertTrue(self.settings.exists(), "concurrent edit must not be deleted")
                self.assertEqual(self.settings.read_bytes(), edited)
                self.assertEqual(result.action, "problem")
                self.assertIn("changed while agt was editing", result.message)

    def test_a_concurrently_created_settings_file_survives_wrap(self):
        checked = statusline._checked

        def create_after_computing(*args):
            result = checked(*args)
            self.write(SETTINGS)
            return result

        with patch.object(statusline, "_checked", side_effect=create_after_computing):
            result = statusline.wrap_file(self.settings, "claude", self.launcher())
        self.assertEqual(self.settings.read_bytes(), SETTINGS.encode("utf-8"))
        self.assertEqual(result.action, "problem")

    def test_a_wrapper_agt_cant_read_is_left_alone_and_reported(self):
        broken = ": agt-usage-tap v1; bash ~/mine.sh"
        self.write(json.dumps({"statusLine": {"type": "command", "command": broken}}) + "\n")
        before = self.settings.read_bytes()
        for result in (self.wrap(), self.unwrap()):
            self.assertFalse(result.changed)
            self.assertEqual(len(result.problems), 1)
            self.assertIn("claude: The status line in ~/.claude/settings.json carries agt's tap", result.problems[0])
            self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(statusline.state(self.claude), "replaced")

        later = statusline.wrapped_command(ORIGINAL, self.launcher()).replace("tap v1;", "tap v2;")
        self.write(json.dumps({"statusLine": {"type": "command", "command": later}}) + "\n")
        before = self.settings.read_bytes()
        self.assertIn("carries agt's tap", self.wrap().problems[0])  # a later agt's wrapper is never wrapped again
        self.assertEqual(self.settings.read_bytes(), before)

        self.write("{ not json")
        result = self.wrap()
        self.assertIn("isn't valid JSON", result.problems[0])
        self.assertEqual(self.settings.read_text(), "{ not json")

    def test_worker_homes_are_never_wrapped(self):
        worker = Path(os.environ["AGT_HOME"]) / "homes" / "claude" / "w1"
        worker.mkdir(parents=True)
        self.write(SETTINGS, worker / "settings.json")
        link = self.user / ".claude-w1"
        link.symlink_to(worker)
        result = self.wrap(worker, link)
        self.assertEqual((result.lines, result.changed), ([], False))
        self.assertEqual((worker / "settings.json").read_bytes(), SETTINGS.encode("utf-8"))
        self.assertEqual(statusline.wrap_file(worker / "settings.json", "w1", self.launcher()).action, "skipped")
        self.assertIsNone(statusline.state(worker))
        self.assertEqual(statusline.recorded(), [])

    def test_windows_wraps_nothing(self):
        self.write(SETTINGS)
        with patch.object(statusline, "_windows", lambda: True):
            result = self.wrap()
            self.assertTrue(result.unsupported)
            self.assertEqual(statusline.state(self.claude), "unsupported")
            self.assertEqual(statusline.wrap_file(self.settings, "claude", self.launcher()).action, "skipped")
        self.assertEqual(self.settings.read_bytes(), SETTINGS.encode("utf-8"))
        self.assertFalse(Path(self.launcher()).exists())
        self.assertEqual(statusline.recorded(), [])

    def test_states(self):
        self.write(SETTINGS)
        self.assertEqual(statusline.state(self.claude), "missing")
        self.wrap()
        self.assertEqual(statusline.state(self.claude), "live")
        os.chmod(self.launcher(), 0o644)
        self.assertEqual(statusline.state(self.claude), "missing")  # the tap can't run
        os.chmod(self.launcher(), 0o755)
        data = self.data()
        data["statusLine"]["command"] = "npx ccstatusline"  # another tool took the status line over
        self.write(json.dumps(data, indent=2) + "\n")
        self.assertEqual(statusline.state(self.claude), "replaced")
        self.wrap()  # wraps the new one: it keeps the new status line
        self.assertEqual(statusline.parse(self.command()), ("npx ccstatusline", self.launcher()))
        self.assertEqual(statusline.state(self.claude), "live")

    def test_parse_gives_back_any_command(self):
        launcher = "/Users/a b/.agents-router/bin/agt-run"
        for original in ("", "'", '"', "$HOME/x", "`date`", "a; b", "a && b || c", "one\ntwo", "café ✓", "-n",
                         "it's \"$(q)\" \\ end", "printf '%s' \"${X:-y}\" | jq -r .model.id"):
            with self.subTest(original=original):
                self.assertEqual(statusline.parse(statusline.wrapped_command(original, launcher)), (original, launcher))
        good = statusline.wrapped_command("x", "/l")
        for word in ('"$(rm -rf ~)"', "~/x", "$HOME", "a b", "'open", '"a\\b"', "`id`"):
            with self.subTest(word=word):
                self.assertIsNone(statusline.parse(good.replace("__agt_o=x;", f"__agt_o={word};")))
        self.assertIsNone(statusline.parse(good + " "))
        self.assertIsNone(statusline.parse(": agt-usage-tap v1; " + good))


class ProfilesTests(StatuslineBase):
    def profile(self, name, uuid):
        folder = self.folder(name)
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}), encoding="utf-8")
        (folder / "logged_in").touch()  # fake `claude auth status`
        return folder

    def test_refresh_wraps_and_restores_every_managed_profile(self):
        self.profile(".claude", "u1")
        work = self.profile(".claude-work", "u2")
        self.write(SETTINGS)
        self.write('{"model": "haiku"}\n', work / "settings.json")
        config = load_config()
        folders = [account.home for account in statusline.managed_profiles(config)]
        self.assertTrue(any(os.path.samefile(folder, self.claude) for folder in folders))
        self.assertTrue(any(os.path.samefile(folder, work) for folder in folders))
        statusline.refresh(config, True)
        self.assertEqual(statusline.state(self.claude), "live")
        self.assertEqual(statusline.state(work), "live")

        statusline.record_path().unlink()  # even without agt's record, turning it off restores each profile
        result = statusline.refresh(config, False)
        self.assertEqual(result.problems, [])
        self.assertEqual(self.settings.read_bytes(), SETTINGS.encode("utf-8"))
        self.assertEqual((work / "settings.json").read_text(), '{"model": "haiku"}\n')


def find_shell(name):
    if name == "sh":
        return "/bin/sh"
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"):
        candidate = Path(folder) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# Reads its input (trailing newlines and all), writes to both streams, quotes, expands, and fails with 3.
SAMPLE = "tr 'a-z' 'A-Z'; printf '\\n--%s--\\n' \"it's $((6*7))\"; echo warn >&2; exit 3"
PAYLOADS = (b'{"session_id":"s1","note":"100% \\\\n done","rate_limits":{}}\n\n',
            b'{"model":{"id":"claude-x"}}')


@unittest.skipIf(os.name == "nt", "the wrapper is for /bin/sh")
class WrapperShellTests(StatuslineBase):
    def fake_launcher(self, body):
        folder = self.root / "fake bin"
        folder.mkdir(exist_ok=True)
        path = folder / "agt-run"
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_shell(self, shell, command, payload):
        # The timeout only stops a hang; nothing here depends on how long a run takes.
        return subprocess.run([shell, "-c", command], input=payload, capture_output=True, timeout=120)

    def test_output_and_exit_code_are_the_originals_in_each_shell(self):
        noisy = self.fake_launcher('cat >/dev/null; echo tap-out; echo tap-err >&2; exit 7\n')
        for name in ("sh", "bash", "zsh", "dash"):
            with self.subTest(shell=name):
                shell = find_shell(name)
                if shell is None:
                    self.skipTest(f"{name} isn't installed")
                for launcher in (noisy, self.root / "gone" / "agt-run"):  # with the tap, and with agt uninstalled
                    command = statusline.wrapped_command(SAMPLE, launcher)
                    for payload in PAYLOADS:
                        original = self.run_shell(shell, SAMPLE, payload)
                        wrapped = self.run_shell(shell, command, payload)
                        self.assertEqual(original.returncode, 3)
                        self.assertEqual((wrapped.stdout, wrapped.stderr, wrapped.returncode),
                                         (original.stdout, original.stderr, original.returncode))
                empty = self.run_shell(shell, statusline.wrapped_command("", noisy), PAYLOADS[0])
                self.assertEqual((empty.stdout, empty.stderr, empty.returncode), (b"", b"", 0))

    def test_the_status_line_never_waits_for_the_tap(self):
        hold = self.root / "hold"
        os.mkfifo(hold)
        holder = os.open(hold, os.O_RDWR)  # the tap's read blocks until the test writes a line
        self.addCleanup(os.close, holder)
        self.addCleanup(lambda: os.write(holder, b"go\n"))  # runs first: never leave the tap blocked
        marks = self.root / "marks"
        marks.mkdir()
        q = lambda path: "'" + str(path) + "'"
        launcher = self.fake_launcher(
            f'cat > {q(marks / "payload")}; printf "%s" "$*" > {q(marks / "args")}; touch {q(marks / "started")}\n'
            f'read -r line < {q(hold)}; touch {q(marks / "done")}\n')
        payload = PAYLOADS[0]
        original = self.run_shell("/bin/sh", SAMPLE, payload)
        wrapped = self.run_shell("/bin/sh", statusline.wrapped_command(SAMPLE, launcher), payload)
        # The wrapper has exited. The tap can't finish before the test writes to the FIFO, so it is still blocked.
        self.assertEqual((wrapped.stdout, wrapped.stderr, wrapped.returncode),
                         (original.stdout, original.stderr, original.returncode))
        wait_for((marks / "started").exists, "the tap to start")
        self.assertFalse((marks / "done").exists())
        self.assertEqual((marks / "payload").read_bytes(), payload)
        self.assertEqual((marks / "args").read_text(), "hook statusline")
        os.write(holder, b"go\n")
        wait_for((marks / "done").exists, "the tap to finish")


if __name__ == "__main__":
    unittest.main()
