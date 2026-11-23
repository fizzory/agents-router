"""agt's usage tap in front of a Claude Code profile's status line: wrap, recognise, restore.

Claude Code runs a profile's `statusLine.command` with `/bin/sh` on each message and passes it a JSON
payload that carries the login's 5-hour and weekly usage. With `usage.statusline` on, agt puts a small
POSIX wrapper in front of that command:

    : agt-usage-tap v1; __agt_o=<yours>; __agt_l=<agt-run>; __agt_in=$(cat; printf .); ...

Your command runs in the foreground, in the same shell, on the same input, so the status line keeps its
bytes and exit code. `agt-run hook statusline` (`usage_tap.py`) runs in the background with its output
thrown away, and only when the launcher is there. A profile with no status line gets the wrapper with an
empty command, which prints nothing (`WRAP_EMPTY`).

- The marker `agt-usage-tap v1` (or a later version's) anywhere in the command means it's agt's: it is
  never wrapped twice.
- Restoring needs the whole command in the form agt wrote; it gives back the command inside it as it is
  now, so an edit you made there survives. A command that carries the marker in any other form is left
  as it is and reported.
- Only `statusLine` changes, and of it only `command` (or the whole key, when agt added it). The rest of
  `settings.json` keeps its bytes: agt edits the text in place instead of rewriting the JSON, backs the
  file up first (`state/statusline-backups/`) and writes through a link to it.
- Folders under `<AGT_HOME>/homes/` (agt's worker homes) are never wrapped, and nothing is on Windows,
  where Claude Code runs the status line in Git Bash or PowerShell (`unsupported`).
- `state/statusline.json` lists the files agt wrapped, so turning the setting off restores each one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import json
import os
from pathlib import Path
import shlex
import time
from typing import Any, Iterable

from agents_router import fsutil
from agents_router.config import homes_dir, state_dir
from agents_router.ui import short_path as _short

MARKER = "agt-usage-tap v1"
_ANY_MARKER = "agt-usage-tap v"  # a later wrapper is agt's too: left alone, never wrapped again
# A profile with no status line gets the tap with an empty command (it prints nothing). One switch, so
# the live check can turn it into "leave such profiles alone" if Claude Code shows a blank row for it.
WRAP_EMPTY = True

_HEAD = f": {MARKER}; __agt_o="
_MID = "; __agt_l="
_TAIL = ('; __agt_in=$(cat; printf .); __agt_in=${__agt_in%.}; if [ -x "$__agt_l" ]; then '
         '{ printf %s "$__agt_in" | "$__agt_l" hook statusline; } >/dev/null 2>&1 & fi; '
         'printf %s "$__agt_in" | eval "$__agt_o"')
_PLAIN = frozenset("@%+=:,./-_")  # unquoted characters a word may hold besides letters and digits
_WS = " \t\n\r"
_DECODER = json.JSONDecoder()


class StatuslineError(RuntimeError):
    """A settings file agt can't read or edit safely; it's left as it is."""


def _windows() -> bool:
    return os.name == "nt"


# The wrapper ---------------------------------------------------------------------------


def wrapped_command(original: str, launcher: str | Path | None = None) -> str:
    """The status line command that runs `original` with the tap in front (the launcher: agt-run by default)."""

    if launcher is None:
        from agents_router.share import triggers

        launcher = triggers.launcher_path()
    return f"{_HEAD}{shlex.quote(original)}{_MID}{shlex.quote(str(launcher))}{_TAIL}"


def is_ours(command: Any) -> bool:
    return isinstance(command, str) and _ANY_MARKER in command


def _word(text: str, start: int) -> int:
    """End of the shell word at `start`, or -1. Only forms whose value `sh` and `shlex` agree on: single quotes,
    double quotes without `$`, backquote or backslash, a backslash before one character, and plain characters.
    One pass, no backtracking."""

    index, size = start, len(text)
    while index < size:
        char = text[index]
        if char == "'":
            close = text.find("'", index + 1)
            if close < 0:
                return -1
            index = close + 1
        elif char == '"':
            index += 1
            while index < size and text[index] not in '"$`\\':
                index += 1
            if index >= size or text[index] != '"':
                return -1
            index += 1
        elif char == "\\":
            if index + 1 >= size or text[index + 1] == "\n":
                return -1
            index += 2
        elif char.isalnum() or char in _PLAIN:
            index += 1
        else:
            break
    return index if index > start else -1


def parse(command: Any) -> tuple[str, str] | None:
    """(your command, launcher) when `command` is exactly agt's wrapper, else None."""

    if not isinstance(command, str) or not command.startswith(_HEAD) or not command.endswith(_TAIL):
        return None
    start = len(_HEAD)
    end = _word(command, start)
    if end < 0 or not command.startswith(_MID, end):
        return None
    launcher_start = end + len(_MID)
    launcher_end = _word(command, launcher_start)
    if launcher_end != len(command) - len(_TAIL):
        return None
    try:
        original = shlex.split(command[start:end])
        launcher = shlex.split(command[launcher_start:launcher_end])
    except ValueError:
        return None
    if len(original) != 1 or len(launcher) != 1:
        return None
    return original[0], launcher[0]


# Editing settings.json in place -------------------------------------------------------------


def _skip(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WS:
        index += 1
    return index


@dataclass
class _Member:
    key: str
    key_start: int
    value_start: int
    value_end: int


def _members(text: str, start: int) -> tuple[list[_Member], int]:
    """The members of the JSON object whose `{` is at `start`, with their spans, and the index of its `}`."""

    if text[start:start + 1] != "{":
        raise ValueError("not an object")
    members: list[_Member] = []
    index = _skip(text, start + 1)
    if text[index:index + 1] == "}":
        return members, index
    while True:
        if text[index:index + 1] != '"':
            raise ValueError("expected a key")
        key, after = json.decoder.scanstring(text, index + 1)
        colon = _skip(text, after)
        if text[colon:colon + 1] != ":":
            raise ValueError("expected ':'")
        value_start = _skip(text, colon + 1)
        _, value_end = _DECODER.raw_decode(text, value_start)
        members.append(_Member(key, index, value_start, value_end))
        index = _skip(text, value_end)
        if text[index:index + 1] == ",":
            index = _skip(text, index + 1)
            continue
        if text[index:index + 1] == "}":
            return members, index
        raise ValueError("expected ',' or '}'")


def _last(members: list[_Member], key: str) -> _Member | None:
    return next((member for member in reversed(members) if member.key == key), None)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _insert_member(text: str, start: int, key: str, value: Any) -> str:
    """`text` with `"key": value` added at the end of the object at `start`, in the file's own layout."""

    members, close = _members(text, start)
    newline = _newline(text)
    if members:
        first = members[0].key_start
        line_start = text.rfind("\n", 0, first) + 1
        unit = text[line_start:first] if "\n" in text[start:close] else ""
        if unit.strip(" \t"):
            unit = ""
        if not unit:  # one line: keep it on one line
            member = f", {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
        else:
            rendered = json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", newline + unit)
            member = f",{newline}{unit}{json.dumps(key)}: {rendered}"
        at = members[-1].value_end
        return text[:at] + member + text[at:]
    unit = "  "
    rendered = json.dumps(value, indent=unit, ensure_ascii=False).replace("\n", newline + unit)
    return text[:start + 1] + f"{newline}{unit}{json.dumps(key)}: {rendered}{newline}" + text[close:]


def _remove_member(text: str, start: int, key: str) -> str:
    """`text` without the last `key` of the object at `start`: the exact reverse of `_insert_member`."""

    members, close = _members(text, start)
    index = max(i for i, member in enumerate(members) if member.key == key)
    if len(members) == 1:
        return text[:start + 1] + text[close:]
    if index > 0:
        return text[:members[index - 1].value_end] + text[members[index].value_end:]
    return text[:members[0].key_start] + text[members[1].key_start:]


def _replace_value(text: str, member: _Member, value: Any) -> str:
    return text[:member.value_start] + json.dumps(value, ensure_ascii=False) + text[member.value_end:]


def _read(path: Path) -> tuple[dict[str, Any], str, bool]:
    """(settings, their text, whether the file exists). Bytes are decoded as they are: CRLF stays CRLF."""

    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return {}, "", False
    except (OSError, UnicodeDecodeError) as exc:
        raise StatuslineError(f"can't read {_short(path)} ({exc})") from None
    if not text.strip():
        return {}, text, True
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StatuslineError(f"{_short(path)} isn't valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise StatuslineError(f"{_short(path)} doesn't hold a JSON object")
    return data, text, True


def _top(text: str) -> int:
    start = _skip(text, 0)
    if text[start:start + 1] != "{":
        raise ValueError("not an object")
    return start


def _checked(text: str, expected: dict[str, Any], path: Path) -> str:
    try:
        same = json.loads(text) == expected
    except ValueError:
        same = False
    if not same:
        raise StatuslineError(f"agt couldn't edit {_short(path)} without changing more than its status line")
    return text


def _backup(path: Path, text: str, label: str) -> Path:
    """A copy of the file before agt edits it. Never overwrites an earlier copy, so the first one stays."""

    folder = backups_dir()
    fsutil.make_private_dir(folder)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for count in itertools.count():
        target = folder / f"{stamp}{f'-{count}' if count else ''}-{label}-{path.name}"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
        return target
    raise AssertionError("unreachable")


def _write(path: Path, text: str, exists: bool) -> None:
    data = text.encode("utf-8")
    if exists:
        fsutil.write_preserving(path, data)
    else:
        fsutil.write_preserving(path, data, new=True)


def _check_unchanged(path: Path, text: str, exists: bool) -> None:
    """Refuse an edit if another settings writer changed the snapshot we used, including its existence."""

    if _read(path)[1:] != (text, exists):
        raise StatuslineError(f"{_short(path)} changed while agt was editing it; try again")


def _same_file(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _worker_home(folder: Path) -> bool:
    """Whether `folder` is one of agt's worker homes (under `<AGT_HOME>/homes/`), which are never wrapped."""

    try:
        Path(os.path.realpath(Path(folder).expanduser())).relative_to(os.path.realpath(homes_dir().expanduser()))
        return True
    except ValueError:
        return False


# One file ----------------------------------------------------------------------------


@dataclass
class Outcome:
    """What wrapping or restoring one file did: `wrapped`, `relaunched` (a new launcher path), `unchanged`,
    `restored`, `absent` (nothing of agt's there), `skipped` (worker home; no status line with WRAP_EMPTY off)
    or `problem` (`message` says why; the file is left as it is). `created`: agt made the file."""

    action: str
    message: str = ""
    created: bool = False


def wrap_file(path: Path, label: str, launcher: str | Path) -> Outcome:
    """Put the tap in front of the status line in `path` (a profile's settings.json)."""

    folder = path.parent
    if _windows() or _worker_home(folder) or not Path(folder).is_dir():
        return Outcome("skipped")  # a profile folder that isn't there gets no settings.json from agt
    launcher = str(launcher)
    try:
        data, text, exists = _read(path)
        line = data.get("statusLine")
        if line is None:  # no key, or `null`: no status line either way
            if not WRAP_EMPTY:
                return Outcome("skipped")
            value = {"type": "command", "command": wrapped_command("", launcher)}
            if "statusLine" in data:  # take the `null`'s place: never two keys of one name
                new = _replace_value(text, _last(_members(text, _top(text))[0], "statusLine"), value)
            else:
                source = text if text.strip() else "{}" + _newline(text)
                new = _insert_member(source, _top(source), "statusLine", value)
            expected = {**data, "statusLine": value}
            action, created = "wrapped", not exists
        else:
            if not isinstance(line, dict) or not isinstance(line.get("command"), str):
                return Outcome("problem", f"the statusLine in {_short(path)} has no command; it was left as it is")
            command = line["command"]
            if is_ours(command):
                parsed = parse(command)
                if parsed is None:
                    return Outcome("problem", f"the status line in {_short(path)} carries agt's tap, but not in the "
                                              "form agt writes; it was left as it is")
                if _same_file(parsed[1], launcher):
                    return Outcome("unchanged")
                updated = wrapped_command(parsed[0], launcher)
                action = "relaunched"
            else:
                updated = wrapped_command(command, launcher)
                action = "wrapped"
            top = _top(text)
            members, _ = _members(text, top)
            span = _last(members, "statusLine")
            inner, _ = _members(text, span.value_start)
            new = _replace_value(text, _last(inner, "command"), updated)
            expected = {**data, "statusLine": {**line, "command": updated}}
            created = False
        _checked(new, expected, path)
        if exists and text:
            _backup(path, text, label)
        _check_unchanged(path, text, exists)
        _write(path, new, exists)
        return Outcome(action, created=created)
    except StatuslineError as exc:
        return Outcome("problem", f"{exc}; it was left as it is")
    except (ValueError, OSError) as exc:
        return Outcome("problem", f"agt couldn't edit {_short(path)} ({exc}); it was left as it is")


def unwrap_file(path: Path, label: str, *, created: bool = False) -> Outcome:
    """Give back the status line the tap runs, as the file is now; nothing else in it changes.

    The key goes when agt added it (the command inside is empty and nothing else was added to it), and the
    file goes when agt made it and nothing else is left in it.
    """

    try:
        data, text, exists = _read(path)
        line = data.get("statusLine")
        if not exists or not isinstance(line, dict) or not is_ours(line.get("command")):
            return Outcome("absent")
        parsed = parse(line["command"])
        if parsed is None:
            return Outcome("problem", f"the status line in {_short(path)} carries agt's tap, but not in the form "
                                      "agt writes, so agt can't tell what to give back; it was left as it is")
        original = parsed[0]
        top = _top(text)
        if original == "" and set(line) <= {"type", "command"} and line.get("type", "command") == "command":
            new = _remove_member(text, top, "statusLine")
            expected = {key: value for key, value in data.items() if key != "statusLine"}
            if created and not expected and not os.path.islink(path):
                _checked(new, expected, path)
                _backup(path, text, label)
                _check_unchanged(path, text, exists)
                path.unlink()
                return Outcome("restored")
        else:
            members, _ = _members(text, top)
            inner, _ = _members(text, _last(members, "statusLine").value_start)
            new = _replace_value(text, _last(inner, "command"), original)
            expected = {**data, "statusLine": {**line, "command": original}}
        _checked(new, expected, path)
        _backup(path, text, label)
        _check_unchanged(path, text, exists)
        _write(path, new, True)
        return Outcome("restored")
    except StatuslineError as exc:
        return Outcome("problem", f"{exc}; it was left as it is")
    except (ValueError, OSError) as exc:
        return Outcome("problem", f"agt couldn't edit {_short(path)} ({exc}); it was left as it is")


# Where agt keeps track -------------------------------------------------------------------


def record_path() -> Path:
    return state_dir() / "statusline.json"


def backups_dir() -> Path:
    return state_dir() / "statusline-backups"


def _lock_path() -> Path:
    return state_dir() / "statusline.lock"


def recorded() -> list[dict[str, Any]]:
    """The settings files agt wrapped: {"label", "path", "created"} each."""

    data = fsutil.read_json(record_path(), {})
    files = data.get("files") if isinstance(data, dict) else None
    return [entry for entry in files or [] if isinstance(entry, dict) and isinstance(entry.get("path"), str)]


def _save_record(files: list[dict[str, Any]]) -> bool:
    value = {"files": files}
    if fsutil.read_json(record_path(), None) == value:
        return False  # the app reads usage again on any change under state/
    if not files and not record_path().exists():
        return False
    fsutil.make_private_dir(state_dir())
    fsutil.write_json(record_path(), value)
    return True


def settings_path(folder: Path) -> Path:
    return Path(folder).expanduser() / "settings.json"


# The state of a profile -------------------------------------------------------------------


@dataclass(frozen=True)
class Inspection:
    """A profile's tap state, whether agt can repair it, and the settings file a person can edit. Reads only."""

    state: str | None
    repairable: bool
    settings_path: Path


def inspect(folder: Path, *, enabled: bool | None = True) -> Inspection:
    """`live` (the tap runs through the current launcher), `missing` (no tap, or one through a launcher that
    isn't the current one), `replaced` (agt wrapped it, and the tap is gone or no longer in agt's form),
    `unsupported` (Windows), or None for agt's worker homes. Reads only: it takes no lock and writes nothing.
    Consent (`enabled`) is checked before reading settings; usage readers may add `no_limits`."""

    path = settings_path(folder)
    if _windows():
        return Inspection("unsupported", False, path)
    if _worker_home(folder):
        return Inspection(None, False, path)
    if enabled is None:
        return Inspection("unset", False, path)
    if enabled is False:
        return Inspection("off", False, path)
    try:
        data, _, _ = _read(path)
    except StatuslineError:
        data = {}
    line = data.get("statusLine")
    command = line.get("command") if isinstance(line, dict) else None
    if is_ours(command):
        parsed = parse(command)
        if parsed is None:
            return Inspection("replaced", False, path)
        from agents_router.share import triggers

        launcher = parsed[1]
        live = _same_file(launcher, triggers.launcher_path()) and os.access(launcher, os.X_OK)
        return Inspection("live" if live else "missing", True, path)
    was_wrapped = any(_same_file(entry["path"], path) for entry in recorded())
    return Inspection("replaced" if was_wrapped else "missing", True, path)


def state(folder: Path) -> str | None:
    """The installed tap's state, without applying a consent choice. Reads only."""

    return inspect(folder).state


# Every profile -------------------------------------------------------------------------------


def managed_profiles(config: Any) -> list[Any]:
    """The Claude Code profiles `refresh` wraps: every profile `agt setup` installs the plugin into
    (`setup.claude_profiles`, found profiles of unknown login too when there's no `claude` command), never
    agt's worker homes."""

    from agents_router.runtimes import get_runtime
    from agents_router.setup import claude_profiles

    runtime_config = config.runtime("claude")
    unknown = get_runtime("claude").find_binary(runtime_config) is None
    return [account for account in claude_profiles(config, unknown=unknown) if not _worker_home(account.home)]


def _folder(item: Any) -> Path:
    """An account's folder, or the folder itself (a Path has a `home` of its own, so check the type first)."""

    return Path(item if isinstance(item, (str, os.PathLike)) else item.home).expanduser()


def _label(item: Any) -> str:
    from agents_router import profiles

    name = None if isinstance(item, (str, os.PathLike)) else getattr(item, "name", None)
    return name if name and name != "primary" else profiles.label(_folder(item))


@dataclass
class Refreshed:
    """What `refresh` did. `lines`: one per profile it changed or couldn't change, for a person. `problems`:
    the files it left as they are, each with why. `changed`: a settings file changed. `unsupported`: Windows,
    so nothing was done."""

    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    changed: bool = False
    unsupported: bool = False


def refresh(config: Any, enabled: bool, profiles: Iterable[Any] | None = None) -> Refreshed:
    """Make every profile's status line match the setting.

    On: provisions `agt-run`, then wraps each profile (`managed_profiles(config)`, or `profiles`: accounts or
    folders), rewriting a wrapper whose launcher isn't the current one. Off: restores every file agt
    recorded, and any of those profiles that still carries the tap. A file agt can't read or parse is left
    as it is and named in `problems`. Nothing happens on Windows.
    """

    found = Refreshed()
    if _windows():
        found.unsupported = True
        return found
    if profiles is None:
        profiles = managed_profiles(config) if config is not None else []
    targets = [(_folder(item), _label(item)) for item in profiles]
    targets = [(folder, label) for folder, label in targets if not _worker_home(folder)]
    fsutil.make_private_dir(state_dir())
    try:
        with fsutil.file_lock(_lock_path(), timeout=30):
            if enabled:
                _wrap_all(targets, found)
            else:
                _unwrap_all(targets, found)
    except TimeoutError:
        _problem(found, "status line", "another agt run is changing status lines; nothing changed, try again")
    return found


def _problem(found: Refreshed, label: str, message: str) -> None:
    line = f"{label}: {message[0].upper()}{message[1:]}."
    found.lines.append(line)
    found.problems.append(line)


def _wrap_all(targets: list[tuple[Path, str]], found: Refreshed) -> None:
    from agents_router.share import triggers

    launcher, _ = triggers.write_launcher()
    files = recorded()
    kept: list[dict[str, Any]] = []
    for folder, label in targets:
        path = settings_path(folder)
        if any(_same_file(entry["path"], path) for entry in kept):
            continue
        before = next((entry for entry in files if _same_file(entry["path"], path)), None)
        outcome = wrap_file(path, label, launcher)
        if outcome.action == "problem":
            _problem(found, label, outcome.message)
            if before is not None:
                kept.append(before)
            continue
        if outcome.action == "skipped":
            continue
        created = outcome.created if outcome.action == "wrapped" else bool(before and before.get("created"))
        kept.append({"label": label, "path": str(path), "created": created})
        if outcome.action == "wrapped":
            found.lines.append(f"{label}: the status line records usage for agt, then runs yours ({_short(path)}).")
        elif outcome.action == "relaunched":
            found.lines.append(f"{label}: the status line's usage tap now runs {_short(launcher)} ({_short(path)}).")
        found.changed = found.changed or outcome.action in ("wrapped", "relaunched")
    for entry in files:  # wrapped earlier, not among these profiles now: stays wrapped, and on record
        if not any(_same_file(entry["path"], other["path"]) for other in kept):
            kept.append(entry)
    _save_record(kept)


def _unwrap_all(targets: list[tuple[Path, str]], found: Refreshed) -> None:
    files = recorded()
    todo = list(files)
    for folder, label in targets:  # the record may be gone; a profile that carries the tap is restored too
        path = settings_path(folder)
        if any(_same_file(entry["path"], path) for entry in todo):
            continue
        try:
            data, _, _ = _read(path)
        except StatuslineError:
            continue
        line = data.get("statusLine")
        if isinstance(line, dict) and is_ours(line.get("command")):
            todo.append({"label": label, "path": str(path), "created": False})
    kept: list[dict[str, Any]] = []
    for entry in todo:
        path, label = Path(entry["path"]), str(entry.get("label") or "claude")
        outcome = unwrap_file(path, label, created=bool(entry.get("created")))
        if outcome.action == "problem":
            _problem(found, label, outcome.message)
            if entry in files:
                kept.append(entry)
            continue
        if outcome.action == "restored":
            found.lines.append(f"{label}: the status line is yours again ({_short(path)}).")
            found.changed = True
    _save_record(kept)
