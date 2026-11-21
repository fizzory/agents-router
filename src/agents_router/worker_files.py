"""Read-only file tools for a worker: read, list and search inside the job's folders.

A worker whose rules forbid shell commands (a researcher on Codex, whose only file tools are
shell and apply_patch) still has to read the files its task names. These tools run in the
agt-parent MCP server, outside the CLI's sandbox, so every path is confined by its real path to
the job's `cwd` and `add_dirs`: `..`, an absolute path elsewhere and a symlink that leaves those
folders are all refused. Output is capped so one call can't flood the worker's context.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any

from agents_router import jobs

DEFAULT_READ_LINES = 2000
MAX_READ_LINES = 5000
MAX_READ_CHARS = 100_000
MAX_LINE_CHARS = 2000
MAX_LIST_ENTRIES = 1000
DEFAULT_SEARCH_RESULTS = 100
MAX_SEARCH_RESULTS = 500
MAX_MATCH_CHARS = 300
MAX_SEARCH_FILE_BYTES = 2_000_000
WALK_SECONDS = 20.0
SNIFF_BYTES = 8192
# Walked into only when the call starts inside one: they are large and rarely what a task means.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})


class FileToolError(ValueError):
    """A refusal the worker can act on: the message says what was wrong."""


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


def _inside(path: Path, folders: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in folders)


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
    return real, text


def _shown(path: Path, folders: list[Path]) -> str:
    """Relative to the job's folder, absolute in an added folder."""

    if path == folders[0]:
        return "."
    if path.is_relative_to(folders[0]):
        return path.relative_to(folders[0]).as_posix()
    return str(path)


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as handle:
        return b"\0" in handle.read(SNIFF_BYTES)


def _matches(relative: str, pattern: str | None) -> bool:
    """A pattern without `/` matches file names at any depth; one with `/` matches the path."""

    if not pattern:
        return True
    if "/" not in pattern:
        return fnmatch.fnmatchcase(relative.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatchcase(relative, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]))


def _walk(top: Path, folders: list[Path], deadline: float):
    """Files under `top` in a stable order, skipping heavy folders and anything that leaves the job's folders.

    Yields (path, relative-to-top) pairs, then a final (None, reason) when the time ran out.
    """

    for current, dirs, files in os.walk(top, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        base = Path(current)
        for name in sorted(files):
            if time.monotonic() > deadline:
                yield None, f"stopped after {int(WALK_SECONDS)} seconds"
                return
            path = base / name
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
    if _is_binary(real):
        raise FileToolError(f"{text} is a binary file")
    first = max(1, int(offset or 1))
    count = max(1, min(MAX_READ_LINES, int(limit or DEFAULT_READ_LINES)))
    lines: list[str] = []
    size = 0
    last = first - 1
    more = False
    with open(real, encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if number < first:
                continue
            if number >= first + count or size >= MAX_READ_CHARS:
                more = True
                break
            line = line.rstrip("\r\n")
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " …[line cut]"
            lines.append(f"{number}\t{line}")
            size += len(lines[-1]) + 1
            last = number
    result: dict[str, Any] = {"path": _shown(real, folders), "start_line": first, "end_line": last,
                              "content": "\n".join(lines)}
    if more:
        result["more"] = True
        result["note"] = f"The file goes on; call read_file again with offset={last + 1}."
    elif not lines and first > 1:
        result["note"] = f"The file has fewer than {first} lines."
    return result


def list_files(job_id: str, path: Any = ".", pattern: Any = None) -> dict[str, Any]:
    folders = roots(job_id)
    real, _text = _resolve(folders, path)
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
        for entry in sorted(os.scandir(real), key=lambda item: item.name):
            if entry.is_symlink():
                try:
                    if not _inside(Path(entry.path).resolve(strict=True), folders):
                        continue
                except OSError:
                    continue
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = f"more than {MAX_LIST_ENTRIES} entries"
                break
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
    result: dict[str, Any] = {"path": _shown(real, folders), "entries": entries}
    if truncated:
        result["truncated"] = truncated
    return result


def search_files(job_id: str, pattern: Any, path: Any = ".", glob: Any = None, ignore_case: Any = False,
                 max_results: Any = DEFAULT_SEARCH_RESULTS) -> dict[str, Any]:
    folders = roots(job_id)
    real, _text = _resolve(folders, path)
    try:
        regex = re.compile(str(pattern or ""), re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise FileToolError(f"not a valid regular expression: {exc}") from None
    if not regex.pattern:
        raise FileToolError("pattern is required")
    limit = max(1, min(MAX_SEARCH_RESULTS, int(max_results or DEFAULT_SEARCH_RESULTS)))
    if real.is_dir():
        candidates = _walk(real, folders, time.monotonic() + WALK_SECONDS)
    else:
        candidates = iter([(real, real.name)])
    matches: list[dict[str, Any]] = []
    truncated = None
    for item, relative in candidates:
        if item is None:
            truncated = relative
            break
        if not _matches(relative, str(glob) if glob else None):
            continue
        try:
            if item.stat().st_size > MAX_SEARCH_FILE_BYTES or _is_binary(item):
                continue
            with open(item, encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if regex.search(line):
                        if len(matches) >= limit:
                            truncated = f"more than {limit} matches"
                            break
                        matches.append({"path": _shown(item, folders), "line": number,
                                        "text": line.rstrip("\r\n")[:MAX_MATCH_CHARS]})
        except OSError:
            continue
        if truncated:
            break
    result: dict[str, Any] = {"matches": matches}
    if truncated:
        result["truncated"] = truncated
    return result
