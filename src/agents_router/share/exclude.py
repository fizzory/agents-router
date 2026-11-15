"""Items that are never shared, and why.

Each check returns the reason shown in `status` and the setup list, or None.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import tomllib

from agents_router import fsutil

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
        if not fsutil.is_link(Path(path)):
            return None
        target = os.readlink(path)
    except (OSError, ValueError):
        return None
    root = root if root is not None else plugin_parts_root()
    leads = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), target))
    for found, base in _pairs(path, leads, root):
        if found.startswith(base + os.sep):
            parts = Path(os.path.relpath(found, base)).parts
            return f"{parts[1]}@{parts[0]}" if len(parts) >= 2 else "a plugin"
    return None


def _pairs(path: Path, leads: str, root: Path) -> Iterator[tuple[str, str]]:
    """(where the link leads, the mirrors' root) to compare: the lexical paths first, and the real ones, which cost
    a `realpath` each, only when those don't match."""

    lexical = os.path.normpath(os.path.abspath(root))
    yield leads, lexical
    real = os.path.realpath(root)
    yield leads, real
    resolved = os.path.realpath(path)
    yield resolved, lexical
    yield resolved, real


def skill(name: str, folder: Path, disabled: set[str] | None = None) -> str | None:
    found = skill_exclusion(name, folder, disabled)
    return found[0] if found else None


def skill_exclusion(name: str, folder: Path, disabled: set[str] | None = None) -> tuple[str, str | None] | None:
    """`skill`'s reason, with the kind of item that stands for the skill instead (`PLUGIN_PARTS` for a link into a
    plugin's mirror, else None), or None for a skill that's shared."""

    if name.startswith("."):
        return "hidden folder", None
    plugin = plugin_part(folder)
    if plugin:
        return f"part of the {plugin} plugin; agt links it into Kiro only", PLUGIN_PARTS
    if name in NOT_SHARED_SKILLS:
        return NOT_SHARED_SKILLS[name], None
    if name == OWN_SKILL or name.startswith(OWN_AGENT_PREFIX):
        return "agt's own skill", None
    if not (folder / "SKILL.md").is_file():
        return "no SKILL.md", None
    if disabled and _real(folder) in disabled:
        return "disabled in Codex", None
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
