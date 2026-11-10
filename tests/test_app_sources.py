"""Portable source gates for the app; no build tools, network, or real CLI calls.

Capability declaration: ui/window-permissions.json lists the exact, individual
Tauri permission IDs used by each window (including app-command permissions).
Each capability must name that one window and match its declaration exactly.
Steps 4–6 update both when adding calls to the single ui/tauri.js adapter. Broad
default groups, wildcards, scopes, and remote capabilities cannot pass this gate.

The Safari denylist comes from plan.md §8 row 15, research-tauri.md R1/R4, and
probe-tauri/css-scan.py / css-counts.md. It deliberately also bans all nesting.
This is a small regression lint, not a complete browser compatibility database;
the plan's real macOS 14 WebKit check is still required. JS view-transition calls
and camelCase CSS properties are checked as well as stylesheet spellings.

Fixture coverage follows models.rs's literal FIXTURES inventory and parse! arms.
Every CommandKind needs a golden file; every DTO must be reachable through a
fixture-backed decode type, including nested structs and error/choice envelopes.
Rust's cargo tests do the actual serde decoding; this guard needs no Rust on PATH.
"""

import json
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
import unittest

import helpers  # noqa: F401 (isolate homes, CLIs and background services)

ROOT = Path(__file__).resolve().parents[1]
TAURI = Path("macos/src-tauri")
UI = Path("macos/ui")

# Each entry has an executable bad example below. Features already in Safari
# 17.0 (:has, color-mix, @property, @container) remain allowed.
SAFARI_DENY = {
    "transition-behavior": r"\b(?:transition-behavior|transitionBehavior)\b",
    "allow-discrete": r"\ballow-discrete\b",
    "starting-style": r"@starting-style\b",
    "text-wrap balance/pretty": r"\b(?:text-wrap(?:-style)?|textWrap(?:Style)?)\s*[:=]\s*['\"]?(?:balance|pretty)\b",
    "view transitions": r"\b(?:view-transition[\w-]*|viewTransition\w*|startViewTransition)\b|@view-transition\b",
    "anchor positioning": r"\b(?:anchor-name|anchor-scope|position-anchor|position-area|position-try[\w-]*|"
                          r"anchorName|anchorScope|positionAnchor|positionArea|positionTry\w*)\b|\banchor(?:-size)?\s*\(",
    "field-sizing": r"\b(?:field-sizing|fieldSizing)\b",
    "interpolate-size": r"\b(?:interpolate-size|interpolateSize)\b",
    "calc-size": r"\bcalc-size\s*\(",
    "light-dark": r"\blight-dark\s*\(",
    "scope": r"@scope\b",
}
MOTION = re.compile(
    r"(?<![\w-])(?:\d*\.)?\d+\s*(?:ms|s)\b"
    r"|\b(?:cubic-bezier|steps|linear)\s*\("
    r"|\b(?:ease(?:-in-out|-in|-out)?|linear|step-start|step-end)\b"
    r"|\b(?:duration|delay|easing|animationDuration|animationDelay|transitionDuration|transitionDelay)"
    r"\s*[:=]\s*(?:\d|['\"]\d)", re.I)


def uncomment(text):
    # Keep quoted strings (URLs in CSP and CSS/JS values are significant).
    return re.sub(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|/\*.*?\*/|//[^\n]*)''',
                  lambda m: "" if m[0].startswith(("/*", "//")) else m[0], text, flags=re.S)


def nested_css(text):
    """Distinguish nested selectors from flat rules inside media/keyframes blocks."""
    text = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', "", text, flags=re.X)
    stack, start = [], 0
    for match in re.finditer(r"[{};]", text):
        token = match[0]
        if token == "{":
            prelude = text[start:match.start()].strip()
            if "style" in stack:
                return True
            parent_keyframes = bool(stack and stack[-1] == "keyframes")
            stack.append("keyframes" if re.match(r"@(?:-webkit-)?keyframes\b", prelude)
                         else "group" if prelude.startswith("@") or parent_keyframes else "style")
        elif token == "}" and stack:
            stack.pop()
        start = match.end()
    return False


def check_sources(root):
    errors = []

    def fail(rule, detail):
        errors.append(f"[{rule}] {detail}")

    def load_json(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            fail("source", f"{path.relative_to(root)}: {error}")
            return {}

    cargo = root / TAURI
    manifest = tomllib.loads((cargo / "Cargo.toml").read_text(encoding="utf-8"))

    def dependencies(table):
        for key, value in table.items():
            if key in ("dependencies", "dev-dependencies", "build-dependencies"):
                yield from value.items()
            elif isinstance(value, dict):
                yield from dependencies(value)

    for name, value in dependencies(manifest):
        version = value if isinstance(value, str) else value.get("version", "")
        if (not isinstance(version, str) or not re.fullmatch(r"=\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version)
                or isinstance(value, dict) and any(key in value for key in ("git", "path", "branch", "rev"))):
            fail("pins", f"{name} needs an exact = version")
        if name == "tauri-plugin-shell" or (isinstance(value, dict) and value.get("package") == "tauri-plugin-shell"):
            fail("shell", "tauri-plugin-shell is forbidden")
    lock = cargo / "Cargo.lock"
    if not lock.is_file():
        fail("lock", "Cargo.lock is missing")
    elif any(item.get("name") == "tauri-plugin-shell" for item in tomllib.loads(lock.read_text()).get("package", [])):
        fail("shell", "tauri-plugin-shell in Cargo.lock")

    config = load_json(cargo / "tauri.conf.json")
    # Include checked-in platform/release overlays and the generated release
    # overlay when present, but never traverse build/tools or Cargo caches.
    configs = set(cargo.glob("tauri*.conf.json")) | set(cargo.glob("tauri*.conf.toml"))
    configs |= set((root / "build").glob("*release*.json"))
    for path in sorted(configs):
        if path.name == "tauri.test.conf.json":
            continue  # Local updater-feed testing is the sole exception.
        data = tomllib.loads(path.read_text()) if path.suffix == ".toml" else load_json(path)
        if "dangerousInsecureTransportProtocol" in json.dumps(data):
            fail("transport", f"{path.relative_to(root)} enables insecure updater transport")
        security = data.get("app", {}).get("security", {})
        for key in ("csp", "devCsp"):
            if key not in security and path.name != "tauri.conf.json":
                continue
            if key == "devCsp" and key not in security:
                continue
            csp = security.get(key)
            if isinstance(csp, str):
                directives = {}
                for part in csp.split(";"):
                    words = part.split()
                    if words:
                        if words[0] in directives:
                            fail("csp", "duplicate CSP directive")
                        directives[words[0]] = words[1:]
            elif isinstance(csp, dict):
                directives = {name: value.split() if isinstance(value, str) else value for name, value in csp.items()}
            else:
                directives = {}
            if not directives.get("default-src") or not directives.get("connect-src"):
                fail("csp", "default-src and IPC-only connect-src are required")
            for name, values in directives.items():
                allowed = {"'self'", "'none'"} if name != "connect-src" else {"'none'", "ipc:", "http://ipc.localhost"}
                if not isinstance(values, list) or not set(values) <= allowed:
                    fail("csp", f"{path.relative_to(root)}: nonlocal or unsafe {name}")
        for window in data.get("app", {}).get("windows", []):
            if re.match(r"(?:\w+:|//)", window.get("url", "")):
                fail("csp", "remote window content is forbidden")

    declared = load_json(root / UI / "window-permissions.json")
    windows = {window["label"] for window in config.get("app", {}).get("windows", [])}
    if set(declared) != windows:
        fail("capability", "window-permissions.json must declare every window")
    capabilities = {}
    for path in sorted((cargo / "capabilities").glob("*.json")):
        cap = load_json(path)
        label = cap.get("identifier")
        capabilities[label] = cap
        permissions = cap.get("permissions", [])
        if (label != path.stem or cap.get("windows") != [label] or label not in declared
                or cap.get("remote") or cap.get("webviews") or cap.get("local") is False):
            fail("capability", f"{path.name} must target its one local window")
        expected = declared.get(label, [])
        if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
            fail("capability", f"{path.name} must use individual permission IDs")
            continue
        if (not isinstance(expected, list) or not all(isinstance(p, str) for p in expected)
                or set(permissions) != set(expected) or len(permissions) != len(set(permissions))):
            fail("capability", f"{path.name} permissions do not match window use")
        if any(p == "default" or p.endswith(":default") or "*" in p or p.startswith("shell:") for p in permissions):
            fail("capability", f"{path.name} grants broad or shell permissions")
    enabled = config.get("app", {}).get("security", {}).get("capabilities")
    if set(capabilities) != windows or not isinstance(enabled, list) or set(enabled) != windows:
        fail("capability", "enable exactly one explicit capability per window")

    for path in sorted((root / UI).rglob("*")):
        if path.suffix not in (".css", ".js", ".html"):
            continue
        text = uncomment(path.read_text(encoding="utf-8"))
        # Names such as var(--ease-out) / getPropertyValue('--ease-out') are
        # token references, not literal curves. Keep any var() fallback value.
        motion_text = re.sub(r"--[\w-]+", "", text)
        if path != root / UI / "motion.css" and MOTION.search(motion_text):
            fail("motion", f"{path.relative_to(root)} has a motion literal outside motion.css")
        for feature, pattern in SAFARI_DENY.items():
            if re.search(pattern, text, re.I):
                fail("safari", f"{path.relative_to(root)} uses {feature}")
        styles = text if path.suffix == ".css" else "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", text, re.S | re.I))
        if nested_css(styles):
            fail("safari", f"{path.relative_to(root)} uses CSS nesting")

    for path in sorted((root / "macos").rglob("*")):
        if any(part in ("target", "gen") for part in path.relative_to(root / "macos").parts):
            continue
        if re.search(r"(?:sf[ -_]?symbols?|\.symbolset|\.symbol\.svg)", path.name, re.I):
            fail("symbols", f"SF Symbol asset: {path.relative_to(root)}")
        if not path.is_file() or path.suffix not in (".rs", ".js", ".html", ".svg", ".json", ".toml"):
            continue
        text = uncomment(path.read_text(encoding="utf-8"))
        if re.search(r"\bset_title\s*(?:::<[^>]+>)?\s*\(", text):
            fail("title", f"{path.relative_to(root)} calls set_title")
        if re.search(r"tauri[_-]plugin[_-]shell", text):
            fail("shell", f"{path.relative_to(root)} references plugin-shell")
        if (re.search(r"SF[ -]Symbols|com\.apple\.SF|(?:imageWith|system)SymbolName", text, re.I)
                or path.suffix == ".svg" and re.search(r'''<g\b[^>]*\bid=["'](?:Symbols|Guides|Notes)["']''', text)):
            fail("symbols", f"{path.relative_to(root)} references SF Symbols")

    models = uncomment((cargo / "src/models.rs").read_text(encoding="utf-8"))
    inventory = models.split("pub const FIXTURES:", 1)[-1].split("];", 1)[0]
    entries = re.findall(r'\(\s*"([^"\n]+\.json)"\s*,\s*CommandKind::(\w+)\s*,?\s*\)', inventory)
    files = {path.name for path in (root / "tests/fixtures/app").glob("*.json")}
    names = {name for name, _ in entries}
    if names != files or len(names) != len(entries) or not entries:
        fail("fixtures", f"inventory/files differ: missing={sorted(names - files)}, unlisted={sorted(files - names)}")
    kinds = re.search(r"pub enum CommandKind\s*\{([^}]+)\}", models)
    if not kinds or set(re.findall(r"\b\w+\b", kinds[1])) != {kind for _, kind in entries}:
        fail("fixtures", "every CommandKind needs a fixture")
    decoders = {}
    for arm, model in re.findall(r"((?:CommandKind::\w+\s*(?:\|\s*)?)+)=>\s*(?:\{\s*)?parse!\((\w+),", models):
        for kind in re.findall(r"CommandKind::(\w+)", arm):
            decoders[kind] = model
    covered = set()
    for name, kind in entries:
        if name not in files:
            continue
        value = load_json(root / "tests/fixtures/app" / name)
        model = "NeedsChoice" if value.get("error") == "needs_choice" else (
            "ErrorEnvelope" if value.get("ok") is False else decoders.get(kind))
        if model:
            covered.add(model)
        else:
            fail("fixtures", f"no decode model for {name}: {kind}")
    bodies = dict(re.findall(r"pub (?:struct|enum) (\w+)[^{;]*\{([^}]+)\}", models))
    bodies.update({name: body for name, body in re.findall(r"pub struct (\w+)\(([^;]+)\);", models)})
    pending = list(covered)
    while pending:
        for nested in set(re.findall(r"\b\w+\b", bodies.get(pending.pop(), ""))) & set(bodies) - covered:
            covered.add(nested)
            pending.append(nested)
    missing = set(bodies) - {"CommandKind", "Snapshot"} - covered
    if missing:
        fail("fixtures", f"models without a fixture-backed decode: {sorted(missing)}")
    return errors


class AppSourceTests(unittest.TestCase):
    def test_current_tree(self):
        self.assertEqual(check_sources(ROOT), [])

    def temp_tree(self):
        temporary = tempfile.TemporaryDirectory(prefix="agt-app-sources-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copytree(ROOT / "macos", root / "macos", ignore=shutil.ignore_patterns("target", "gen"))
        shutil.copytree(ROOT / "tests/fixtures/app", root / "tests/fixtures/app")
        return root

    def reject(self, rule, mutate):
        root = self.temp_tree()
        mutate(root)
        errors = check_sources(root)
        self.assertTrue(any(error.startswith(f"[{rule}]") for error in errors), errors)

    @staticmethod
    def write(root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_rejects_unpinned_dependency_including_target_and_build_sections(self):
        for table in ("dependencies", "build-dependencies", "target.'cfg(windows)'.dependencies"):
            with self.subTest(table=table):
                self.reject("pins", lambda root: self.write(root, TAURI / "Cargo.toml",
                    (root / TAURI / "Cargo.toml").read_text() + f'\n[{table}.bad-crate]\nversion = "1.2.3"\n'))
        self.reject("pins", lambda root: self.write(root, TAURI / "Cargo.toml",
            (root / TAURI / "Cargo.toml").read_text() + '\n[dependencies.bad-crate]\nversion = "=1.2.3"\ngit = "https://example.com/crate"\n'))

    def test_rejects_missing_lock(self):
        self.reject("lock", lambda root: (root / TAURI / "Cargo.lock").unlink())

    def test_rejects_plugin_shell_even_renamed(self):
        self.reject("shell", lambda root: self.write(root, TAURI / "Cargo.toml",
            (root / TAURI / "Cargo.toml").read_text() + '\n[dependencies.run]\npackage = "tauri-plugin-shell"\nversion = "=2.0.0"\n'))

    def test_rejects_unused_window_permission(self):
        def mutate(root):
            path = TAURI / "capabilities/popover.json"
            cap = json.loads((root / path).read_text())
            cap["permissions"] = ["core:window:allow-close"]
            self.write(root, path, json.dumps(cap))
        self.reject("capability", mutate)

    def test_rejects_remote_or_disabled_csp(self):
        for csp in (None, "default-src 'self'; connect-src https:",
                    "default-src 'self'; connect-src ipc:; script-src https://example.com",
                    {"default-src": ["'self'"], "connect-src": ["ipc:", "ws:"]}):
            with self.subTest(csp=csp):
                def mutate(root):
                    path = TAURI / "tauri.conf.json"
                    config = json.loads((root / path).read_text())
                    config["app"]["security"]["csp"] = csp
                    self.write(root, path, json.dumps(config))
                self.reject("csp", mutate)

    def test_rejects_insecure_release_overlay(self):
        self.reject("transport", lambda root: self.write(root, Path("build/release.conf.json"),
            '{"plugins":{"updater":{"dangerousInsecureTransportProtocol":true}}}'))

    def test_rejects_motion_literals_in_css_and_js(self):
        for name, text in (("bad.css", ".row { transition: opacity 100ms; }"),
                           ("bad.css", ".row { transition-timing-function: cubic-bezier(0.2, 0.8, 0.2, 1); }"),
                           ("bad.css", ".row { transition-timing-function: ease-out; }"),
                           ("bad.js", "row.animate([], {duration: 240});"),
                           ("bad.js", 'row.animate([], {easing: "ease-out"});')):
            with self.subTest(text=text):
                self.reject("motion", lambda root: self.write(root, UI / name, text))

    def test_motion_file_is_optional_and_is_the_only_literal_owner(self):
        root = self.temp_tree()
        self.write(root, UI / "motion.css", ":root { --travel: 240ms; --ease-out: cubic-bezier(0.2, 0.8, 0.2, 1); }")
        self.write(root, UI / "valid.css", ".row { transition: opacity var(--travel) var(--ease-out); }")
        self.write(root, UI / "valid.js", "const curve = styles.getPropertyValue('--ease-out');")
        self.assertEqual(check_sources(root), [])

    def test_rejects_every_safari_denylist_entry(self):
        examples = [".row { transition-behavior: allow-discrete; }", "@starting-style { .row { opacity: 0; } }",
                    ".row { text-wrap: balance; }", ".row { view-transition-name: row; }",
                    ".row { anchor-name: --row; }", ".row { field-sizing: content; }",
                    ".row { interpolate-size: allow-keywords; }", ".row { width: calc-size(auto, size); }",
                    ".row { color: light-dark(black, white); }", "@scope (.row) { .label { color: red; } }",
                    ".row { & .label { color: red; } }", ".row { .label { color: red; } }"]
        for text in examples:
            with self.subTest(text=text):
                self.reject("safari", lambda root: self.write(root, UI / "bad.css", text))
        for text in ("document.startViewTransition(update);", 'row.style.fieldSizing = "content";'):
            with self.subTest(text=text):
                self.reject("safari", lambda root: self.write(root, UI / "bad.js", text))

    def test_allows_safari_17_features_and_flat_group_rules(self):
        root = self.temp_tree()
        self.write(root, UI / "valid.css", '''
            /* transition-behavior is banned in actual code, not in comments. */
            @property --progress { syntax: "<number>"; initial-value: 0; inherits: false; }
            @media (prefers-reduced-motion: reduce) { .row:has(button) { color: color-mix(in srgb, red, blue); } }
            @container (width > 10px) { .row { opacity: 1; } }
            @keyframes turn { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        ''')
        self.assertEqual(check_sources(root), [])

    def test_rejects_set_title(self):
        self.reject("title", lambda root: self.write(root, TAURI / "src/bad.rs", 'fn bad() { tray.set_title(Some("86%")); }'))

    def test_rejects_sf_symbol_assets_and_exports(self):
        self.reject("symbols", lambda root: self.write(root, UI / "icons/route.symbolset/Contents.json", "{}"))
        self.reject("symbols", lambda root: self.write(root, UI / "icons/route.svg", '<svg><title>SF Symbols export</title></svg>'))
        self.reject("symbols", lambda root: self.write(root, UI / "icons/route.svg", '<svg><g id="Symbols"></g></svg>'))

    def test_rejects_missing_fixture_and_uncovered_contract_or_model(self):
        self.reject("fixtures", lambda root: (root / "tests/fixtures/app/usage.json").unlink())
        for addition in ("pub struct NewContract { pub value: String }",
                         "pub enum NewContract { Value(String) }"):
            with self.subTest(addition=addition):
                self.reject("fixtures", lambda root: self.write(root, TAURI / "src/models.rs",
                    (root / TAURI / "src/models.rs").read_text() + "\n" + addition))


if __name__ == "__main__":
    unittest.main()
