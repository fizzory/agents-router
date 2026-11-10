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

            data = normalize(json.loads(out.getvalue()))
            text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            result[name + ".json"] = text

        try:
            os.chdir(project)
            with patch.dict(os.environ, env), patch("time.time", return_value=NOW), \
                    patch("agents_router.hosts.detect_host", return_value="unknown"), patch("sys.stdin.isatty", return_value=False):
                capture("config-show", ["config", "show"])
                capture("config-set", ["config", "set", "max_depth", "2", "--user"])
                entry = next(synclog.entries())["id"]
                capture("config-set-error", ["config", "set", "codex.usage_ceiling", "wrong", "--user"], 2)
                capture("config-unset-absent", ["config", "unset", "kiro.default_model", "--user"])
                capture("accounts-list", ["accounts", "list"])
                capture("accounts-check", ["accounts", "check", "primary"])
                capture("accounts-add-check", ["accounts", "add", "spare", "--check"])
                capture("accounts-enable-refusal", ["accounts", "enable", "primary"], 2)
                capture("config-revert", ["config", "revert", entry])
                capture("accounts-disable", ["accounts", "disable", "work"])
                capture("accounts-enable", ["accounts", "enable", "work"])
                user = agt / "settings.toml"
                user.write_text('[claude]\nprimary = "~/.claude"\ninclude_primary = true\n'
                                '[[claude.accounts]]\nname = "work"\nhome = "~/accounts/work"\n'
                                '[roles.reviewer]\nproviders = ["claude:work/sonnet:high"]\n', encoding="utf-8")
                capture("roles-show", ["roles", "show"])
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
        finally:
            os.chdir(previous_cwd)
        return result


if __name__ == "__main__":
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, text in fixture_texts().items():
        (FOLDER / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote tests/fixtures/app/{name}")
