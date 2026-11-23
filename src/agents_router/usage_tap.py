"""Claude Code's status line tap: `agt hook statusline`.

agt's wrapper in front of a profile's `statusLine.command` pipes Claude Code's status line JSON here in the
background, while the person's own status line runs in the foreground on the same input. The launcher runs this
file with `python -I` (agt's own entry point reaches `main` too): the standard library only, never the rest of agt,
so it starts fast.

It keeps the profile's current 5-hour and weekly figures in `<AGT_HOME>/state/claude-live/<folder key>.json`:

    {"login": "3f2a9c...", "limits": true,
     "windows": {"five_hour": {"utilization": 0.62, "resets_at": 1763900000.0, "observed_at": 1763890000.5}, ...},
     "superseded": {"five_hour": {"utilization": 0.4, "resets_at": 1763903600.0}, ...}}

- The folder is `CLAUDE_CONFIG_DIR`, else `~/.claude`, keyed as `accounts._home_key` keys it. agt's worker homes
  (`<AGT_HOME>/homes/`) are skipped.
- `login` is the folder's login, hashed as `profiles.account_info` hashes `oauthAccount.accountUuid`, from the same
  `.claude.json`. It is read only once a write could be due, once per write, and never printed. No login, no write.
  Figures belong to the login in the file: when the folder's login is another one, the file is replaced, not merged,
  and the old login's last figures move to `superseded` (values only). `login_since` records when the switch was
  first seen; windows from transcripts last written before it are ignored. An incoming window in the same window (reset
  minute) as one of those, and no more used, is never written while that window lasts: within one window
  utilization only rises, so it's the old login's reading from before the new login's first reply, in this session
  or another one. Superseded windows that have reset are pruned on every write.
- `limits` is true once a window is recorded for the login. It is false only for a login never seen with windows
  here, when `rate_limits` is absent and `prompt_cache` is present (a reply came back without usage limits). It
  stays true when a Max login's windows all reset and drop out of the payload. A file replaced for a new login
  before its first window or limits answer has no `limits` (not known yet).
- `used_percentage` (0-100) becomes `utilization` (0-1). `observed_at` is the earlier of now and the transcript's
  last write. A window missing from the payload, or an empty `rate_limits`, is no news (Claude Code drops a window
  once it resets), never 0%. `spend_limit` is a gateway limit, not a 5-hour or weekly window: ignored.
- It reads the live file and compares before taking any lock (or reading `.claude.json`), so an unchanged payload
  writes nothing anywhere under `state/` (the menu bar app re-reads usage on any change there). A window is news
  when it beats the stored one in the total order (`window_key`, also used by `window_order`) and its figures changed
  or its observation moved forward `REFRESH_SECONDS` or more. `.claude.json` is read only when a window isn't a
  superseded login's and is news or differs from the stored one (which may be another login's), or when
  `limits: false` could be due (the file may name another login). A write re-reads and merges under a per-file
  lock, so two sessions of one profile never overwrite each other's newer window; it waits at most `LOCK_SECONDS`
  for the lock, then gives up.
- It never prints and always exits 0. Bad input or any error writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: agt never wraps a status line there, so the tap never writes
    fcntl = None  # type: ignore[assignment]

WINDOWS = ("five_hour", "seven_day")
REFRESH_SECONDS = 300
LOCK_SECONDS = 1.0
MAX_BYTES = 1 << 20
_MISSING = -math.inf


# The total order: window_order imports these rules from here ---------------------------------------------------


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _or_missing(value: Any) -> float:
    number = _finite(value)
    return _MISSING if number is None else number


def window_key(window: dict[str, Any], now: float) -> tuple:
    """The total order every merge uses; the greater key wins.

    A window that hasn't reset yet (or has no reset time) beats one that has. Then the reset time to the minute (a
    later window), the observation time (a newer reading of the same window, even a lower one after a mid-window
    limit reset), and the utilization. The full content comes last, so equal keys mean equal windows and the result
    never depends on the order the sources arrive in.
    """

    resets = _finite(window.get("resets_at"))
    return (resets is None or resets > now,
            _MISSING if resets is None else round(resets / 60),
            _or_missing(window.get("observed_at")),
            _or_missing(window.get("utilization")),
            json.dumps(window, sort_keys=True, default=str))


# The payload ---------------------------------------------------------------------------------------------------


def observed_at(payload: dict[str, Any], now: float) -> float:
    """The earlier of now and the transcript's last write (the figures are as of the session's last reply)."""

    path = payload.get("transcript_path")
    if isinstance(path, str) and path:
        try:
            return min(now, os.stat(path).st_mtime)
        except (OSError, ValueError):
            pass
    return now


def windows_from(payload: Any, observed: float) -> dict[str, dict[str, Any]] | None:
    """The 5-hour and weekly windows in a status line payload, each stamped `observed`.

    {} when there are none (no news); None when the payload or any window in it is malformed (nothing is written).
    """

    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        return None
    found: dict[str, dict[str, Any]] = {}
    for name in WINDOWS:
        if name not in limits:
            continue
        window = limits[name]
        used = _finite(window.get("used_percentage")) if isinstance(window, dict) else None
        resets = _finite(window.get("resets_at")) if isinstance(window, dict) else None
        if used is None or used < 0 or resets is None or resets < 0:
            return None
        found[name] = {"utilization": used / 100, "resets_at": resets, "observed_at": observed}
    return found


# Where it goes ---------------------------------------------------------------------------------------------------


def agt_home() -> Path:
    """`config.home()`: `AGT_HOME` (as the launcher exports it), else `~/.agents-router`."""

    configured = os.environ.get("AGT_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".agents-router"


def profile_folder() -> Path:
    """The Claude Code profile running the status line: `CLAUDE_CONFIG_DIR`, else `~/.claude`."""

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def home_key(home: Path, *, resolved: Path | None = None) -> str:
    """`accounts._home_key`, copied: the same key for every name that points to the folder."""

    if resolved is None:
        try:
            resolved = Path(home).expanduser().resolve()
        except (OSError, RuntimeError):
            resolved = Path(os.path.abspath(str(home)))
    return hashlib.sha256(os.path.normcase(str(resolved)).encode("utf-8")).hexdigest()[:16]


def in_worker_home(folder: Path, home: Path, *, resolved: Path | None = None) -> bool:
    """Whether `folder` is one of agt's worker homes (`<AGT_HOME>/homes/...`), which agt keeps up to date itself."""

    real = os.path.normcase(str(resolved) if resolved is not None else os.path.realpath(folder))
    homes = os.path.normcase(os.path.realpath(home / "homes"))
    return real == homes or real.startswith(homes.rstrip(os.sep) + os.sep)


def live_path(home: Path, key: str) -> Path:
    return home / "state" / "claude-live" / f"{key}.json"


def lock_path(live: Path) -> Path:
    return live.with_suffix(".lock")


# The folder's login: `profiles.account_info`, copied (test_usage_tap pins the two together) -----------------------


def _canonical(path: Path) -> str:
    """`profiles.canonical`, as `profiles.same_folder` compares it."""

    path = Path(path).expanduser()
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = Path(os.path.abspath(path))
    return os.path.normcase(str(path))


def login_files(folder: Path) -> list[Path]:
    """The `.claude.json` files `profiles.account_info` reads, in its order: `~/.claude.json` first for `~/.claude`."""

    folder = Path(folder).expanduser()
    files = [folder / ".claude.json"]
    if _canonical(folder) == _canonical(Path.home() / ".claude"):
        files.insert(0, Path.home() / ".claude.json")
    return files


def _hash(value: Any) -> str | None:
    """`profiles._hash`: the first 12 hex digits of the value's sha256."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12] if value else None


def _read_regular(path: Path) -> bytes:
    """A regular file's bytes, following links like `read_text`; anything else (a FIFO) raises without blocking."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not a regular file")
        return handle.read()


def read_login(folder: Path) -> str | None:
    """The folder's login as `profiles.account_info` hashes it, or None when no `.claude.json` names one.

    `.claude.json` holds more than the login: nothing from it is kept but this hash, and it's never printed.
    """

    for path in login_files(folder):
        try:
            data = json.loads(_read_regular(path).decode("utf-8"))
        except (OSError, ValueError):
            continue
        account = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(account, dict) and account.get("accountUuid"):
            return _hash(account.get("accountUuid"))
    return None


# Deciding and writing ------------------------------------------------------------------------------------------------


def read_live(path: Path, *, metadata: list[os.stat_result] | None = None) -> dict[str, Any]:
    """The stored live file, or {} when it's missing, unreadable, not a regular file or not a JSON object.
    If requested, append the opened file's metadata so a reader can cache the exact file it parsed."""

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if metadata is not None:
            metadata.append(info)
        if not stat.S_ISREG(info.st_mode) or (metadata is not None and info.st_size > MAX_BYTES):
            return {}
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_BYTES:
            return {}
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except OSError:
        if metadata is not None:
            metadata.clear()  # a read failure is retried even if the file's metadata has not changed
        return {}
    except ValueError:
        return {}
    finally:
        os.close(descriptor)
    return data if isinstance(data, dict) else {}


def _stored_windows(stored: dict[str, Any]) -> dict[str, Any]:
    windows = stored.get("windows")
    return windows if isinstance(windows, dict) else {}


def _minute(window: dict[str, Any]) -> float | None:
    resets = _finite(window.get("resets_at"))
    return None if resets is None else round(resets / 60)


def _same_figures(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """The same utilization and the same reset minute (the total order's notion of one window)."""

    return _finite(left.get("utilization")) == _finite(right.get("utilization")) and _minute(left) == _minute(right)


def _news(window: dict[str, Any], old: dict[str, Any], now: float) -> bool:
    """Whether `window` is news over the stored `old` of its login: it beats it in the total order, and its figures
    changed or its observation moved forward `REFRESH_SECONDS` or more. An older or equal reading never is."""

    if window_key(window, now) <= window_key(old, now):
        return False
    moved = _or_missing(window.get("observed_at")) - _or_missing(old.get("observed_at"))
    return not _same_figures(window, old) or moved >= REFRESH_SECONDS


def _had_windows(stored: dict[str, Any]) -> bool:
    return stored.get("limits") is True or bool(_stored_windows(stored))


def _equals(old: Any, window: dict[str, Any]) -> bool:
    return isinstance(old, dict) and _same_figures(window, old)


def _not_after(old: Any, window: dict[str, Any]) -> bool:
    """Whether `window` could be a reading of an earlier login's last figures `old` taken at or before them: the same
    window (its reset minute) and no more used, since within one window utilization only rises. Another session of
    the earlier login shows such figures until its next reply."""

    if not isinstance(old, dict) or _minute(old) is None or _minute(window) != _minute(old):
        return False
    used, last = _finite(window.get("utilization")), _finite(old.get("utilization"))
    return used is not None and last is not None and used <= last


def _superseded(stored: dict[str, Any], now: float) -> dict[str, dict[str, Any]]:
    """The earlier logins' last figures whose window hasn't reset yet."""

    entries = stored.get("superseded")
    if not isinstance(entries, dict):
        return {}
    return {name: entry for name, entry in entries.items()
            if isinstance(entry, dict) and _or_missing(entry.get("resets_at")) > now}


def _after_switch(stored: dict[str, Any], incoming: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Only sessions that have replied since the current login was first seen can supply its figures."""

    since = _finite(stored.get("login_since"))
    return {name: window for name, window in incoming.items()
            if since is None or _or_missing(window.get("observed_at")) >= since}


def due(stored: dict[str, Any], incoming: dict[str, dict[str, Any]], now: float,
        login: str | None = None) -> dict[str, dict[str, Any]]:
    """The incoming windows worth writing over `stored` for the folder's `login`; {} means nothing to write.

    A window that is an earlier login's (`_not_after` its last figures: `superseded`, or the stored windows when
    `stored` is another login's) is never written: it's a session's reading from before the new login's first reply.
    Of the rest: for the login in `stored`, the windows that are news (`_news`); for another login, all of them. No
    login, nothing.
    """

    if login is None:
        return {}
    superseded = _superseded(stored, now)
    windows = _stored_windows(stored)
    incoming = {name: window for name, window in _after_switch(stored, incoming).items()
                if not _not_after(superseded.get(name), window)}
    if stored.get("login") != login:
        return {name: window for name, window in incoming.items() if not _not_after(windows.get(name), window)}
    return {name: window for name, window in incoming.items()
            if not isinstance(windows.get(name), dict) or _news(window, windows[name], now)}


def _replacement(stored: dict[str, Any], login: str, now: float) -> dict[str, Any]:
    """A fresh file for `login`, keeping only the figures it supersedes (values only, never another login's id)."""

    last = {name: {"utilization": window.get("utilization"), "resets_at": window.get("resets_at")}
            for name, window in _stored_windows(stored).items() if isinstance(window, dict)}
    value = {"login": login, "windows": {}, "superseded": {**_superseded(stored, now), **last}}
    if stored.get("login"):
        value["login_since"] = now
    return value


def updated(stored: dict[str, Any], incoming: dict[str, dict[str, Any]], now: float, login: str | None,
            no_limits: bool = False) -> dict[str, Any] | None:
    """The live file to write over `stored`, or None when there's nothing to write.

    Another login's file (or one without a login) is replaced whenever the payload says anything, even when every
    window in it is the old login's, so the file names the folder's login. `no_limits`: the payload has no
    `rate_limits` though a reply came back (`prompt_cache`); it writes `limits: false` only for a login never seen
    with windows here. `superseded` entries whose window has reset are pruned on every write.
    """

    if login is None or not (incoming or no_limits):
        return None
    fresh = stored.get("login") != login
    value = _replacement(stored, login, now) if fresh else dict(stored)
    news = due(stored, _after_switch(value, incoming), now, login)
    if news:
        value.update(limits=True, windows={**_stored_windows(value), **news})
    elif no_limits and not _had_windows(value) and value.get("limits") is not False:
        value["limits"] = False
    elif not fresh:
        return None  # sticky: a Max login whose windows all reset keeps `limits: true`
    superseded = _superseded(value, now)
    if superseded:
        value["superseded"] = superseded
    else:
        value.pop("superseded", None)
    return value


def might_write(stored: dict[str, Any], incoming: dict[str, dict[str, Any]], now: float,
                no_limits: bool = False) -> bool:
    """Whether a write could be due for whichever login the folder has, judged without reading `.claude.json`.

    False only when every window is an earlier login's (`_not_after` a superseded one) or the stored one (the same figures, and not
    news). `limits: false` always needs the login: the file is replaced when it names another one.
    """

    if incoming:
        superseded, windows = _superseded(stored, now), _stored_windows(stored)
        return any(not _not_after(superseded.get(name), window)
                   and (not _equals(windows.get(name), window) or _news(window, windows[name], now))
                   for name, window in _after_switch(stored, incoming).items())
    return no_limits


def _make_dirs(path: Path) -> None:
    """`fsutil.make_private_dir`, copied: each missing level with owner-only permissions."""

    missing: list[Path] = []
    current = path
    while not os.path.lexists(current) and current.parent != current:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass


def _lock(path: Path, timeout: float = LOCK_SECONDS) -> int | None:
    """An open descriptor holding an exclusive lock on `path`, or None after `timeout` seconds (or without flock)."""

    if fcntl is None:
        return None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except OSError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(0.02)


def _write(path: Path, value: dict[str, Any]) -> None:
    """Write `value` as JSON through a fresh temporary file and a rename (`fsutil.write_private`, copied)."""

    data = json.dumps(value, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def commit(live: Path, incoming: dict[str, dict[str, Any]], now: float, login: str | None = None,
           no_limits: bool = False) -> bool:
    """Under the file's lock, re-read the live file and write what is still news. Whether it wrote."""

    if login is None:
        return False
    _make_dirs(live.parent)
    descriptor = _lock(lock_path(live))
    if descriptor is None:
        return False
    try:
        value = updated(read_live(live), incoming, now, login, no_limits)
        if value is None:
            return False  # another session wrote the same or newer meanwhile
        _write(live, value)
        return True
    finally:
        os.close(descriptor)  # closing releases the lock


def record(live: Path, incoming: dict[str, dict[str, Any]], now: float, login: str | None = None,
           no_limits: bool = False, *, stored: dict[str, Any] | None = None) -> bool:
    """Write `incoming` into the live file for `login` when it holds news. Nothing under `state/` is touched
    otherwise."""

    if stored is None:
        stored = read_live(live)
    if updated(stored, incoming, now, login, no_limits) is None:
        return False
    return commit(live, incoming, now, login, no_limits)


def tap(raw: bytes, now: float) -> bool:
    """Record one status line payload. Whether it wrote."""

    if len(raw) > MAX_BYTES:
        return False
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return False
    incoming = windows_from(payload, observed_at(payload, now))
    if incoming is None:
        return False
    no_limits = "rate_limits" not in payload and payload.get("prompt_cache") is not None
    if not incoming and not no_limits:
        return False
    folder, home = profile_folder(), agt_home()
    try:
        resolved = folder.resolve()
    except (OSError, RuntimeError):
        resolved = None  # keep each helper's fallback for paths that cannot be resolved
    if in_worker_home(folder, home, resolved=resolved):
        return False  # agt's worker homes report through their jobs
    live = live_path(home, home_key(folder, resolved=resolved))
    stored = read_live(live)
    if not might_write(stored, incoming, now, no_limits):
        return False  # the unchanged path: no lock, and no read of .claude.json
    login = read_login(folder)  # the one read of .claude.json, on the write path only
    return record(live, incoming, now, login, no_limits, stored=stored)


def main(argv: list[str] | None = None) -> int:
    try:
        tap(sys.stdin.buffer.read(MAX_BYTES + 1), time.time())
    except BaseException:  # the status line must never notice the tap
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
