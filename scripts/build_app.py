"""Release build implementation; use build_app.sh so the tool gate runs first.

Only the standard library and the already-prepared repo-local Tauri CLI are
needed. Python build tools and every cache/download are private to build/.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import unquote, urlparse

from app_updater import latest, validate_release

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
TAURI = ROOT / "macos/src-tauri"
PLUGIN_ITEMS = ("agents", "skills", "commands", "hooks", ".claude-plugin",
                "plugin.json", "mcp_config.json", "LICENSE", "NOTICE")
MACHO_MAGIC = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca",
    "cafebabf", "bfbafeca")}


def run(*args, cwd=None, capture=False, env=None):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                          check=True, text=True, capture_output=capture).stdout


def versions():
    """Never manufacture a release version independently of package metadata."""
    sources = {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        ".claude-plugin/plugin.json": json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"],
        "src/agents_router/__init__.py": re.search(
            r'__version__\s*=\s*[\'"]([^\'"]+)',
            (ROOT / "src/agents_router/__init__.py").read_text())[1],
        "Cargo.toml": tomllib.loads((TAURI / "Cargo.toml").read_text())["package"]["version"],
        "tauri.conf.json": json.loads((TAURI / "tauri.conf.json").read_text())["version"],
    }
    if len(set(sources.values())) != 1:
        raise ValueError(f"Release versions disagree: {sources}")
    version = sources["pyproject.toml"]
    locked = tomllib.loads((TAURI / "Cargo.lock").read_text())["package"]
    if next(p["version"] for p in locked if p["name"] == "agents-router-app") != version:
        raise ValueError("Cargo.lock app version disagrees; update the app entry before building")
    print(f"Release version: {version}", flush=True)
    return version


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def download(url, sha256):
    path = BUILD / "downloads" / Path(unquote(urlparse(url).path)).name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            run("curl", "--fail", "--location", "--silent", "--show-error", "--retry", "3",
                "--connect-timeout", "30", "--max-time", "600", url, "--output", partial)
            if digest(partial) != sha256:
                raise ValueError(f"SHA256 mismatch: {path.name}")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if digest(path) != sha256:
        raise ValueError(f"SHA256 mismatch in cached download: {path}")
    print(f"SHA256 verified: {path.name} {sha256}", flush=True)
    return path


def requirements(group, dependencies):
    path = BUILD / f"{group}-requirements.txt"
    path.write_text("".join(f"{d['name']}=={d['version']} --hash=sha256:{d['sha256']}\n"
                            for d in dependencies))
    return path


def extract_python(archive, destination):
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    with tarfile.open(archive) as file:
        file.extractall(destination, filter="data")
    return destination / "python"


def macho_files(root):
    for path in sorted(root.rglob("*"), key=lambda p: (-len(p.parts), str(p))):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as file:
                if file.read(4) in MACHO_MAGIC:
                    yield path


def sign_runtime(runtime):
    files = list(macho_files(runtime))
    # Files first, then their enclosing bundles from deepest to outermost.
    for path in files:
        run("codesign", "--force", "--sign", "-", "--timestamp=none", path, capture=True)
    bundles = sorted((p for p in runtime.rglob("*") if p.is_dir() and not p.is_symlink()
                      and p.suffix in (".app", ".framework", ".bundle", ".xpc")),
                     key=lambda p: (-len(p.parts), str(p)))
    for path in bundles:
        run("codesign", "--force", "--sign", "-", "--timestamp=none", path, capture=True)
    print(f"Pre-signed {len(files)} Mach-O files and {len(bundles)} nested bundles inside-out.", flush=True)


def stage(version, lock):
    archive = download(lock["python"]["url"], lock["python"]["sha256"])
    toolchain = extract_python(archive, BUILD / "python-toolchain")
    python = toolchain / "bin/python3"
    actual = run(python, "-I", "-c", "import platform; print(platform.python_version())", capture=True).strip()
    if actual != lock["python"]["version"]:
        raise ValueError(f"Locked Python {lock['python']['version']} differs from archive {actual}")
    runtime_req = requirements("runtime", lock["runtime"])
    build_req = requirements("build", lock["build"])
    wheels = BUILD / "downloads/wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    run(python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "download",
        "--index-url", "https://pypi.org/simple",
        "--cache-dir", BUILD / "pip-cache", "--require-hashes", "--only-binary=:all:",
        "--dest", wheels, "-r", runtime_req, "-r", build_req)
    builder = BUILD / "wheel-builder"
    shutil.rmtree(builder, ignore_errors=True)
    run(python, "-I", "-m", "venv", builder)
    builder_python = builder / "bin/python3"
    run(builder_python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
        "--no-index", "--find-links", wheels,
        "--cache-dir", BUILD / "pip-cache", "--require-hashes", "-r", build_req)
    # Build from a clean source copy, excluding repo state and old egg-info.
    source = BUILD / "wheel-source"
    shutil.rmtree(source, ignore_errors=True)
    source.mkdir()
    for item in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / item, source / item)
    shutil.copytree(ROOT / "src", source / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    output = BUILD / "agt-wheel"
    shutil.rmtree(output, ignore_errors=True)
    run(builder_python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "wheel",
        "--no-deps", "--no-build-isolation",
        "--cache-dir", BUILD / "pip-cache", "--wheel-dir", output, source)
    wheel, = output.glob("*.whl")
    agt_req = BUILD / "agt-requirements.txt"
    agt_req.write_text(f"agents-router=={version} --hash=sha256:{digest(wheel)}\n")
    staging = BUILD / "app-stage"
    runtime = extract_python(archive, staging)
    runtime.rename(staging / "runtime")
    runtime = staging / "runtime"
    run(runtime / "bin/python3", "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
        "install", "--no-index",
        "--find-links", wheels, "--find-links", output, "--cache-dir", BUILD / "pip-cache",
        "--require-hashes", "--no-compile", "-r", runtime_req, "-r", agt_req)
    # Shipping omits build tools, tests, headers and static libraries.
    # Keep the complete executable stdlib and its shared/native dependencies.
    lib = runtime / "lib" / f"python{actual.rsplit('.', 1)[0]}"
    for path in [runtime / "include", runtime / "share", runtime / "lib/pkgconfig",
                 lib / "test", lib / "ensurepip", *lib.glob("config-*"),
                 *lib.glob("site-packages/pip*"), *runtime.rglob("__pycache__")]:
        if path.is_dir():
            shutil.rmtree(path)
    for path in runtime.rglob("*.a"):
        path.unlink()
    for path in (runtime / "bin").iterdir():
        if path.name != "python3.13":
            path.unlink()
    # Tauri's resource copier dereferences aliases. Ship the one interpreter
    # path the installer/launcher uses instead of three full binary copies.
    (runtime / "bin/python3.13").rename(runtime / "bin/python3")
    plugin = runtime / "plugin"
    plugin.mkdir()
    for item in PLUGIN_ITEMS:
        source = ROOT / item
        if source.is_dir():
            shutil.copytree(source, plugin / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, plugin / item)
    (runtime / "agt-bundle.json").write_text(json.dumps({
        "schema_version": 1, "source": "app", "app_version": version,
        "runtime_version": version, "python_version": actual, "plugin_path": "plugin",
    }, indent=2) + "\n")
    # Bare `python3 -I -m ...` otherwise adds unsealed __pycache__ resources and
    # invalidates the outer app signature. Compile portable, checked-hash caches
    # before signing, so both installed and in-bundle execution leave it intact.
    run(runtime / "bin/python3", "-I", "-B", "-m", "compileall", "-q",
        "--invalidation-mode", "checked-hash", "-s", runtime, runtime / "lib/python3.13")
    sign_runtime(runtime)
    return runtime


def minimum_versions(build):
    """Read deployment targets, excluding vtool's linker/tool and SDK versions."""
    versions = []
    for command in build.split("Load command ")[1:]:
        field = "minos" if re.search(r"cmd LC_BUILD_VERSION\b", command) else (
            "version" if re.search(r"cmd LC_VERSION_MIN_MACOSX\b", command) else None)
        if field:
            match = re.search(rf"^\s*{field}\s+(\d+(?:\.\d+)+)\s*$", command, re.M)
            if match:
                versions.append(match[1])
    return versions


def verify_app(app, version, lock):
    run("codesign", "--verify", "--strict", "--verbose=2", app, capture=True)
    entries = []
    for path in macho_files(app):
        run("codesign", "--verify", "--strict", "--verbose=2", path, capture=True)
        arch = run("lipo", "-archs", path, capture=True).strip()
        if arch != "arm64":
            raise ValueError(f"Expected arm64 only, found {arch}: {path}")
        build = run("vtool", "-show-build", path, capture=True)
        minimum = minimum_versions(build)
        if not minimum or any(tuple(map(int, v.split(".")[:2])) > (14, 0) for v in minimum):
            raise ValueError(f"Missing or unsupported minimum OS: {path}: {build}")
        entries.append({"path": str(path.relative_to(app)), "arch": arch, "minimum_os": minimum})
    if not entries:
        raise ValueError("No Mach-O files in app")
    with (app / "Contents/Info.plist").open("rb") as file:
        info = plistlib.load(file)
    if info["CFBundleShortVersionString"] != version or info["LSMinimumSystemVersion"] != "14.0":
        raise ValueError("App plist version/deployment target differs")
    runtime = app / "Contents/Resources/runtime"
    manifest = json.loads((runtime / "agt-bundle.json").read_text())
    if (manifest["app_version"], manifest["runtime_version"], manifest["python_version"]) != (
            version, version, lock["python"]["version"]):
        raise ValueError("Runtime manifest differs from release metadata")
    plugin = runtime / "plugin"
    if json.loads((plugin / ".claude-plugin/plugin.json").read_text())["version"] != version:
        raise ValueError("Bundled plugin version differs")
    for item in PLUGIN_ITEMS:
        source = ROOT / item
        paths = source.rglob("*") if source.is_dir() else [source]
        for path in paths:
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                if digest(path) != digest(plugin / path.relative_to(ROOT)):
                    raise ValueError(f"Plugin payload differs: {path}")
    # Run in a private home, never loading or updating the person's setup.
    with tempfile.TemporaryDirectory(prefix="runtime-check-", dir=BUILD) as scratch:
        env = {**os.environ, "HOME": scratch, "AGT_HOME": str(Path(scratch) / "agt"),
               "AGT_CONFIG_DIR": str(Path(scratch) / "config"), "PYTHONDONTWRITEBYTECODE": "1"}
        python = runtime / "bin/python3"
        result = run(python, "-I", "-m", "agents_router", "--version", capture=True, env=env).strip()
        if result != f"agents-router {version}":
            raise ValueError(f"Bundled version check failed: {result}")
        run(python, "-I", "-c",
            "import _ssl, _hashlib, _ctypes, _sqlite3, _bz2, _lzma, _decimal, zlib, readline; "
            "import rich, rich_argparse, markdown_it, mdurl, pygments; "
            "from agents_router import bundle; assert bundle.detect() is not None; "
            "print('native modules, locked dependencies and app ownership: OK')", capture=True, env=env)
    run("codesign", "--verify", "--strict", "--verbose=2", app, capture=True)
    print(f"Verified {app}: strict signatures, {len(entries)} arm64 Mach-O files, minimum OS <= 14.0, "
          f"plugin payload, Python/native imports and agt {version}.", flush=True)
    return entries


def tree_size(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def main():
    if len(sys.argv) != 1:
        raise ValueError("Usage: scripts/build_app.sh (sign updater artifacts with TAURI_SIGNING_PRIVATE_KEY)")
    version = versions()  # Read-only, before staging or downloading anything.
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("App releases require an Apple Silicon Mac and Command Line Tools")
    lock = tomllib.loads((ROOT / "scripts/python-runtime.lock").read_text())
    updater = bool(os.environ.get("TAURI_SIGNING_PRIVATE_KEY"))
    # A signing key means release artifacts. Scratch keys/configs belong only in
    # check_app_update.py, which never writes dist/ or a release overlay.
    base_config = json.loads((TAURI / "tauri.conf.json").read_text())
    if os.environ.get("TAURI_SIGNING_PUBLIC_KEY"):
        raise ValueError("Public-key overrides cannot publish. Use scripts/check_app_update.py for throwaway keys")
    if updater:
        validate_release(base_config)  # Before staging; the placeholder can never publish.
    BUILD.mkdir(exist_ok=True)
    (BUILD / "tmp").mkdir(exist_ok=True)
    os.environ.update(CARGO_HOME=str(BUILD / "cargo-home"), CARGO_TARGET_DIR=str(BUILD / "app-target"),
                      MACOSX_DEPLOYMENT_TARGET="14.0", CI="true", RUSTUP_AUTO_INSTALL="0",
                      PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CACHE_DIR=str(BUILD / "pip-cache"),
                      SOURCE_DATE_EPOCH="1704067200", TMPDIR=str(BUILD / "tmp"))
    runtime = stage(version, lock)
    overlay = BUILD / "release.conf.json"
    config = {"bundle": {"resources": {str(runtime): "runtime/"}, "createUpdaterArtifacts": updater}}
    overlay.write_text(json.dumps(config, indent=2) + "\n")
    env = {**os.environ, "PATH": str(BUILD / "tools/bin") + os.pathsep + os.environ["PATH"]}
    run("cargo", "tauri", "build", "--target", "aarch64-apple-darwin", "--bundles", "app,dmg",
        "--config", overlay, "--", "--locked", cwd=TAURI, env=env)
    bundles = BUILD / "app-target/aarch64-apple-darwin/release/bundle"
    app = bundles / "macos/Agents Router.app"
    entries = verify_app(app, version, lock)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    if not updater:
        for name in ("Agents Router.app.tar.gz", "Agents Router.app.tar.gz.sig", "latest.json"):
            (dist / name).unlink(missing_ok=True)  # Never leave older signed artifacts beside a keyless build.
    dmg = dist / f"Agents-Router-{version}-arm64.dmg"
    shutil.copy2(bundles / f"dmg/Agents Router_{version}_aarch64.dmg", dmg)
    # Isolate the mount, detach even when content verification fails.
    with tempfile.TemporaryDirectory(prefix="dmg-check-", dir=BUILD) as mount:
        run("hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, dmg, capture=True)
        try:
            if not (Path(mount) / "Agents Router.app").is_dir() or os.readlink(Path(mount) / "Applications") != "/Applications":
                raise ValueError("DMG must contain Agents Router.app and the /Applications link")
            print("hdiutil attach: Agents Router.app and Applications -> /Applications verified.", flush=True)
        finally:
            run("hdiutil", "detach", mount, capture=True)
    if updater:
        archive = bundles / "macos/Agents Router.app.tar.gz"
        for path in (archive, archive.with_suffix(archive.suffix + ".sig")):
            shutil.copy2(path, dist / path.name)
        with tempfile.TemporaryDirectory(prefix="updater-check-", dir=BUILD) as scratch:
            with tarfile.open(archive) as file:
                file.extractall(scratch, filter="data")
            verify_app(Path(scratch) / "Agents Router.app", version, lock)
        metadata = latest(version, archive.name,
                          archive.with_suffix(archive.suffix + ".sig").read_text(),
                          os.environ.get("AGT_RELEASE_NOTES", ""))
        (dist / "latest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    sizes = {"app": tree_size(app), "runtime": tree_size(app / "Contents/Resources/runtime"),
             "dmg": dmg.stat().st_size}
    sizes["shell"] = sizes["app"] - sizes["runtime"]
    report = {"version": version, "python": lock["python"], "updater_verified": updater,
              "sizes_bytes": sizes, "mach_o": entries}
    (BUILD / "app-build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Sizes (MiB): " + ", ".join(f"{k}={v / 1048576:.2f}" for k, v in sizes.items()), flush=True)
    for key, budget in (("app", 140), ("runtime", 120), ("shell", 20), ("dmg", 50)):
        if sizes[key] > budget * 1048576:
            raise ValueError(f"{key} exceeds the agreed {budget} MiB budget")
    print(f"Built and verified: {dmg}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"App build failed: {error}")
