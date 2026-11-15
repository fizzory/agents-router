"""Plugin parts for CLIs that install no plugins: the skills and commands inside your Claude Code plugins.

One way only, like hooks: agt owns what it writes here, and nothing flows back to a plugin. For each plugin id
it picks one source, the newest enabled user-scope install across the included Claude folders, and renders a
mirror of it in agt's state:

    state/share/plugin-parts/<marketplace>/<plugin>/
    ├── skills/<skill>/SKILL.md            variables and `./`, `../` paths made absolute mirror paths
    ├── .agt-commands/<command>/SKILL.md   a skill made from commands/<command>.md
    └── hooks/, scripts/, …                copied as they are, so `../` paths and replaced paths resolve

| Rule | Choice |
|---|---|
| Source | Newest enabled (`enabledPlugins` true) user-scope install of a plugin id; ties go to folder order. agents-router, turned off everywhere or project scope only: none. |
| Mirror | A copy of the plugin tree with its layout kept, `.git` left out, built next to the mirror and swapped in by rename. The plugin cache is only read: a link inside the plugin leads to the same place in the mirror, and one out of it is left out. |
| Variables | `${CLAUDE_PLUGIN_ROOT}`, `$CLAUDE_PLUGIN_ROOT`, `${CLAUDE_SKILL_DIR}` and `$CLAUDE_SKILL_DIR` in markdown files under `skills/`; shell forms like `${CLAUDE_SKILL_DIR:+…}` stay. |
| Paths | In those files, `./x` and `../x` read from the file's folder become absolute mirror paths when they name something inside the mirror; a command skill also takes bare plugin-root paths (`skills/x/…`, first segment a top-level entry). Other paths and URLs (their queries too) stay. |
| Commands | `commands/<name>.md` becomes a skill unless a skill of the plugin has that name (two skills that clash over it keep it too) or the name isn't valid; both are reported. |
| Keeping current | A run that finds the same source, version and source mtimes, and an unchanged mirror, rebuilds and hashes nothing. A mirror changed since agt built it, before or during the rebuild, is handed to `backup`. |
| Kiro | `<kiro>/skills/<name>` is an absolute link to the mirror skill folder, a path with no version in it. Only Kiro gets them: Claude and Codex load plugins themselves. |

`select`, `refresh` and `project` change only the mirror folders. `run` is the whole pass a sync makes: it keeps
`state/share/plugin-parts.json`, links the mirror skills into each Kiro folder, and moves whatever it replaces or
takes away to `state/share/backups/<run>/plugin-parts/`. The ordinary agents and skills sharing never shares a
link into a mirror (`exclude.plugin_part`), so a projection never travels to Claude or Codex.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import stat
import time
from typing import Any

from agents_router import fsutil
from agents_router.share import exclude, frontmatter, gitexclude, names, plugins, providers, state

KIND = exclude.PLUGIN_PARTS
COMMANDS = ".agt-commands"  # the command skills' folder inside a mirror
SKIPPED = frozenset({".git"})
MARKDOWN = frozenset({".md", ".markdown"})
TOKEN_RE = re.compile(r"\$\{(CLAUDE_PLUGIN_ROOT|CLAUDE_SKILL_DIR)\}|\$(CLAUDE_PLUGIN_ROOT|CLAUDE_SKILL_DIR)(?![A-Za-z0-9_])")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # a marketplace or plugin name, as one folder name
# `./x` or `../x` at the start of a word, a link target or a code span.
RELATIVE_RE = re.compile(r"(?<![^\s(\[<`'\"=])(\.\.?/[^\s)\]>`'\"#?]*)")
# The same, or a bare `first/rest` (command skills: `first` must be a top-level entry of the mirror).
COMMAND_PATH_RE = re.compile(r"(?<![^\s(\[<`'\"=])(\.\.?/[^\s)\]>`'\"#?]*|[^\s/()\[\]<>`'\"=#?]+/[^\s()\[\]<>`'\"=#?]+)")
# A URL, up to the next space: no path inside it, its query included, is rewritten.
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
ARGUMENT_RE = re.compile(r"\$ARGUMENTS\b|\$\d")
VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
ARGUMENT_NOTE = ("`$ARGUMENTS` below stands for everything asked along with this command, "
                 "and `$1` for its first word (`$2` the second, and so on).")


# Picking one source per plugin -----------------------------------------------------------


@dataclass
class Source:
    """The install a plugin's parts come from."""

    id: str  # name@marketplace
    label: str  # the Claude folder's label
    folder: Path  # that Claude folder
    version: str
    root: Path  # the plugin's installed files (read only)

    @property
    def name(self) -> str:
        return self.id.partition("@")[0]

    @property
    def marketplace(self) -> str:
        return self.id.partition("@")[2]


@dataclass
class Selection:
    sources: dict[str, Source] = field(default_factory=dict)  # plugin id -> its source
    unreadable: dict[str, str] = field(default_factory=dict)  # label -> why its plugin files can't be read now
    held: dict[str, str] = field(default_factory=dict)  # plugin id -> why it's on but can't be read now
    reports: list[str] = field(default_factory=list)


def version_key(version: str) -> tuple[Any, ...]:
    """Semantic version order: dotted numbers, a prerelease below its release. Versions that aren't numbers
    (a commit, `unknown`) rank lowest and equal to each other, so folder order decides between them."""

    match = VERSION_RE.match(version.strip())
    if not match:
        return (0,)
    release = [int(part) for part in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    prerelease = match.group(2)
    if prerelease is None:
        return (1, tuple(release), (1,))
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in prerelease.split("."))
    return (1, tuple(release), (0, parts))


def select(included: list[providers.Provider]) -> Selection:
    """One source per plugin id from the included Claude folders, in folder order.

    A folder whose plugin files can't be read is in `unreadable`, and a plugin that's on but whose installed
    files are missing is in `held`: neither means the plugin is gone.
    """

    selection = Selection()
    candidates: dict[str, list[Source]] = {}
    missing: dict[str, list[str]] = {}
    for provider in included:
        if provider.cli != "claude":
            continue
        holding = plugins.read(provider)
        if holding.problem:
            selection.unreadable[provider.label] = holding.problem
            continue
        for plugin_id, plugin in holding.plugins.items():
            # plugins.read already drops agents-router; this check stays as a guard.
            if plugin.name == plugins.OWN or plugin.enabled is not True:
                continue
            market, name = plugin.marketplace, plugin.name
            if not (SEGMENT_RE.match(name) and SEGMENT_RE.match(market)):
                report = f"{plugin_id}: agt can't use its name as a folder name"
                if report not in selection.reports:
                    selection.reports.append(report)
                continue
            if plugin.root is None or not plugin.root.is_dir():
                missing.setdefault(plugin_id, []).append(
                    f"{plugin_id}: its installed files in {provider.label} aren't there"
                    + (f" ({plugin.root})" if plugin.root is not None else ""))
                continue
            candidates.setdefault(plugin_id, []).append(
                Source(plugin_id, provider.label, provider.folder, plugin.version, plugin.root))
    for plugin_id in sorted(set(candidates) | set(missing)):
        found = candidates.get(plugin_id)
        if found:
            # max() keeps the first of equal keys, so ties go to folder order.
            selection.sources[plugin_id] = max(found, key=lambda source: version_key(source.version))
        else:
            selection.held[plugin_id] = "; ".join(missing[plugin_id])
        selection.reports.extend(missing.get(plugin_id, []) if found else [])
    return selection


# The mirror ------------------------------------------------------------------------------


# The parts a mirror leaves out, as `status` and `sync` note them: (part, why) -> the note for one part and for several.
LEFT_OUT = {
    ("skill", "name"): ("1 skill left out, Kiro doesn't accept its name",
                        "{n} skills left out, Kiro doesn't accept their names"),
    ("skill", "unreadable"): ("1 skill left out, agt can't read its SKILL.md",
                              "{n} skills left out, agt can't read their SKILL.md"),
    ("skill", "clash"): ("1 skill name left out, two or more of its skills have it",
                         "{n} skill names left out, two or more of its skills have each"),
    ("command", "covered"): ("1 command not made a skill, its skill of the same name covers it",
                             "{n} commands not made skills, its skills of the same names cover them"),
    ("command", "reserved"): ("1 command not made a skill, its skills that clash over the name keep it",
                              "{n} commands not made skills, its skills that clash over those names keep them"),
    ("command", "name"): ("1 command not made a skill, Kiro doesn't accept its name",
                          "{n} commands not made skills, Kiro doesn't accept their names"),
    ("command", "unreadable"): ("1 command not made a skill, agt can't read it",
                                "{n} commands not made skills, agt can't read them"),
    ("command", "empty"): ("1 command not made a skill, it has no description and no text",
                           "{n} commands not made skills, they have no description and no text"),
    ("file", "raw"): ("1 file copied as it is, it isn't UTF-8, so its paths and variables stay as written",
                      "{n} files copied as they are, they aren't UTF-8, so their paths and variables stay as written"),
    ("link", "outside"): ("1 link left out, it leads out of the plugin",
                          "{n} links left out, they lead out of the plugin"),
}


class _Reports(list):
    """A build's report lines, and for each part left out its kind, name and why (a `LEFT_OUT` key), which the record
    keeps as `left_out` for the notes."""

    def __init__(self) -> None:
        super().__init__()
        self.left: list[dict[str, str]] = []

    def leave(self, line: str, part: str, name: str, why: str) -> None:
        self.append(line)
        self.left.append({"part": part, "name": name, "why": why})


@dataclass
class Part:
    name: str  # what the CLI calls it: the skill's frontmatter name, or the command's file name
    folder: Path  # its skill folder in the mirror


@dataclass
class Projection:
    id: str
    source: Source
    mirror: Path
    skills: list[Part] = field(default_factory=list)
    commands: list[Part] = field(default_factory=list)  # command skills
    reports: list[str] = field(default_factory=list)  # skills and commands left out, and why
    record: dict[str, Any] | None = None  # what the caller keeps for the next run (None: nothing built yet)
    rebuilt: bool = False
    edited: bool = False  # the mirror had changed since agt built it, and went to `backup`
    failed: str | None = None  # the build failed; the mirror and record are the earlier ones
    held: str | None = None  # its source can't be read now, so the earlier mirror and record stay as they are
    deferred: bool = False  # it needs a build, which this run (a session start or a save) leaves to the next sync


def default_root() -> Path:
    """`state/share/plugin-parts`: your user folders' share state, whatever the current scope."""

    return exclude.plugin_parts_root()


def mirror_path(root: Path, plugin_id: str) -> Path:
    name, _, market = plugin_id.partition("@")
    return root / market / name


Entries = list[tuple[str, os.stat_result, bool]]  # (relative path, lstat, is a folder), as `_entries` lists a tree


def _entries(folder: Path) -> Entries:
    """(relative path, lstat, is a folder) for everything under `folder`, `.git` left out, links not followed."""

    found = []
    stack = [(folder, "")]
    while stack:
        path, prefix = stack.pop()
        with os.scandir(path) as listing:
            for entry in listing:
                if entry.name in SKIPPED:
                    continue
                relative = f"{prefix}{entry.name}"
                info = entry.stat(follow_symlinks=False)
                is_folder = stat.S_ISDIR(info.st_mode)
                found.append((relative, info, is_folder))
                if is_folder:
                    stack.append((Path(entry.path), relative + "/"))
    return sorted(found, key=lambda item: item[0])


def _signature(folder: Path, entries: Entries | None = None) -> str | None:
    """Names, sizes and mtimes (ns) of a tree's files and links: a change detector that reads no content.
    `entries` is the tree's `_entries`, when the caller has them already."""

    if entries is None:
        if not folder.is_dir():
            return None
        entries = _entries(folder)
    digest = hashlib.sha256()
    for relative, info, is_folder in entries:
        if not is_folder:
            digest.update(f"{relative}\0{info.st_size}\0{info.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _digest(folder: Path, entries: Entries | None = None) -> str:
    """The mirror's content: every file's bytes and every link's target, dot folders included."""

    digest = hashlib.sha256()
    for relative, info, is_folder in entries if entries is not None else _entries(folder):
        path = folder / relative
        if is_folder:
            continue
        if stat.S_ISLNK(info.st_mode):
            value = "link:" + os.readlink(path)
        else:
            value = fsutil.sha256_file(path)
        digest.update(f"{relative}\0{value}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _copy_file(source: str, destination: str) -> object:
    """Copy one file with its bytes and times (a seam the tests use)."""

    return shutil.copy2(source, destination)


def _remove_tree(path: Path) -> None:
    def writable(function, failed, _info):
        os.chmod(failed, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        function(failed)

    if os.path.lexists(path):
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, onerror=writable)


def _write_new(path: Path, data: bytes) -> None:
    """Put a new file at `path` (never written through a link there) with the old one's mode."""

    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode) if not path.is_symlink() else 0o644
    except FileNotFoundError:
        mode = 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = fsutil.temp_sibling(path)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _substitute(text: str, plugin_root: str, skill_dir: str | None) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1) or match.group(2)
        if token == "CLAUDE_PLUGIN_ROOT":
            return plugin_root
        return skill_dir if skill_dir is not None else match.group(0)

    return TOKEN_RE.sub(replace, text)


def _read_markdown(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def _skills(build: Path, final: Path, reports: _Reports) -> tuple[list[Part], dict[str, tuple[str, str]]]:
    """The plugin's skills, each under its frontmatter name; ones agt can't link are reported.

    Also returns the names two or more skills clash over (identity -> the name and their folders): no skill
    links under such a name, and it stays reserved, so a command of that name doesn't take it either.
    """

    found: dict[str, list[Part]] = {}
    clashes: dict[str, tuple[str, str]] = {}
    folder = build / "skills"
    if not folder.is_dir() or folder.is_symlink():
        return [], clashes
    for entry in sorted(folder.iterdir()):
        manifest = entry / "SKILL.md"
        if not entry.is_dir() or not manifest.is_file():
            continue
        where = f"skills/{entry.name}"
        text = _read_markdown(manifest)
        if text is None:
            reports.leave(f"{where}: left out, its SKILL.md isn't UTF-8", "skill", entry.name, "unreadable")
            continue
        try:
            declared = frontmatter.parse(text).fields.get("name")
        except frontmatter.FrontmatterError as exc:
            reports.leave(f"{where}: left out, agt can't read its front matter ({exc})", "skill", entry.name, "unreadable")
            continue
        name = str(declared).strip() if declared not in (None, "") else entry.name
        if why := names.problem(name, "kiro"):
            reports.leave(f"{where}: left out, its name {name} doesn't fit ({why})", "skill", name, "name")
            continue
        found.setdefault(names.identity(name), []).append(Part(name, final / "skills" / entry.name))
    parts = []
    for key, same in found.items():
        if len(same) == 1:
            parts.append(same[0])
        else:
            folders = ", ".join(f"skills/{part.folder.name}" for part in same)
            reports.leave(f"{folders}: left out, they all name the skill {same[0].name}", "skill", same[0].name, "clash")
            clashes[key] = (same[0].name, folders)
    return sorted(parts, key=lambda part: part.name), clashes


def _render_skills(build: Path, final: Path, reports: _Reports) -> None:
    """Make the variables and existing relative paths in the markdown files under `skills/` absolute mirror
    paths: regular files only, never through a link."""

    folder = build / "skills"
    if not folder.is_dir() or folder.is_symlink():
        return
    for relative, info, is_folder in _entries(folder):
        if is_folder or not stat.S_ISREG(info.st_mode) or Path(relative).suffix.lower() not in MARKDOWN:
            continue
        path = folder / relative
        first = relative.split("/", 1)[0]
        skill_dir = (final / "skills" / first).as_posix() if "/" in relative and (folder / first / "SKILL.md").is_file() \
            else None
        base = posixpath.dirname(f"skills/{relative}")  # paths are read from the file's own folder
        text = _read_markdown(path)
        if text is None:
            lossy = path.read_bytes().decode("utf-8", "replace")
            if _rewrite(lossy, build, final, base, skill_dir) != lossy:
                reports.leave(f"skills/{relative}: copied as it is, it isn't UTF-8", "file", f"skills/{relative}", "raw")
            continue
        changed = _rewrite(text, build, final, base, skill_dir)
        if changed != text:
            _write_new(path, changed.encode("utf-8"))


def _dot_segment_end(path: str) -> bool:
    return path in (".", "..") or path.endswith(("/.", "/.."))


def _absolute_paths(text: str, build: Path, final: Path, base: str, top: frozenset[str] | None = None) -> str:
    """`./x` and `../x` read from `base` (a folder in the plugin) become absolute mirror paths when they exist
    inside the mirror. With `top` (the mirror's top-level entries), a bare `first/rest` whose `first` is one of
    them is read from the plugin root the same way. URLs stay as written, their queries included."""

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        trail = ""
        # Trailing punctuation isn't part of the path, but the dots of a `.` or `..` segment are.
        while path and path[-1] in ",;:!." and not (path[-1] == "." and _dot_segment_end(path)):
            path, trail = path[:-1], path[-1] + trail
        if not any(segment not in ("", ".", "..") for segment in path.split("/")):
            return match.group(0)  # only dots (`cd ../..`, `ls ../`): the reader's own folder, never the mirror's
        if path.startswith(("./", "../")):
            joined = posixpath.normpath(posixpath.join(base, path))
        elif top is not None and path.split("/", 1)[0] in top:
            joined = posixpath.normpath(path)
        else:
            return match.group(0)
        if joined == ".." or joined.startswith("../") or posixpath.isabs(joined) or not os.path.lexists(build / joined):
            return match.group(0)
        absolute = (final / joined).as_posix() if joined != "." else final.as_posix()
        return absolute + ("/" if path.endswith("/") and not absolute.endswith("/") else "") + trail

    pattern = COMMAND_PATH_RE if top is not None else RELATIVE_RE
    pieces, last = [], 0
    for url in URL_RE.finditer(text):  # a URL stays as written
        pieces += [pattern.sub(replace, text[last:url.start()]), url.group(0)]
        last = url.end()
    return "".join([*pieces, pattern.sub(replace, text[last:])])


def _rewrite(text: str, build: Path, final: Path, base: str, skill_dir: str | None,
             top: frozenset[str] | None = None) -> str:
    """Paths first, then variables: neither pass reads the mirror paths the other wrote."""

    return _substitute(_absolute_paths(text, build, final, base, top), final.as_posix(), skill_dir)


def _description(fields: dict[str, Any], body: str) -> str:
    value = fields.get("description")
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    text = str(value).strip() if value is not None else ""
    if text:
        return text
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _commands(build: Path, final: Path, plugin: str, skills: list[Part], clashes: dict[str, tuple[str, str]],
              reports: _Reports) -> list[Part]:
    """Write `.agt-commands/<name>/SKILL.md` for each command whose name no skill of the plugin has."""

    folder = build / "commands"
    if not folder.is_dir():
        return []
    covered = {names.identity(part.name): (f"the plugin's skill {part.name} covers it", "covered") for part in skills}
    for key, (name, folders) in clashes.items():
        covered.setdefault(key, (f"the name {name} stays with the plugin's skills {folders}, which clash over it", "reserved"))
    top = frozenset(entry.name for entry in build.iterdir()) - {COMMANDS}  # before any command skill is written
    parts = []
    for path in sorted(folder.glob("*.md")):
        name, where = path.stem, f"commands/{path.name}"
        if not path.is_file():
            continue
        if why := names.problem(name, "kiro"):
            reports.leave(f"{where}: not made a skill, its name doesn't fit ({why})", "command", name, "name")
            continue
        if names.identity(name) in covered:
            text, why = covered[names.identity(name)]
            reports.leave(f"{where}: not made a skill, {text}", "command", name, why)
            continue
        text = _read_markdown(path)
        if text is None:
            reports.leave(f"{where}: not made a skill, it isn't UTF-8", "command", name, "unreadable")
            continue
        try:
            document = frontmatter.parse(text)
        except frontmatter.FrontmatterError as exc:
            reports.leave(f"{where}: not made a skill, agt can't read its front matter ({exc})", "command", name, "unreadable")
            continue
        description = _description(document.fields, document.body)
        if not description:
            reports.leave(f"{where}: not made a skill, it has no description and no text", "command", name, "empty")
            continue
        skill_dir = final / COMMANDS / name
        body = _rewrite(document.body, build, final, "commands", skill_dir.as_posix(), top)
        head = [f"Made by agt from the `{name}` command of the {plugin} plugin."]
        hint = document.fields.get("argument-hint")
        if ARGUMENT_RE.search(body) or hint not in (None, ""):
            head.append(ARGUMENT_NOTE)
        if hint not in (None, ""):
            hint = " ".join(str(item) for item in hint) if isinstance(hint, list) else str(hint)
            head.append(f"Arguments: {hint.strip()}")
        content = frontmatter.render({"name": name, "description": description},
                                     "\n" + "\n\n".join(head) + "\n\n" + body.lstrip("\r\n"))
        _write_new(build / COMMANDS / name / "SKILL.md", content.encode("utf-8"))
        parts.append(Part(name, skill_dir))
    return parts


def _inside(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _rebase_links(source: Path, build: Path, reports: _Reports) -> None:
    """The plugin's links, as copied into `build`: one that leads inside the plugin, by an absolute path too, now
    leads to the same place in the mirror by a relative one, so nothing read or written through it reaches the
    plugin cache. One that leads out of the plugin (another version's folder, anywhere else) is left out."""

    real_root = os.path.realpath(source)
    for relative, info, _ in _entries(build):
        if not stat.S_ISLNK(info.st_mode):
            continue
        path = build / relative
        leads = os.path.realpath(source / relative)  # copytree went into no link, so only this one is followed
        inside = os.path.relpath(leads, real_root) if _inside(leads, real_root) else None
        if inside is None or inside.split(os.sep, 1)[0] in SKIPPED:
            os.unlink(path)
            reports.leave(f"{relative}: left out, it's a link out of the plugin", "link", relative, "outside")
            continue
        target = os.path.relpath(build / inside, path.parent)
        if os.readlink(path) != target:
            os.unlink(path)
            os.symlink(target, path, target_is_directory=(build / inside).is_dir())


def _render(source: Source, build: Path, final: Path) -> tuple[list[Part], list[Part], _Reports]:
    """Copy the plugin into `build` and turn it into the mirror that will live at `final`."""

    def ignore(folder: str, entries: list[str]) -> set[str]:
        left_out = {entry for entry in entries if entry in SKIPPED}
        if os.path.samefile(folder, source.root):
            left_out |= {COMMANDS} & set(entries)
        return left_out

    shutil.copytree(source.root, build, symlinks=True, ignore=ignore, copy_function=_copy_file)
    for current, folders, _ in os.walk(build):  # a read-only plugin folder still takes the files agt writes
        for name in folders:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IRWXU)
    os.chmod(build, stat.S_IMODE(os.lstat(build).st_mode) | stat.S_IRWXU)
    reports = _Reports()
    _rebase_links(source.root, build, reports)  # before anything reads through them
    _render_skills(build, final, reports)
    skills, clashes = _skills(build, final, reports)
    commands = _commands(build, final, source.name, skills, clashes, reports)
    return skills, commands, reports


def _sweep(mirror: Path, backup: Callable[[Path], None] | None) -> None:
    """Folders an interrupted run left next to the mirror: unfinished builds go, a set-aside mirror goes to backup."""

    if not mirror.parent.is_dir():
        return
    for entry in mirror.parent.iterdir():
        if not entry.name.startswith(f".{mirror.name}."):
            continue
        if entry.name.endswith(".tmp"):
            _remove_tree(entry)
        elif entry.name.endswith(".old"):
            if backup is not None:
                backup(entry)
            _remove_tree(entry)


def _swap(build: Path, mirror: Path) -> Path | None:
    """Put the finished build in the mirror's place, and return where the old mirror went.

    The old mirror moves aside whole, so it's never half-written: a reader sees the old tree or the new one.
    """

    old = None
    if os.path.lexists(mirror):
        old = mirror.with_name(f".{mirror.name}.{secrets.token_hex(6)}.old")
        os.rename(mirror, old)
    try:
        os.rename(build, mirror)
    except BaseException:
        if old is not None:
            os.rename(old, mirror)
        raise
    return old


def _record(source: Source, signature: str | None, mirror_signature: str | None, digest: str,
            skills: list[Part], commands: list[Part], reports: _Reports, mirror: Path) -> dict[str, Any]:
    return {
        "id": source.id, "source": source.label, "folder": str(source.folder), "root": str(source.root),
        "version": source.version,
        "source_signature": signature, "mirror_signature": mirror_signature, "digest": digest,
        "skills": [{"name": part.name, "folder": part.folder.relative_to(mirror).as_posix()} for part in skills],
        "commands": [{"name": part.name, "folder": part.folder.relative_to(mirror).as_posix()} for part in commands],
        "reports": list(reports),
        "left_out": [dict(entry) for entry in reports.left],
    }


def _from_record(source: Source, mirror: Path, record: dict[str, Any]) -> Projection:
    def parts(key: str) -> list[Part]:
        return [Part(str(item["name"]), mirror / str(item["folder"]))
                for item in record.get(key) or [] if isinstance(item, dict) and "name" in item and "folder" in item]

    return Projection(source.id, source, mirror, parts("skills"), parts("commands"),
                      [str(line) for line in record.get("reports") or []], record)


def refresh(source: Source, root: Path, previous: dict[str, Any] | None = None, *,
            backup: Callable[[Path], None] | None = None, build: bool = True) -> Projection:
    """Build or keep the mirror of one plugin under `root`.

    `previous` is the record this returned last time. With the same source, version and source mtimes, and a
    mirror that still matches its record, nothing is built or hashed. A mirror that changed since agt built it
    (or one agt has no record of) is passed to `backup` before it's replaced. Without `build` (a session start or
    a save), a mirror that needs building or hashing keeps its earlier record, marked `deferred`, and nothing is
    copied: the watcher or `agt share sync` builds it.
    """

    mirror = mirror_path(root, source.id)
    previous = previous if isinstance(previous, dict) else None
    exists = os.path.lexists(mirror)
    # Without `build`, a change known before any tree is walked (no mirror or record, a link in its place, another
    # root or version) defers at once.
    if not build and (previous is None or not exists or mirror.is_symlink() or previous.get("root") != str(source.root)
                      or previous.get("version") != source.version):
        return _deferred(source, mirror, previous)
    try:
        signature = _signature(source.root)
    except OSError as exc:
        return _failed(source, mirror, previous, f"agt can't read {source.root} ({exc})")
    if not build and signature != previous.get("source_signature"):
        return _deferred(source, mirror, previous)  # the mirror tree needn't be walked either
    edited = False
    if exists and previous is not None:
        mirror_signature = None if mirror.is_symlink() else _signature(mirror)
        if mirror_signature is not None and mirror_signature != previous.get("mirror_signature"):
            if not build:
                return _deferred(source, mirror, previous)
            if _digest(mirror) == previous.get("digest"):
                previous = {**previous, "mirror_signature": mirror_signature}  # touched, not changed
            else:
                edited = True
        elif mirror_signature is None:
            edited = True  # not a folder agt built
        same = (previous.get("root") == str(source.root) and previous.get("version") == source.version
                and previous.get("source_signature") == signature)
        if same and not edited:
            projection = _from_record(source, mirror, previous)
            projection.record = {**previous, "source": source.label}
            return projection
    elif exists:
        edited = True
    if not build:
        return _deferred(source, mirror, previous)
    try:
        root.mkdir(parents=True, exist_ok=True)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _sweep(mirror, backup)
    except OSError as exc:
        return _failed(source, mirror, previous, f"agt can't prepare {mirror.parent} ({exc})")
    build = fsutil.temp_sibling(mirror)
    try:
        skills, commands, reports = _render(source, build, mirror)
        built = _entries(build)  # one listing of the finished tree for both fingerprints
        digest, built_signature = _digest(build, built), _signature(build, built)
        old = _swap(build, mirror)
    except (OSError, shutil.Error) as exc:
        return _failed(source, mirror, previous, f"agt couldn't build its mirror ({exc})")
    finally:
        _remove_tree(build)
    record = _record(source, signature, built_signature, digest, skills, commands, reports, mirror)
    projection = Projection(source.id, source, mirror, skills, commands, reports, record, rebuilt=True, edited=edited)
    if old is not None:
        try:
            # Read again now: an edit made while agt built the new tree is in the old one, and is kept too.
            if backup is not None and (edited or _changed(old, previous)):
                backup(old)
            _remove_tree(old)
        except OSError as exc:  # the next run's sweep tries again
            projection.reports.append(f"agt couldn't put the old mirror away yet ({exc})")
    return projection


def _changed(folder: Path, record: dict[str, Any] | None) -> bool:
    """Whether a mirror set aside differs from the one agt built and recorded."""

    if record is None or os.path.islink(folder):
        return True
    entries = _entries(folder)
    return (_signature(folder, entries) != record.get("mirror_signature")
            and _digest(folder, entries) != record.get("digest"))


def _failed(source: Source, mirror: Path, previous: dict[str, Any] | None, why: str) -> Projection:
    projection = _from_record(source, mirror, previous) if previous is not None else Projection(source.id, source, mirror)
    projection.failed = why
    return projection


def _deferred(source: Source, mirror: Path, previous: dict[str, Any] | None) -> Projection:
    projection = _from_record(source, mirror, previous) if previous is not None else Projection(source.id, source, mirror)
    projection.deferred = True
    return projection


def _held(plugin_id: str, record: dict[str, Any], root: Path, why: str) -> Projection:
    source = Source(plugin_id, str(record.get("source") or ""), Path(str(record.get("folder") or "")),
                    str(record.get("version") or ""), Path(str(record.get("root") or "")))
    projection = _from_record(source, mirror_path(root, plugin_id), record)
    projection.held = why
    return projection


@dataclass
class Outcome:
    selection: Selection
    projections: dict[str, Projection] = field(default_factory=dict)  # plugin id -> its mirror


def project(included: list[providers.Provider], root: Path | None = None,
            previous: dict[str, dict[str, Any]] | None = None, *,
            backup: Callable[[str, Path], None] | None = None, build: bool = True) -> Outcome:
    """Pick a source for each plugin in the included Claude folders and build or keep its mirror.

    `previous` maps plugin id -> the record of the last run; `backup(plugin_id, folder)` receives a mirror that
    changed since agt built it. A plugin whose earlier source folder can't be read now, or whose installed files
    are missing (Claude Code may be mid-update), keeps its earlier mirror, marked `held`: it is neither rebuilt
    from another folder nor gone. Mirrors of plugins left out of `projections` are for the caller to remove.
    """

    root = root if root is not None else default_root()
    previous = previous or {}
    selection = select(included)
    outcome = Outcome(selection)
    for plugin_id in sorted(set(selection.sources) | set(previous)):
        record = previous.get(plugin_id) if isinstance(previous.get(plugin_id), dict) else None
        label = str(record.get("source") or "") if record is not None else ""
        why = selection.held.get(plugin_id) or (f"{label}: {selection.unreadable[label]}"
                                                 if label in selection.unreadable else None)
        if record is not None and why:
            outcome.projections[plugin_id] = _held(plugin_id, record, root, why)
        elif plugin_id in selection.sources:
            keep = (lambda folder, plugin_id=plugin_id: backup(plugin_id, folder)) if backup is not None else None
            outcome.projections[plugin_id] = refresh(selection.sources[plugin_id], root, record, backup=keep, build=build)
    return outcome


# Linking the mirrors into Kiro -------------------------------------------------------------

STATE_SCHEMA = 1


class StateError(state.StateError):
    """`plugin-parts.json` can't be used; the run changes nothing."""


@dataclass
class Written:
    """One change a run made, or tried to make."""

    name: str  # a skill's name, or the plugin id for its mirror
    label: str  # the Kiro folder's label; "" for a mirror
    action: str  # "link", "relink", "remove" (a link or mirror moved to the backups), "rebuild", "backup" (a mirror agt replaced)
    path: Path
    ok: bool
    message: str = ""
    backup: Path | None = None


@dataclass
class Result:
    run: str
    changes: list[Written] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)  # what isn't linked and why: clashes, conflicts, held or failed builds
    excluded: list[tuple[Path, list[str]]] = field(default_factory=list)  # git exclude files changed
    outcome: Outcome | None = None
    problem: str | None = None  # why the run changed nothing at all (a damaged state file)
    issues: list[dict[str, Any]] = field(default_factory=list)  # what isn't linked: conflicts, failed builds (attention), clashes (notes)
    deferred: list[str] = field(default_factory=list)  # plugins whose build this run left to the next sync
    state: dict[str, Any] | None = None  # `plugin-parts.json` as this run left it (None: the run didn't get that far)

    @property
    def written(self) -> list[Written]:
        return [change for change in self.changes if change.ok]

    @property
    def failed(self) -> list[Written]:
        return [change for change in self.changes if not change.ok]


def state_path() -> Path:
    return default_root().with_name(f"{KIND}.json")


def load_state(path: Path | None = None) -> dict[str, Any]:
    """`plugin-parts.json`: plugin id -> its record (source, version, digest, parts, links per Kiro label), and the
    last run's report lines. Missing is a first run; unreadable or newer stops the run, so agt never takes its own
    mirrors for edits and its own links for yours."""

    path = path or state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema": STATE_SCHEMA, "plugins": {}, "reports": []}
    except OSError as exc:
        raise StateError(f"agt can't read {path} ({exc})") from None
    fix = "remove it, and the next run builds the mirrors again (the old ones go to the backups)"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"{path} is damaged ({exc}); {fix}") from None
    if not isinstance(data, dict) or not isinstance(data.get("plugins", {}), dict):
        raise StateError(f"{path} is damaged; {fix}")
    schema = data.get("schema")
    if not isinstance(schema, int) or schema > STATE_SCHEMA:
        raise StateError(f"{path} was written by a newer agt (schema {schema}); update agt first")
    return data


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._-]+", "-", label).strip("-") or "kiro"


class _Backups:
    """`state/share/backups/<run>/plugin-parts/`: links under `links/<label>/`, mirrors under `mirrors/<market>/<plugin>`."""

    def __init__(self, run: str):
        from agents_router.share import scope

        self.folder = scope.share_root() / "backups" / run / KIND

    def move(self, path: Path, *parts: str) -> Path:
        target = self.folder.joinpath(*parts)
        fsutil.make_private_dir(target.parent)
        number = 1
        while os.path.lexists(target):
            target = target.with_name(f"{parts[-1]}.{number}")
            number += 1
        shutil.move(str(path), str(target))  # a link moves as a link, a folder as a whole
        return target


def _leads_to(link: Path, folder: Path) -> bool:
    try:
        target = os.readlink(link)
    except OSError:
        return False
    leads = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(link)), target))
    return leads == os.path.normpath(os.path.abspath(folder)) or os.path.realpath(link) == os.path.realpath(folder)


def _user_skills(included: list[providers.Provider], root: Path) -> dict[str, str]:
    """Skill names in every included folder that aren't agt's plugin links (identity -> the first label holding it).
    In a Kiro skills folder any entry counts, since a link can't go where it is."""

    found: dict[str, str] = {}
    for provider in included:
        for location in provider.locations.get("skills", []):
            if not location.path.is_dir():
                continue
            for entry in sorted(location.path.iterdir()):
                if entry.name.startswith(".") or exclude.plugin_part(entry, root):
                    continue
                if provider.cli == "kiro" or entry.is_dir():
                    found.setdefault(names.identity(entry.name), provider.label)
    return found


def run(included: list[providers.Provider], run_id: str, *, root: Path | None = None, build: bool = True,
        picks: Any = None, restore: dict[str, list[str]] | None = None) -> Result:
    """One plugin-parts run for your user folders: build or keep each plugin's mirror, then make each Kiro skills
    folder hold a link per mirror skill, and keep `plugin-parts.json`.

    Call it holding `apply.lock()` for the included folders (taken after the settings lock, never the reverse), and
    before the agents and skills plan, so a link that steps aside for your skill is gone when that plan links your
    skill. `run_id` names the backups folder, `state/share/backups/<run_id>/plugin-parts/`. In a repo's scope it does
    nothing. Rules:

    | Case | What happens |
    |---|---|
    | Your skill of that name in any included folder, or anything else at its Kiro path | yours wins: no link, and agt's link steps aside into the backups |
    | Two plugins with a skill of one name | a reported conflict, neither linked |
    | A mirror changed since agt built it (an edit through Kiro) | it goes to the backups and is rebuilt from the plugin |
    | A plugin uninstalled, off everywhere or project scope only | its links and mirror go to the backups |
    | A plugin update | the mirror is rebuilt in place; the link path has no version, so it never changes |
    | A link you delete in a Kiro folder | that skill stops going there (`stopped` in the state file) until `agt share pick plugin-parts/<name> --in <label>` (`restore`: skill identity -> those labels) |
    | `picks` (your share settings) keep a skill out of a Kiro folder | its link there goes to the backups |
    | A link agt can't move away | its mirror and record stay until the link is gone |
    | A Kiro skills folder agt linked or shared skills into before, missing now | nothing is linked or removed there, and its records stay, until it's back |
    | A Kiro skills folder that is another CLI's folder, or agt's, under another name (a link either way) | nothing is linked or removed there, and it's an attention entry |

    Without `build` (a session start or a save), no mirror is built or hashed: those plugins keep their earlier
    mirror and links, and their ids are in `Result.deferred` for the next sync.
    """

    from agents_router.share import scope

    result = Result(run_id)
    if scope.current().project:
        return result
    targets = [provider for provider in included if provider.cli == "kiro" and provider.writable("skills") is not None]
    if not targets:
        result.reports.append("no Kiro folder is shared, so no plugin parts are linked")
        return result
    root = root if root is not None else default_root()
    try:
        saved = load_state()
    except StateError as exc:
        result.problem = str(exc)
        return result
    previous = {plugin_id: record for plugin_id, record in (saved.get("plugins") or {}).items()
                if isinstance(record, dict) and _valid_id(plugin_id)}
    stopped = _stopped(saved)
    waiting = _waiting(targets, included, previous, result)
    # A link agt made that you deleted since: that skill stops going to that Kiro folder. A folder that's gone
    # altogether stops nothing, and neither does one agt left alone last run (missing, or another CLI's folder under
    # another name): back as a folder of its own, it gets its links again.
    was_waiting = set(saved.get("waiting") or []) if isinstance(saved.get("waiting"), list) else set()
    for provider in targets:
        folder = provider.writable("skills").path
        if provider.label in waiting or provider.label in was_waiting or not folder.is_dir():
            continue
        for record in previous.values():
            for name in (record.get("links") or {}).get(provider.label) or []:
                if not os.path.lexists(folder / str(name)):
                    stopped.setdefault(names.identity(str(name)), set()).add(provider.label)
    for key, labels in (restore or {}).items():  # `pick --in`: wanted there again, a link deleted just now too
        if key in stopped:
            stopped[key] -= set(labels)

    def kept_out(key: str, label: str) -> str | None:
        return _kept_out(picks, stopped, key, label)

    backups = _Backups(run_id)

    def keep_mirror(plugin_id: str, folder: Path) -> None:
        name, _, market = plugin_id.partition("@")
        moved = backups.move(folder, "mirrors", market, name)
        result.changes.append(Written(plugin_id, "", "backup", mirror_path(root, plugin_id), True,
                                      "the mirror agt replaced, edits included", moved))

    outcome = project(included, root, previous, backup=keep_mirror, build=build)
    result.outcome = outcome
    selection = outcome.selection
    result.reports += [f"{label}: agt can't read its plugins now ({why})" for label, why in selection.unreadable.items()]
    result.reports += selection.reports
    for plugin_id, projection in outcome.projections.items():
        result.reports += [f"{plugin_id}: {line}" for line in projection.reports]
        if projection.held:
            result.reports.append(f"{plugin_id}: kept as it was ({projection.held})")
        if projection.deferred:
            result.deferred.append(plugin_id)
        if projection.failed:
            result.changes.append(Written(plugin_id, "", "rebuild", projection.mirror, False, projection.failed))
            result.issues.append({"kind": "part-failed", "item": f"{KIND}/{plugin_id}", "name": plugin_id,
                                  "message": f"agt couldn't build the {plugin_id} plugin's parts for Kiro: {projection.failed}",
                                  "fix": ["agt share sync"]})
        elif projection.rebuilt:
            result.changes.append(Written(plugin_id, "", "rebuild", projection.mirror, True,
                                          f"{projection.source.version} from {projection.source.label}"))

    # Which name goes where: one plugin per name, and never over a skill of yours.
    offers: dict[str, list[tuple[str, Part]]] = {}
    for plugin_id, projection in sorted(outcome.projections.items()):
        if projection.record is not None:
            for part in [*projection.skills, *projection.commands]:
                offers.setdefault(names.identity(part.name), []).append((plugin_id, part))
    yours = _user_skills(included, root)
    wanted: dict[str, tuple[str, Part]] = {}
    why_not: dict[str, str] = {}
    labels = [provider.label for provider in targets]
    for key, offered in sorted(offers.items()):
        name = offered[0][1].name
        open_in = [label for label in labels if not kept_out(key, label)]  # none: you took it out everywhere
        keep_out = f"agt share pick {KIND}/{name} --not-in {','.join(open_in)}"
        if len(offered) > 1:
            plugin_ids = [plugin_id for plugin_id, _ in offered]
            why_not[key] = f"the plugins {', '.join(plugin_ids)} all have a skill {name}"
            result.reports.append(f"{name}: not linked, {why_not[key]}")
            if open_in:
                result.issues.append({"kind": "part-conflict", "item": f"{KIND}/{key}", "name": name, "plugins": plugin_ids,
                                      "message": f"{name} is a skill in {_and(plugin_ids)}, so neither goes to Kiro; turn "
                                                 "one plugin off in Claude Code, or leave it out", "fix": [keep_out]})
        elif key in yours:
            why_not[key] = f"your own skill {name} in {yours[key]} comes first"
            result.reports.append(f"{name} ({offered[0][0]}): not linked, {why_not[key]}")
            if open_in:  # the designed outcome, so a note in `status`, never an attention entry
                result.issues.append({"kind": "part-clash", "item": f"{KIND}/{key}", "name": name, "plugins": [offered[0][0]],
                                      "label": yours[key],
                                      "message": f"your own skill {name} in {yours[key]} comes first, so the {offered[0][0]} "
                                                 f"plugin's goes to no Kiro folder; rename yours to get both, or `{keep_out}` "
                                                 "leaves the plugin's out", "fix": [keep_out]})
        else:
            wanted[key] = offered[0]

    made: list[Path] = []
    removed: list[Path] = []
    linked: dict[str, dict[str, list[str]]] = {}  # plugin id -> label -> names linked there now
    for provider in targets:
        folder = provider.writable("skills").path
        label = provider.label
        if label in waiting:  # left alone: the links recorded there stay recorded
            for plugin_id, record in previous.items():
                held = (record.get("links") or {}).get(label)
                if isinstance(held, list) and held:
                    linked.setdefault(plugin_id, {})[label] = [str(name) for name in held]
            continue
        present = {}
        if folder.is_dir():
            present = {entry.name: entry for entry in sorted(folder.iterdir())
                       if not entry.name.startswith(".") and exclude.plugin_part(entry, root)}
        have: set[str] = set()
        for name, entry in present.items():
            key = names.identity(name)
            out = kept_out(key, label)
            offer = wanted.get(key) if not out else None
            if offer is not None and name == offer[1].name and _leads_to(entry, offer[1].folder):
                have.add(key)
                linked.setdefault(offer[0], {}).setdefault(label, []).append(name)
                continue
            if offer is not None and name == offer[1].name:
                change = _relink(entry, offer[1], label, backups, root)
                result.changes.append(change)
                if change.ok:
                    have.add(key)
                    linked.setdefault(offer[0], {}).setdefault(label, []).append(name)
                else:
                    _still(entry, name, label, root, outcome, linked)
                continue
            why = out or why_not.get(key) or (f"agt links it as {offer[1].name} now" if offer is not None
                                              else "its plugin isn't projected any more")
            change = _take_away(entry, name, label, why, backups, root)
            result.changes.append(change)
            if change.ok:
                removed.append(entry)
            else:
                _still(entry, name, label, root, outcome, linked)
        for key, (plugin_id, part) in sorted(wanted.items()):
            if key in have or kept_out(key, label):
                continue
            change = _link(folder / part.name, part, label)
            result.changes.append(change)
            if change.ok:
                made.append(change.path)
                linked.setdefault(plugin_id, {}).setdefault(label, []).append(part.name)
            elif change.message.startswith(NOT_NOW):
                result.changes.pop()
                result.reports.append(f"{part.name} ({plugin_id}): not linked in {label}, {change.message[len(NOT_NOW):]}")

    # Mirrors of plugins that aren't projected any more go to the backups (their links went above), unless a link
    # still leads into one: it and its record stay until the link is gone.
    records: dict[str, dict[str, Any]] = {}
    for plugin_id in sorted(set(previous) - set(outcome.projections)):
        mirror = mirror_path(root, plugin_id)
        if linked.get(plugin_id):
            records[plugin_id] = {**previous[plugin_id], "links": _sorted_links(linked[plugin_id])}
            result.reports.append(f"{plugin_id}: its mirror stays while agt's links to it in "
                                  f"{_and(sorted(linked[plugin_id]))} do")
            continue
        if not os.path.lexists(mirror):
            continue
        name, _, market = plugin_id.partition("@")
        try:
            moved = backups.move(mirror, "mirrors", market, name)
        except OSError as exc:
            result.changes.append(Written(plugin_id, "", "remove", mirror, False,
                                          f"couldn't move it to the backups ({exc}); the next run tries again"))
            records[plugin_id] = previous[plugin_id]
            continue
        result.changes.append(Written(plugin_id, "", "remove", mirror, True, "its plugin isn't projected any more", moved))
    for plugin_id, projection in outcome.projections.items():
        if projection.record is not None:
            records[plugin_id] = {**projection.record, "links": _sorted_links(linked.get(plugin_id, {}))}
    kept = {"schema": STATE_SCHEMA, "plugins": records, "reports": result.reports, "issues": result.issues,
            "stopped": {key: sorted(found) for key, found in sorted(stopped.items()) if found},
            **({"waiting": sorted(waiting)} if waiting else {})}
    if kept != saved:  # the watcher runs every minute: a run that changed nothing writes nothing
        fsutil.write_json(state_path(), kept)
    result.state = kept
    if made:
        result.excluded = gitexclude.update(made)
    if removed:
        gitexclude.update(removed, remove=True)  # the links are in the backups now
    _manifest(backups, result)
    return result


NOT_NOW = "skipped this run: "


def _valid_id(plugin_id: str) -> bool:
    name, _, market = plugin_id.partition("@")
    return bool(SEGMENT_RE.match(name) and SEGMENT_RE.match(market))


def _sorted_links(found: dict[str, list[str]]) -> dict[str, list[str]]:
    return {label: sorted(set(held)) for label, held in sorted(found.items()) if held}


def _still(entry: Path, name: str, label: str, root: Path, outcome: Outcome,
           linked: dict[str, dict[str, list[str]]]) -> None:
    """A link agt couldn't move away that still leads into the mirror of a plugin no longer projected: that plugin's
    mirror and record stay, holding it."""

    owner = exclude.plugin_part(entry, root) if os.path.lexists(entry) else None
    if owner and owner not in outcome.projections and _valid_id(owner):
        linked.setdefault(owner, {}).setdefault(label, []).append(name)


def _aliased(provider: providers.Provider, included: list[providers.Provider]) -> str | None:
    """Why a Kiro skills folder is another CLI's folder, or agt's, under another name (a link either way), or None:
    what agt linked there would land in that folder too."""

    from agents_router.share import scope

    folder = provider.writable("skills").path
    real = os.path.realpath(folder)
    others = [other for other in included if other.cli != "kiro"]
    places = [(other.label, location.path) for other in others
              for found in other.locations.values() for location in found]
    places += [(other.label, other.folder) for other in others] + [("agt", scope.share_root())]  # the closest first
    for label, path in places:
        there = os.path.realpath(path)
        if there == real:
            return f"{folder} is {path} ({label}'s) under another name"
        if _inside(real, there):
            return f"{folder} is a folder inside {path} ({label}'s) under another name"
        if _inside(there, real):
            return f"{path} ({label}'s) is a folder inside {folder} under another name"
    return None


def _synced(label: str, previous: dict[str, dict[str, Any]]) -> bool:
    """Whether agt linked plugin parts or shared skills into this Kiro label before."""

    if any(isinstance(record.get("links"), dict) and record["links"].get(label) for record in previous.values()):
        return True
    try:
        current = state.load()
    except state.StateError:
        return True  # unknown, so the folder isn't made again
    return any(key.startswith("skills/") and label in (record.get("copies") or {})
               for key, record in current.items.items() if isinstance(record, dict))


def _waiting(targets: list[providers.Provider], included: list[providers.Provider], previous: dict[str, dict[str, Any]],
             result: Result) -> dict[str, str]:
    """The Kiro labels this run leaves alone, label -> why."""

    waiting: dict[str, str] = {}
    for provider in targets:
        label, folder = provider.label, provider.writable("skills").path
        if why := _aliased(provider, included):
            waiting[label] = why
            message = (f"agt links no plugin parts into {label}: {why}, so they'd land there too; "
                       f"make {folder} a folder of its own, then sync")
            result.reports.append(message)
            result.issues.append({"kind": "part-failed", "item": KIND, "name": "", "label": label, "message": message,
                                  "fix": ["agt share sync"]})
        elif not folder.is_dir() and _synced(label, previous):
            waiting[label] = f"{label}'s skills folder is missing: nothing is linked or removed there until it's back"
            result.reports.append(waiting[label])
    return waiting


def _link(path: Path, part: Part, label: str) -> Written:
    """A new link at `path` to the mirror skill folder: never over something there, never a copy."""

    if os.path.lexists(path):
        return Written(part.name, label, "link", path, False, f"{NOT_NOW}{path} appeared while agt was working")
    if not part.folder.is_dir():
        return Written(part.name, label, "link", path, False, f"{NOT_NOW}its mirror folder {part.folder} isn't there")
    try:
        how = fsutil.make_dir_link(part.folder, path)
    except OSError as exc:
        return Written(part.name, label, "link", path, False, f"couldn't make the link ({exc}); the next run tries again")
    if how == "copy":
        shutil.rmtree(path, ignore_errors=True)
        return Written(part.name, label, "link", path, False, "this system can't make folder links here")
    if not _leads_to(path, part.folder):
        if os.path.lexists(path) and fsutil.is_link(path):
            fsutil.remove_link(path)
        return Written(part.name, label, "link", path, False, "the link didn't lead to the mirror, so agt took it away")
    return Written(part.name, label, "link", path, True)


def _take_away(path: Path, name: str, label: str, why: str, backups: _Backups, root: Path) -> Written:
    if exclude.plugin_part(path, root) is None:
        return Written(name, label, "remove", path, False, f"{NOT_NOW}{path} changed while agt was working")
    try:
        moved = backups.move(path, "links", _safe(label), name)
    except OSError as exc:
        return Written(name, label, "remove", path, False, f"couldn't move it to the backups ({exc}); the next run tries again")
    return Written(name, label, "remove", path, True, why, moved)


def _relink(path: Path, part: Part, label: str, backups: _Backups, root: Path) -> Written:
    """agt's link of this name leads elsewhere (the skill moved in its plugin, or another plugin has the name now)."""

    away = _take_away(path, part.name, label, "", backups, root)
    if not away.ok:
        away.action = "relink"
        return away
    made = _link(path, part, label)
    if not made.ok:
        if not os.path.lexists(path):  # put the old link back, never into something that appeared there
            try:
                shutil.move(str(away.backup), str(path))
            except OSError:
                pass
        made.action = "relink"
        return made
    return Written(part.name, label, "relink", path, True, backup=away.backup)


def _manifest(backups: _Backups, result: Result) -> None:
    """`backups/<run>/plugin-parts/manifest.json`: what went into this run's backups, from where and why. It sits
    beside the run's own manifest, so `agt share undo` doesn't try to put agt's links back."""

    moved = [{"name": change.name, "label": change.label, "action": change.action, "path": str(change.path),
              "backup": str(change.backup), "why": change.message} for change in result.written if change.backup]
    if moved:
        fsutil.make_private_dir(backups.folder)
        fsutil.write_json(backups.folder / "manifest.json", {"run": result.run, "time": time.time_ns(), "changes": moved})


def _and(found: list[str]) -> str:
    return found[0] if len(found) == 1 else ", ".join(found[:-1]) + " and " + found[-1] if found else ""


def _stopped(saved: dict[str, Any]) -> dict[str, set[str]]:
    """Skill identity -> the Kiro labels where you deleted agt's link."""

    found = saved.get("stopped") if isinstance(saved.get("stopped"), dict) else {}
    return {str(key): {str(label) for label in labels} for key, labels in found.items() if isinstance(labels, list)}


def _kept_out(picks: Any, stopped: dict[str, set[str]], key: str, label: str) -> str | None:
    """Why a skill (its identity) doesn't go to this Kiro folder by your choice, or None."""

    pick = f"{KIND}/{key}"
    if picks is not None:
        if pick in picks.off:
            return "you turned it off"
        if label in picks.not_in.get(pick, []):
            return f"you took it out of {label}"
        only = picks.only.get(pick)
        if only is not None and label not in only:
            return f"you keep it in {_and(sorted(only))} only" if only else "you keep it out of every CLI"
    if label in stopped.get(key, ()):
        return f"you deleted its link in {label}"
    return None


def _write_state(data: dict[str, Any]) -> None:
    fsutil.write_json(state_path(), data)


def recorded_labels() -> list[str]:
    """The Kiro labels the state file holds links for."""

    try:
        saved = load_state()
    except StateError:
        return []
    return sorted({label for record in (saved.get("plugins") or {}).values() if isinstance(record, dict)
                   for label in (record.get("links") or {})})


def forget_label(labels: set[str], *, stops: bool = True) -> None:
    """A Kiro folder that stops sharing: the state file forgets its links (they stay as your files, or went already),
    and with `stops`, the links you deleted there."""

    try:
        saved = load_state()
    except StateError:
        return
    if not state_path().exists():
        return
    for record in (saved.get("plugins") or {}).values():
        if isinstance(record, dict) and isinstance(record.get("links"), dict):
            record["links"] = {label: found for label, found in record["links"].items() if label not in labels}
    stopped = {key: found - labels if stops else found for key, found in _stopped(saved).items()}
    saved["stopped"] = {key: sorted(found) for key, found in sorted(stopped.items()) if found}
    _write_state(saved)


def detach(included: list[providers.Provider], run_id: str, labels: set[str], *, root: Path | None = None) -> Result:
    """`agt share cli remove <kiro> --keep-copies`: agt's links in those Kiro folders become folders of your own, each a
    copy of what its link led to, so none follows or loses a mirror agt goes on changing for the Kiro folders still
    sharing. The state file forgets those folders once every link is a folder. A kept copy names its plugin's other
    files (its scripts) by their mirror paths, as the link did. Call it holding the lock."""

    from agents_router.share import scope

    result = Result(run_id)
    if scope.current().project:
        return result
    root = root if root is not None else default_root()
    backups = _Backups(run_id)
    # A Kiro folder that is another CLI's under another name: a copy there would be that CLI's skill too, so agt's link
    # goes to the backups instead.
    aliased = {provider.label for provider in included if provider.cli == "kiro" and provider.label in labels
               and provider.writable("skills") is not None and _aliased(provider, included)}
    for label, path, _ in links(included, labels, root):
        result.changes.append(_take_away(path, path.name, label, "it would land in another CLI's folder too", backups,
                                         root) if label in aliased else _detach(path, label))
    if not result.failed:
        forget_label(labels)
    _manifest(backups, result)
    return result


def _detach(path: Path, label: str) -> Written:
    """Swap one link for a copy of the folder it leads to: copied next to it, then put in its place by rename."""

    copy = fsutil.temp_sibling(path)
    aside = path.with_name(f".{path.name}.{secrets.token_hex(6)}.old")
    try:
        shutil.copytree(path, copy, symlinks=False, ignore_dangling_symlinks=True, copy_function=_copy_file)
        os.rename(path, aside)
        try:
            os.rename(copy, path)
        except OSError:
            os.rename(aside, path)
            raise
        fsutil.remove_link(aside)
    except (OSError, shutil.Error, RecursionError) as exc:
        if os.path.lexists(copy):
            _remove_tree(copy)
        return Written(path.name, label, "detach", path, False,
                       f"couldn't make it a folder of its own ({exc}); run it again to finish")
    return Written(path.name, label, "detach", path, True, "a folder of your own now")


def links(included: list[providers.Provider], labels: set[str] | None = None,
          root: Path | None = None) -> list[tuple[str, Path, str]]:
    """agt's plugin-part links in the Kiro folders sharing (only `labels`, when given): (label, path, plugin id)."""

    root = root if root is not None else default_root()
    found = []
    for provider in included:
        location = provider.writable("skills") if provider.cli == "kiro" else None
        if location is None or (labels is not None and provider.label not in labels) or not location.path.is_dir():
            continue
        for entry in sorted(location.path.iterdir()):
            plugin_id = exclude.plugin_part(entry, root)
            if plugin_id and not entry.name.startswith("."):
                found.append((provider.label, entry, plugin_id))
    return found


def remove(included: list[providers.Provider], run_id: str, *, labels: set[str] | None = None,
           root: Path | None = None) -> Result:
    """Move agt's plugin-part links out of Kiro folders into `backups/<run_id>/plugin-parts/`, holding the lock.

    With `labels`, those folders only (`agt share cli remove`), and the state file forgets them. Without, every Kiro
    folder sharing (`agt share kinds remove plugin-parts --remove-copies`): the mirrors go to the backups too, and
    the state file goes, once every link moved.
    """

    from agents_router.share import scope

    result = Result(run_id)
    if scope.current().project:
        return result
    root = root if root is not None else default_root()
    backups = _Backups(run_id)
    moved: list[Path] = []
    for label, path, _ in links(included, labels, root):
        change = _take_away(path, path.name, label, "plugin parts don't go there any more", backups, root)
        result.changes.append(change)
        if change.ok:
            moved.append(path)
    if labels is not None:
        forget_label(labels)
    elif not result.failed:
        if os.path.lexists(root):
            try:
                kept = backups.move(root, "mirrors")
                result.changes.append(Written("mirrors", "", "remove", root, True, "plugin parts don't go to Kiro any more",
                                              kept))
            except OSError as exc:
                result.changes.append(Written("mirrors", "", "remove", root, False,
                                              f"couldn't move them to the backups ({exc}); run it again to finish"))
        if not result.failed:
            state_path().unlink(missing_ok=True)
    if moved:
        gitexclude.update(moved, remove=True)
    _manifest(backups, result)
    return result


def watched_files(included: list[providers.Provider]) -> list[Path]:
    """What an install, an update or turning a plugin on or off changes: each Claude folder's plugin inventory and
    `settings.json` (with `known_marketplaces.json`)."""

    return plugins.watched_files(included)


# What status and list show: read from the state file, never built ------------------------------


@dataclass
class View:
    labels: list[str]  # the Kiro folders sharing
    others: dict[str, str]  # label -> cli of every other folder sharing
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    stopped: dict[str, set[str]] = field(default_factory=dict)
    picks: Any = None
    problem: str | None = None

    def parts(self) -> dict[str, dict[str, Any]]:
        """Skill identity -> its name, the plugins offering it and the Kiro labels holding agt's link now."""

        found: dict[str, dict[str, Any]] = {}
        for plugin_id, record in sorted(self.records.items()):
            links = record.get("links") if isinstance(record.get("links"), dict) else {}
            linked = {label: {name for name in held if isinstance(name, str)}
                      for label, held in links.items() if isinstance(held, list)}
            for part in [*(record.get("skills") or []), *(record.get("commands") or [])]:
                if not isinstance(part, dict) or "name" not in part:
                    continue
                name = str(part["name"])
                entry = found.setdefault(names.identity(name), {"name": name, "plugins": [], "linked": set()})
                entry["plugins"].append(plugin_id)
                entry["linked"] |= {label for label, held in linked.items() if name in held}
        return found

    def notes(self) -> list[str]:
        """`status` notes: a skill of yours that comes first, so the plugin's of that name goes to no Kiro folder."""

        found = [str(issue.get("message") or "") for issue in self.issues if issue.get("kind") == "part-clash"]
        return [f"{message[:1].upper()}{message[1:]}." for message in found if message]

    def left_out(self, named: bool = False) -> list[str]:
        """`status` and `sync` notes: what each plugin's mirror leaves out, one note per plugin and reason, never an
        attention entry. `named` (-v, --json) adds the names. A plugin goes by its name unless two plugins share it."""

        short = {plugin_id: plugin_id.partition("@")[0] for plugin_id in self.records}
        taken = list(short.values())
        notes = []
        for plugin_id, record in sorted(self.records.items()):
            grouped: dict[tuple[str, str], list[str]] = {}
            for entry in record.get("left_out") or []:
                if isinstance(entry, dict) and (entry.get("part"), entry.get("why")) in LEFT_OUT:
                    grouped.setdefault((entry["part"], entry["why"]), []).append(str(entry.get("name") or ""))
            plugin = short[plugin_id] if taken.count(short[plugin_id]) == 1 else plugin_id
            for key in (key for key in LEFT_OUT if key in grouped):
                found = grouped[key]
                one, several = LEFT_OUT[key]
                text = one if len(found) == 1 else several.format(n=len(found))
                notes.append(f"{plugin}: {text}" + (f" ({', '.join(found)})" if named else "") + ".")
        return notes

    def summary(self) -> dict[str, Any]:
        parts = self.parts()
        return {"plugins": len(self.records), "skills": len(parts),
                "linked": {label: sum(1 for entry in parts.values() if label in entry["linked"]) for label in self.labels},
                "issues": len(self.issues), "folders": list(self.labels), "problem": self.problem}

    def cells(self, key: str, entry: dict[str, Any], cell: Callable[..., dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """One plugin part's cell per folder, for `agt share list`."""

        from agents_router.share import plan as planner

        issue = next((found for found in self.issues if found.get("item") == f"{KIND}/{key}"), None)
        cells = {}
        for label in self.labels:
            out = _kept_out(self.picks, self.stopped, key, label)
            if label in entry["linked"]:
                cells[label] = cell(planner.HAS, f"linked from the {_and(entry['plugins'])} plugin")
            elif out:
                cells[label] = cell(planner.SKIPPED, out)
            elif issue is not None:
                cells[label] = cell(planner.BLOCKED, issue["message"], issue["message"])
            else:
                cells[label] = cell(planner.COPY, "the next sync links it")
        for label, cli in self.others.items():
            why = "loads it from the plugin" if cli in ("claude", "codex") else "plugin parts go to Kiro only"
            cells[label] = cell(planner.BLOCKED, why, why)
        return cells


def view(included: list[providers.Provider], picks: Any = None, run_state: dict[str, Any] | None = None) -> View:
    """What the last run left, for `status` and `list`: nothing is read from the plugins and nothing is built.
    `run_state` is that run's `Result.state`, when the caller has it; without, the state file is read."""

    found = View([provider.label for provider in included if provider.cli == "kiro"],
                 {provider.label: provider.cli for provider in included if provider.cli != "kiro"}, picks=picks)
    saved = run_state
    if saved is None:
        try:
            saved = load_state()
        except StateError as exc:
            found.problem = str(exc)
            return found
    found.records = {plugin_id: record for plugin_id, record in (saved.get("plugins") or {}).items()
                     if isinstance(record, dict) and _valid_id(plugin_id)}
    found.issues = [issue for issue in saved.get("issues") or [] if isinstance(issue, dict) and issue.get("item")]
    found.stopped = _stopped(saved)
    return found


def attention(shown: View) -> list[dict[str, Any]]:
    """The attention list's entries: two plugins with one skill, a build that failed, and a state file agt can't use.
    Each has its fix. Your own skill coming first is the designed outcome, so it's one of `View.notes`, not here."""

    from agents_router.share import attention as listed

    who = {"part-conflict": "choice", "part-failed": "action"}
    found = [listed._entry(issue["kind"], issue["item"], who[issue["kind"]], str(issue.get("message") or ""),
                           [str(fix) for fix in issue.get("fix") or []], str(issue.get("label") or ""))
             for issue in shown.issues if issue.get("kind") in who]
    if shown.problem:
        found.append(listed._entry("part-failed", KIND, "action", shown.problem, []))
    return found
