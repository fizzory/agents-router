"""Write app golden JSON through the real producers in a deterministic fake home.

Run: PYTHONPATH=src:tests python3 tests/generate_settings_fixtures.py
Only temporary paths and backup filenames are normalized; contract fields and revisions are not.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from unittest.mock import patch

from helpers import FAKE_HOST

from agents_router import cli, synclog

FOLDER = Path(__file__).parent / "fixtures" / "app"
NOW = 1800000000.0


def share_inputs(home: Path) -> None:
    """Synthetic inputs for every non-file sharing planner; never execute them."""
    claude = home / ".claude"
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "notes": {"command": "notes-server", "args": []},
        "private": {"command": "private-server", "env": {"API_KEY": "synthetic-fixture-secret"}}
    }}), encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({
        "enabledPlugins": {"example@tools": True},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo example"}]}]}
    }), encoding="utf-8")
    plugin = claude / "plugins" / "example"
    skill = plugin / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\nExample.\n", encoding="utf-8")
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "example@tools": [{"scope": "user", "installPath": str(plugin), "version": "1.0.0"}]
    }}), encoding="utf-8")
    (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps({
        "tools": {"source": {"source": "github", "repo": "example/tools"}}
    }), encoding="utf-8")
    (claude / "CLAUDE.md").write_text("Example instructions.\n", encoding="utf-8")


def fixture_texts() -> dict[str, str]:
    strftime = time.strftime

    def utc_clock(format, when=None):
        # macOS strftime can use the process timezone for %z even with gmtime.
        return strftime(format.replace("%z", "+0000"), time.gmtime(NOW) if when is None else when)

    with tempfile.TemporaryDirectory(prefix="agt-settings-fixtures-") as temporary:
        root = Path(temporary).resolve()
        home, agt, project = root / "home", root / "agt", root / "project"
        for path in (home / ".claude", home / "accounts" / "work", agt, project / ".agt"):
            path.mkdir(parents=True)
        (home / ".claude" / "logged_in").touch()
        (agt / "settings.toml").write_text('max_jobs = 6\n[claude]\nprimary = "~/.claude"\n'
                                          'include_primary = true\n[[claude.accounts]]\nname = "work"\n'
                                          'home = "~/accounts/work"\nenabled = true\n[codex]\nmax_jobs = 4\n', encoding="utf-8", newline="\n")
        (project / ".agt" / "settings.toml").write_text("max_depth = 1\n", encoding="utf-8", newline="\n")
        (project / ".agt" / "settings.local.toml").write_text("max_depth = 0\n", encoding="utf-8", newline="\n")
        fake = lambda kind: json.dumps([sys.executable, str(FAKE_HOST), kind])
        env = {"HOME": str(home), "USERPROFILE": str(home), "AGT_HOME": str(agt),
               "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
               "AGT_CLAUDE_COMMAND": fake("claude"), "AGT_CODEX_COMMAND": fake("codex"),
               "AGT_AGY_COMMAND": fake("agy"), "AGT_KIRO_COMMAND": fake("kiro")}
        previous_cwd = Path.cwd()
        result = {}

        def capture(name, argv, expected=0):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([*argv, "--json"])
            if code != expected:
                raise AssertionError(f"{argv}: exit {code}, expected {expected}: {err.getvalue()}")
            def normalize(value):
                if isinstance(value, dict):
                    return {key: normalize(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [normalize(item) for item in value]
                if not isinstance(value, str):
                    return value
                text = value.replace(str(root), "<ROOT>").replace(str(FAKE_HOST), "<FAKE_HOST>")
                if "<ROOT>" in text:
                    text = text.replace(os.sep, "/")
                text = re.sub(r"<ROOT>/agt/state/backups/settings/[^\n]+\.toml", "<ROOT>/agt/state/backups/settings/<BACKUP>.toml", text)
                return re.sub(r"<ROOT>/agt/state/backups/settings/[^\n ]+-roles-undo\.json",
                              "<ROOT>/agt/state/backups/settings/<UNDO>.json", text)

            try:
                data = normalize(json.loads(out.getvalue()))
            except json.JSONDecodeError as error:
                raise AssertionError(f"{argv}: not JSON: {out.getvalue()!r}") from error
            text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            result[name + ".json"] = text

        try:
            os.chdir(project)
            with patch.dict(os.environ, env), patch("time.time", return_value=NOW), patch("time.monotonic", return_value=12345.0), \
                    patch("time.localtime", side_effect=lambda seconds=None: time.gmtime(NOW if seconds is None else seconds)), \
                    patch("time.strftime", side_effect=utc_clock), \
                    patch("agents_router.hosts.detect_host", return_value="unknown"), patch("sys.stdin.isatty", return_value=False):
                capture("config-show", ["config", "show"])
                # General edge states, with distinct User/Workspace file-only
                # values. Capture the producer rather than editing golden JSON.
                user = agt / "settings.toml"
                original = user.read_text()
                user.write_text(original + '\n[kiro]\nmax_jobs_by_room = [[0, 2]]\n', encoding="utf-8")
                workspace_file = project / ".agt" / "settings.toml"
                original_workspace = workspace_file.read_text()
                workspace_file.write_text(original_workspace + '[codex]\nmax_jobs_by_room = [[0, 1]]\n', encoding="utf-8")
                capture("config-show-levels", ["config", "show", "--all"])
                user.write_text('max_jobs = 6\ninvalid = ?\n', encoding="utf-8")
                capture("config-show-broken", ["config", "show", "--all"], 2)
                user.write_text(original, encoding="utf-8")
                workspace_file.write_text(original_workspace, encoding="utf-8")
                capture("config-set", ["config", "set", "max_depth", "2", "--user"])
                entry = next(synclog.entries())["id"]
                with patch("agents_router.hosts.detect_host", return_value="claude"):
                    capture("config-set-needs-user-words", ["config", "set", "max_depth", "3", "--user"], 3)
                capture("config-set-error", ["config", "set", "codex.usage_ceiling", "wrong", "--user"], 2)
                capture("config-unset-absent", ["config", "unset", "kiro.default_model", "--user"])
                capture("accounts-list", ["accounts", "list"])
                capture("accounts-check", ["accounts", "check", "primary"])
                capture("accounts-add-check", ["accounts", "add", "spare", "--check"])
                capture("accounts-add-check-error", ["accounts", "add", "work", "--check"], 1)
                discovered = home / ".claude-lab"
                discovered.mkdir()
                (discovered / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "fixture-lab"}}), encoding="utf-8")
                capture("accounts-list-locked", ["accounts", "list"])
                (discovered / ".claude.json").unlink()
                discovered.rmdir()
                capture("accounts-enable-refusal", ["accounts", "enable", "primary"], 2)
                capture("config-revert", ["config", "revert", entry])
                capture("accounts-disable", ["accounts", "disable", "work"])
                capture("accounts-enable", ["accounts", "enable", "work"])
                user = agt / "settings.toml"
                user.write_text('[claude]\nprimary = "~/.claude"\ninclude_primary = true\n'
                                '[[claude.accounts]]\nname = "work"\nhome = "~/accounts/work"\n'
                                '[roles.reviewer]\nproviders = ["claude:work/sonnet:high"]\n', encoding="utf-8")
                capture("roles-show", ["roles", "show"])
                local = project / ".agt" / "settings.local.toml"
                local_text = local.read_text()
                original_roles = user.read_text()
                local.write_text(local_text + '[roles.reviewer]\nenabled = false\n', encoding="utf-8")
                user.write_text(original_roles + '[roles.builder]\nproviders = ["codex", "claude:work"]\n', encoding="utf-8")
                user.write_text(user.read_text() + '[roles.executor]\naccess = "full"\nproviders = ["codex", "claude:work"]\n', encoding="utf-8")
                capture("roles-show-panes", ["roles", "show"])
                local.write_text(local_text, encoding="utf-8")
                user.write_text(original_roles, encoding="utf-8")
                original_settings = user.read_text()
                user.write_text(original_settings.replace('name = "work"',
                    'name = "work"\nmodels = ["sonnet"]\nmax_effort = "medium"'), encoding="utf-8")
                from agents_router.runtimes.claude import ClaudeRuntime
                with patch.object(ClaudeRuntime, "discover_models", return_value=("sonnet", "opus")):
                    capture("roles-show-discovered", ["roles", "show", "--discover-models"])
                user.write_text(original_settings, encoding="utf-8")
                capture("roles-config", ["roles", "config", "--set-model", "claude:work=opus", "--user"])
                capture("roles-config-error", ["roles", "config", "--set-effort", "claude:work=invalid", "--user"], 2)
                with patch("agents_router.hosts.detect_host", return_value="claude"):
                    capture("roles-config-needs-user-words", ["roles", "config", "--disable", "reviewer", "--user"], 3)
                for folder in (home / ".claude", home / ".codex"):
                    (folder / "agents").mkdir(parents=True, exist_ok=True)
                (home / ".claude" / "agents" / "example.md").write_text(
                    '---\nname: example\ndescription: Example agent\n---\nHelp with examples.\n', encoding="utf-8")
                os.utime(home / ".claude" / "agents" / "example.md", (NOW - 60, NOW - 60))
                from agents_router.share import settings
                capture("share-status-off", ["share", "status"])
                saved = settings.Settings(enabled=True, watcher=False, push_on_save=False,
                                          folders=[settings.Folder("claude", home / ".claude", "claude"),
                                                   settings.Folder("codex", home / ".codex", "codex")])
                settings.save(saved)
                capture("share-list", ["share", "list"])
                conflict = home / ".codex" / "agents" / "example.toml"
                conflict.write_text('name = "example"\ndescription = "Example agent"\n'
                                    'developer_instructions = "A different example agent."\n', encoding="utf-8")
                os.utime(conflict, (NOW - 60, NOW - 60))
                capture("share-status", ["share", "status"], 1)
                conflict.unlink()
                capture("share-pick-needs-choice", ["share", "pick"], 3)
                share_inputs(home)
                saved.mcp, saved.hooks, saved.plugins, saved.instructions = True, True, True, "block"
                settings.save(saved)
                capture("share-list-kinds", ["share", "list"])
                # A fixed run id sequence keeps share backups and run references byte-stable.
                with patch("agents_router.share.apply.new_run_id", side_effect=[f"fixture-run-{i}" for i in range(20)]):
                    capture("share-pick-not-in", ["share", "pick", "agents/example", "--not-in", "codex"])
                    capture("share-pick-in", ["share", "pick", "agents/example", "--in", "codex"])
                    capture("share-kinds-remove", ["share", "kinds", "remove", "mcp"])
                    capture("share-kinds-add", ["share", "kinds", "add", "mcp"])
                    capture("share-kinds-unchanged", ["share", "kinds", "add", "mcp"])
                # A real producer-backed single-item sync with a backup, then undo.
                with patch("agents_router.share.apply.new_run_id", side_effect=["fixture-backed-sync", "fixture-backed-undo"]):
                    source = home / ".claude/agents/example.md"
                    source.write_text(source.read_text() + "Updated example.\n", encoding="utf-8")
                    os.utime(source, (NOW - 60, NOW - 60))
                    capture("share-sync-backed", ["share", "sync"])
                    capture("activity-log-backed", ["log", "--all", "--limit", "200"])
                    capture("share-undo", ["share", "undo", "agents/example"])
                saved = settings.load()
                saved.auto_copy = ()
                settings.save(saved)
                waiting = home / ".claude/agents/waiting.md"
                waiting.write_text('---\nname: waiting\ndescription: Waiting agent\n---\nWait for a pick.\n', encoding="utf-8")
                os.utime(waiting, (NOW - 60, NOW - 60))
                capture("share-status-waiting", ["share", "status"])
                # Relative home paths keep config's content revision deterministic.
                user.write_text(user.read_text().replace(str(home), "~"), encoding="utf-8")
                capture("config-show-sharing", ["config", "show", "--all"])
                # Log producers supply the event shapes. Synthetic failure input never runs a tool.
                from types import SimpleNamespace
                from agents_router.share import apply
                apply._log(SimpleNamespace(items=[]), apply.Result(run="fixture-failure", outcomes=[
                    apply.Outcome("agents", "example", ["codex"], "copy", home / ".codex/agents/example.toml",
                                  False, "Example write failure")]), "agt share sync", 12345.0, None)
                from agents_router import levels
                synclog.refused(levels.SettingsError("Example invalid setting", file=project / ".agt/settings.local.toml", line=2), None)
                capture("activity-log", ["log", "--all", "--limit", "200"])
        finally:
            os.chdir(previous_cwd)
        return result


if __name__ == "__main__":
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, text in fixture_texts().items():
        (FOLDER / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote tests/fixtures/app/{name}")
