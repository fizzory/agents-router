"""Read-only file tools for a worker: read, list and search inside the job's folders.

A worker whose rules forbid shell commands (a researcher on Codex, whose only file tools are
shell and apply_patch) still has to read the files its task names. These tools run in the
agt-parent MCP server, outside the CLI's sandbox and its permission rules, so they keep their
own:

- Every path is confined by its real path to the job's `cwd` and `add_dirs`: `..`, an absolute
  path elsewhere and a symlink that leaves those folders are all refused.
- A file is checked again through the descriptor that reads it, so swapping a checked path for a
  symlink (or a FIFO) before it is opened reaches nothing outside those folders.
- Keys, logins and environment files are refused by the rules the Antigravity review gate uses
  (`agy_review.paths`), and so is Agents Router's own state: the gate approves agt-parent calls,
  so these tools must not read what the gate would deny.
- A search runs in a child process with a hard time limit, so a pattern that backtracks without
  end stops there instead of hanging the server.

Output is capped so one call can't flood the worker's context.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agents_router import jobs
from agents_router.agy_review import paths as secret_paths

DEFAULT_READ_LINES = 2000
MAX_READ_LINES = 5000
MAX_READ_CHARS = 100_000
MAX_LINE_CHARS = 2000
MAX_LIST_ENTRIES = 1000
DEFAULT_SEARCH_RESULTS = 100
MAX_SEARCH_RESULTS = 500
MAX_MATCH_CHARS = 300
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_SKIPPED_SHOWN = 20
WALK_SECONDS = 20.0
SEARCH_HARD_SECONDS = WALK_SECONDS + 10  # the child is killed here, whatever it is doing
SNIFF_BYTES = 8192
CHUNK_CHARS = 65_536
# Walked into only when the call starts inside one: they are large and rarely what a task means.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
# macOS and Windows volumes are case-insensitive by default: compare paths without case there.
FOLD_CASE = sys.platform in ("darwin", "win32")


class FileToolError(ValueError):
    """A refusal the worker can act on: the message says what was wrong."""


class _Unreadable(FileToolError):
    """The opened file isn't a regular file inside the job's folders (checked on its descriptor)."""


def roots(job_id: str) -> list[Path]:
    """The job's folder first, then its added folders, each by real path."""

    spec = jobs.read_spec(job_id)
    found: list[Path] = []
    for item in [spec.cwd, *(spec.add_dirs or [])]:
        try:
            path = Path(item).resolve(strict=True)
        except OSError:
            continue
        if path.is_dir() and path not in found:
            found.append(path)
    if not found:
        raise FileToolError("this job has no folder to read")
    return found


def _key(path: Any) -> str:
    text = os.path.normpath(str(path))
    return text.casefold() if FOLD_CASE else text


def _inside(path: Any, folders: list[Path]) -> bool:
    key = _key(path)
    for root in folders:
        base = _key(root)
        if key == base or key.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _homes() -> list[str]:
    """Home folders whose logins the secret rules know: this process's HOME and the account's own."""

    found = [os.path.expanduser("~")]
    try:
        import pwd

        found.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    return list(dict.fromkeys(home for home in found if home and home != "~"))


def _protected() -> list[Path]:
    """Agents Router's own state: jobs, other accounts' homes, settings."""

    from agents_router.config import home as agt_home

    try:
        return [Path(agt_home()).resolve()]
    except OSError:
        return []


def _secret(path: Any, folders: list[Path]) -> str | None:
    text = str(path)
    # The gate's DOS short-name rule (RUNNER~1) only means something on Windows: elsewhere `notes~1.md`
    # is just a name. Trusting the whole path leaves that rule no component to look at.
    reason = secret_paths.secret_reason(text, _homes(), trusted=str(folders[0]) if os.name == "nt" else text)
    if reason is None and _inside(text, _protected()):
        reason = "it is Agents Router's own state (jobs, other accounts, config)"
    return reason


def _resolve(folders: list[Path], raw: Any) -> tuple[Path, str]:
    text = str(raw or ".").strip() or "."
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = folders[0] / candidate
    try:
        real = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise FileToolError(f"no such file or folder: {text}") from None
    except OSError as exc:
        raise FileToolError(f"can't open {text}: {exc.strerror or exc}") from None
    if not _inside(real, folders):
        allowed = ", ".join(str(root) for root in folders)
        raise FileToolError(f"{text} is outside the folders this job may read ({allowed})")
    # Both forms, as the gate checks them: a link named .env, or a harmless name for a key file.
    reason = _secret(os.path.normpath(candidate), folders) or _secret(real, folders)
    if reason:
        raise FileToolError(f"{text} is off limits: {reason}")
    return real, text


def _shown(path: Path, folders: list[Path]) -> str:
    """Relative to the job's folder, absolute in an added folder."""

    if path == folders[0]:
        return "."
    if path.is_relative_to(folders[0]):
        return path.relative_to(folders[0]).as_posix()
    return str(path)


def _is_regular(path: Path) -> bool:
    """A plain file: opening a FIFO, socket or device could block the server or never end."""

    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _fd_path(fd: int) -> str | None:
    """Where the kernel says an open descriptor is, or None where it can't tell."""

    if sys.platform == "darwin":
        import fcntl

        if hasattr(fcntl, "F_GETPATH"):
            try:
                return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
            except OSError:
                return None
        return None
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    return None if target.endswith(" (deleted)") else target


def _check_fd(fd: int, path: Path, folders: list[Path], *, folder: bool = False) -> None:
    info = os.fstat(fd)
    if not (stat.S_ISDIR(info.st_mode) if folder else stat.S_ISREG(info.st_mode)):
        raise _Unreadable("not a regular file" if not folder else "not a folder")
    actual = _fd_path(fd)
    if actual is None:  # no descriptor path here: the real path must still name this same file
        real = os.path.realpath(path)
        try:
            same = os.path.samestat(info, os.stat(real))
        except OSError:
            same = False
        if not same:
            raise _Unreadable("it changed while it was opened")
        actual = real
    if not _inside(actual, folders):
        raise _Unreadable("outside the folders this job may read")
    if not folder and _secret(actual, folders):
        raise _Unreadable("off limits")


def _open_checked(path: Path, folders: list[Path]) -> io.BufferedReader:
    """Open a regular file for reading, then confirm on the descriptor that it is the one that was checked.

    O_NONBLOCK keeps a FIFO swapped in after the first check from blocking the open itself.
    """

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        _check_fd(fd, path, folders)
        if hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _text(handle: io.BufferedReader) -> io.TextIOWrapper | None:
    """The file as text, or None for a binary file (a NUL byte near its start)."""

    if b"\0" in handle.read(SNIFF_BYTES):
        return None
    handle.seek(0)
    return io.TextIOWrapper(handle, encoding="utf-8", errors="replace")


def _lines(handle, keep: int):
    """Lines without their line ending, each cut to `keep` characters; memory stays bounded on any file.

    Yields (text, cut) pairs. The rest of a cut line is read in chunks and dropped.
    """

    while True:
        line = handle.readline(keep + 1)
        if not line:
            return
        text = line.rstrip("\r\n")
        cut = len(text) > keep
        if not line.endswith("\n") and len(line) > keep:
            while True:
                rest = handle.readline(CHUNK_CHARS)
                if not rest or rest.endswith("\n"):
                    break
        yield text[:keep], cut


def _matches(relative: str, pattern: str | None) -> bool:
    """A pattern without `/` matches file names at any depth; one with `/` matches the path."""

    if not pattern:
        return True
    if "/" not in pattern:
        return fnmatch.fnmatchcase(relative.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatchcase(relative, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]))


def _walk(top: Path, folders: list[Path], deadline: float):
    """Files under `top` in a stable order, skipping heavy folders, credential folders and anything that leaves the job's folders.

    Yields (path, relative-to-top) pairs, then a final (None, reason) when the time ran out.
    `os.fwalk` (where it exists) descends by descriptor and never into a folder swapped for a
    symlink; each file is checked again when it is opened.
    """

    if hasattr(os, "fwalk"):
        walker = ((current, dirs, files) for current, dirs, files, _fd in os.fwalk(top, follow_symlinks=False))
    else:
        walker = os.walk(top, followlinks=False)
    stopped = f"stopped after {int(WALK_SECONDS)} seconds"
    for current, dirs, files in walker:
        if time.monotonic() > deadline:
            yield None, stopped
            return
        dirs[:] = sorted(name for name in dirs
                         if name not in SKIP_DIRS and name.casefold() not in secret_paths.SECRET_DIRS)
        base = Path(current)
        for name in sorted(files):
            if time.monotonic() > deadline:
                yield None, stopped
                return
            path = base / name
            if not _is_regular(path):
                continue
            if path.is_symlink():
                try:
                    if not _inside(path.resolve(strict=True), folders):
                        continue
                except OSError:
                    continue
            yield path, path.relative_to(top).as_posix()


def read_file(job_id: str, path: Any, offset: Any = 1, limit: Any = DEFAULT_READ_LINES) -> dict[str, Any]:
    folders = roots(job_id)
    real, text = _resolve(folders, path)
    if real.is_dir():
        raise FileToolError(f"{text} is a folder; use list_files")
    if not _is_regular(real):
        raise FileToolError(f"{text} is not a regular file")
    first = max(1, int(offset or 1))
    count = max(1, min(MAX_READ_LINES, int(limit or DEFAULT_READ_LINES)))
    lines: list[str] = []
    size = 0
    last = first - 1
    more = stopped = False
    deadline = time.monotonic() + WALK_SECONDS
    try:
        raw = _open_checked(real, folders)
    except _Unreadable as exc:
        raise FileToolError(f"{text} is {exc}" if str(exc).startswith("not ") else f"{text}: {exc}") from None
    except OSError as exc:
        raise FileToolError(f"can't open {text}: {exc.strerror or exc}") from None
    with raw:
        handle = _text(raw)
        if handle is None:
            raise FileToolError(f"{text} is a binary file")
        for number, (line, cut) in enumerate(_lines(handle, MAX_LINE_CHARS), 1):
            if number % 1000 == 0 and time.monotonic() > deadline:
                more = stopped = True
                break
            if number < first:
                continue
            if number >= first + count or size >= MAX_READ_CHARS:
                more = True
                break
            if cut:
                line += " …[line cut]"
            lines.append(f"{number}\t{line}")
            size += len(lines[-1]) + 1
            last = number
        handle.detach()
    result: dict[str, Any] = {"path": _shown(real, folders), "start_line": first, "end_line": last,
                              "content": "\n".join(lines)}
    if more:
        result["more"] = True
        result["note"] = (f"Stopped after {int(WALK_SECONDS)} seconds; " if stopped else "The file goes on; ") + \
            f"call read_file again with offset={last + 1}."
    elif not lines and first > 1:
        result["note"] = f"The file has fewer than {first} lines."
    return result


def _scan(real: Path, folders: list[Path]) -> list[tuple[str, bool]]:
    """(name, is a folder) for each entry the job may see, read through a checked descriptor where possible."""

    shown: list[tuple[str, bool]] = []
    if os.name == "posix":
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            _check_fd(fd, real, folders, folder=True)
            entries = sorted(os.scandir(fd), key=lambda item: item.name)
        finally:
            os.close(fd)
    else:
        entries = sorted(os.scandir(real), key=lambda item: item.name)
    for entry in entries:
        if entry.is_symlink():
            try:
                if not _inside((real / entry.name).resolve(strict=True), folders):
                    continue
            except OSError:
                continue
        shown.append((entry.name, entry.is_dir()))
    return shown


def list_files(job_id: str, path: Any = ".", pattern: Any = None) -> dict[str, Any]:
    folders = roots(job_id)
    real, text = _resolve(folders, path)
    if not real.is_dir():
        return {"path": _shown(real, folders), "entries": [_shown(real, folders)]}
    entries: list[str] = []
    truncated = None
    if pattern:
        for item, relative in _walk(real, folders, time.monotonic() + WALK_SECONDS):
            if item is None:
                truncated = relative
                break
            if _matches(relative, str(pattern)):
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = f"more than {MAX_LIST_ENTRIES} matches"
                    break
                entries.append(relative)
    else:
        try:
            scanned = _scan(real, folders)
        except _Unreadable as exc:
            raise FileToolError(f"{text}: {exc}") from None
        for name, folder in scanned:
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = f"more than {MAX_LIST_ENTRIES} entries"
                break
            entries.append(name + ("/" if folder else ""))
    result: dict[str, Any] = {"path": _shown(real, folders), "entries": entries}
    if truncated:
        result["truncated"] = truncated
    return result


def search_files(job_id: str, pattern: Any, path: Any = ".", glob: Any = None, ignore_case: Any = False,
                 max_results: Any = DEFAULT_SEARCH_RESULTS) -> dict[str, Any]:
    """Run the search in a child process: Python can't interrupt a regular expression that backtracks without end."""

    from agents_router.fsutil import no_window_kwargs

    request = {"job_id": job_id, "pattern": None if pattern is None else str(pattern),
               "path": None if path is None else str(path), "glob": None if glob is None else str(glob),
               "ignore_case": bool(ignore_case), "max_results": max_results}
    package_parent = str(Path(__file__).resolve().parent.parent)
    code = "import sys; sys.path.insert(0, sys.argv[1]); from agents_router import worker_files; worker_files._child()"
    try:
        done = subprocess.run([sys.executable, "-I", "-c", code, package_parent], input=json.dumps(request),
                              capture_output=True, text=True, encoding="utf-8", timeout=SEARCH_HARD_SECONDS,
                              **no_window_kwargs())
    except subprocess.TimeoutExpired:
        raise FileToolError(f"the search was stopped after {int(SEARCH_HARD_SECONDS)} seconds; a pattern with nested "
                            "repeats such as (a+)+ can take forever: simplify it or narrow path and glob") from None
    except OSError as exc:
        raise FileToolError(f"the search could not start: {exc}") from None
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise FileToolError(f"the search failed: {tail[0][:300]}") from None
    if "error" in answer:
        raise FileToolError(str(answer["error"]))
    return answer["result"]


def _child() -> None:
    request = json.loads(sys.stdin.read())
    try:
        answer: dict[str, Any] = {"result": _search(**request)}
    except FileToolError as exc:
        answer = {"error": str(exc)}
    except (OSError, ValueError, TypeError) as exc:
        answer = {"error": f"the search failed: {exc}"}
    sys.stdout.write(json.dumps(answer))


def _search(job_id: str, pattern: Any, path: Any = ".", glob: Any = None, ignore_case: Any = False,
            max_results: Any = DEFAULT_SEARCH_RESULTS) -> dict[str, Any]:
    folders = roots(job_id)
    real, text = _resolve(folders, path)
    try:
        regex = re.compile(str(pattern or ""), re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise FileToolError(f"not a valid regular expression: {exc}") from None
    if not regex.pattern:
        raise FileToolError("pattern is required")
    limit = max(1, min(MAX_SEARCH_RESULTS, int(max_results or DEFAULT_SEARCH_RESULTS)))
    deadline = time.monotonic() + WALK_SECONDS
    if real.is_dir():
        candidates = _walk(real, folders, deadline)
    else:
        if not _is_regular(real):
            raise FileToolError(f"{text} is not a regular file")
        if real.stat().st_size > MAX_SEARCH_FILE_BYTES:
            raise FileToolError(f"{text} is larger than {MAX_SEARCH_FILE_BYTES // 1_000_000} MB, too big to search; "
                                "read it in parts with read_file")
        candidates = iter([(real, real.name)])
    matches: list[dict[str, Any]] = []
    skipped: list[str] = []
    secret: list[str] = []
    truncated = None
    for item, relative in candidates:
        if item is None:
            truncated = relative
            break
        if not _matches(relative, str(glob) if glob else None):
            continue
        if _secret(item, folders) or (item.is_symlink() and _secret(item.resolve(), folders)):
            secret.append(_shown(item, folders))
            continue
        try:
            if item.stat().st_size > MAX_SEARCH_FILE_BYTES:
                skipped.append(_shown(item, folders))
                continue
            with _open_checked(item, folders) as raw:
                handle = _text(raw)
                if handle is None:
                    continue
                for number, (line, _cut) in enumerate(_lines(handle, MAX_SEARCH_FILE_BYTES), 1):
                    if number % 1000 == 0 and time.monotonic() > deadline:
                        truncated = f"stopped after {int(WALK_SECONDS)} seconds"
                        break
                    if regex.search(line):
                        if len(matches) >= limit:
                            truncated = f"more than {limit} matches"
                            break
                        matches.append({"path": _shown(item, folders), "line": number,
                                        "text": line[:MAX_MATCH_CHARS]})
                handle.detach()
        except (_Unreadable, OSError):
            continue
        if truncated:
            break
    result: dict[str, Any] = {"matches": matches}
    if truncated:
        result["truncated"] = truncated
    if skipped:
        result["skipped"] = (f"not searched, larger than {MAX_SEARCH_FILE_BYTES // 1_000_000} MB "
                             f"(read them in parts with read_file): {_listed(skipped)}")
    if secret:
        result["off_limits"] = f"not searched, they may hold keys or logins: {_listed(secret)}"
    return result


def _listed(items: list[str]) -> str:
    return ", ".join(items[:MAX_SKIPPED_SHOWN]) + (
        f" and {len(items) - MAX_SKIPPED_SHOWN} more" if len(items) > MAX_SKIPPED_SHOWN else "")
