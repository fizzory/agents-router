"""Items that are never shared, and why.

Each check returns the reason shown in `status` and the setup list, or None.
"""

from __future__ import annotations

import os
from pathlib import Path
import tomllib

OWN_AGENT_PREFIX = "agt-"  # agt's relay agents
OWN_SKILL = "agents-router"
NOT_SHARED_SKILLS = {"synced": "skills/synced is never shared"}
PLUGIN_PARTS = "plugin-parts"  # the folder under agt's share state that holds the plugin mirrors
# Agents that rely on tools only KiroCrew has.
KIROCREW_PREFIX = "kirocrew"
KIROCREW_ONLY = {"ops-reporter"}


def agent(cli: str, name: str, path: Path, data: dict | None = None) -> str | None:
    if name.startswith("."):
        return "hidden file"
    if name.startswith(OWN_AGENT_PREFIX):
        return "agt's own relay agent"
    if name.lower().startswith(KIROCREW_PREFIX):
        return "a KiroCrew agent"
    if name in KIROCREW_ONLY:
        return "relies on KiroCrew-only tools"
    if cli == "kiro":
        if path.with_suffix(".lock").exists():
            return f"managed by {path.with_suffix('.lock').name}"
        if data is not None and str(data.get("prompt") or "").strip().lower().startswith("file://"):
            return "its prompt is a file:// link"
    return None


def plugin_parts_root() -> Path:
    """Where agt keeps the mirrors of your plugins' skills and commands (`share/plugin_parts.py`), in any scope."""

    from agents_router.share import scope

    return scope.share_root() / PLUGIN_PARTS


def plugin_part(path: Path, root: Path | None = None) -> str | None:
    """The plugin (`name@marketplace`) whose mirror a link at `path` leads into, or None for anything else.

    It reads the link itself, so a link that dangles for a moment (its mirror is being rebuilt) is still agt's.
    """

    try:
        if not os.path.islink(path) and not _junction(path):
            return None
        target = os.readlink(path)
    except (OSError, ValueError):
        return None
    root = root if root is not None else plugin_parts_root()
    leads = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), target))
    for found in (leads, os.path.realpath(path)):
        for base in (os.path.normpath(os.path.abspath(root)), os.path.realpath(root)):
            if found.startswith(base + os.sep):
                parts = Path(os.path.relpath(found, base)).parts
                return f"{parts[1]}@{parts[0]}" if len(parts) >= 2 else "a plugin"
    return None


def _junction(path: Path) -> bool:
    if os.name != "nt":
        return False
    from agents_router import fsutil

    return fsutil.is_link(Path(path))


def skill(name: str, folder: Path, disabled: set[str] | None = None) -> str | None:
    if name.startswith("."):
        return "hidden folder"
    plugin = plugin_part(folder)
    if plugin:
        return f"part of the {plugin} plugin; agt links it into Kiro only"
    if name in NOT_SHARED_SKILLS:
        return NOT_SHARED_SKILLS[name]
    if name == OWN_SKILL or name.startswith(OWN_AGENT_PREFIX):
        return "agt's own skill"
    if not (folder / "SKILL.md").is_file():
        return "no SKILL.md"
    if disabled and _real(folder) in disabled:
        return "disabled in Codex"
    return None


def codex_disabled_skills(codex_home: Path) -> set[str]:
    """Real paths of skill folders that Codex's config.toml turns off (`[[skills.config]]`, enabled = false)."""

    try:
        data = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    entries = (data.get("skills") or {}).get("config") if isinstance(data.get("skills"), dict) else None
    found = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("enabled") is False and entry.get("path"):
            path = Path(str(entry["path"])).expanduser()
            found.add(_real(path.parent if path.name == "SKILL.md" else path))
    return found


def _real(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
