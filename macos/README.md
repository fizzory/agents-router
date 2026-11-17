# macOS app development

The production executable is the stable launcher at
`~/Library/Application Support/Agents Router/bin/agt`. The app never searches
PATH. Runtime installation is described below.

## Runtime manifest and CLI ownership (step 7)

The installer in step 8 must write `agt-bundle.json` at the root of each staged
runtime before validation and publication. The published location is
`$AGT_APP_SUPPORT/runtimes/<version>/agt-bundle.json`:

```json
{
  "schema_version": 1,
  "source": "app",
  "app_version": "0.17.0",
  "runtime_version": "0.17.0",
  "python_version": "3.13.18",
  "plugin_path": "plugin"
}
```

Versions are nonempty version strings (digits, letters, dots, plus and dash,
starting with a digit). `plugin_path` is relative to the manifest, stays inside
the runtime, and contains the version-matched shipped plugin tree, including
`.claude-plugin/marketplace.json`. Python is `bin/python3`; the installed agt
package and Python must both resolve inside that same runtime. A folder named
like an app runtime, or an app installed beside a uv/pipx CLI, never claims that
other CLI. Malformed manifests do not establish ownership.

`AGT_APP_SUPPORT` overrides `~/Library/Application Support/Agents Router`;
`AGT_BIN_DIR` overrides `~/.local/bin`. Both apply to scratch installs and checks.
A test build has its own defaults and flags ([Test build folders](#test-build-folders)).
The stable launcher is always `$AGT_APP_SUPPORT/bin/agt`; it must resolve
`current` to its physical runtime once, then execute that runtime's
`bin/python3 -I -m agents_router` and preserve arguments. Resolving before exec
keeps the running interpreter and imported modules on one version if `current`
switches mid-command. The public
`$AGT_BIN_DIR/agt` symlink points at it. The Rust app uses the same support override.
The launcher preserves the selected support and bin overrides in its environment
so hooks and MCP servers started by a host CLI use the same install paths.

App-owned setup/update derives `$AGT_APP_SUPPORT/plugins/<runtime_version>` from
the immutable runtime's plugin tree. Only that derived copy rewrites hook shell
commands and MCP executable fields to the absolute stable launcher. Host plugin
commands replace the old marketplace with this local source before installing;
there is no package reinstall or Git fetch. A damaged payload fails locally.
The settings-only Claude route replaces its marketplace source too. Dry runs
validate the payload and print the local steps without creating the copy.

`agt-run`, shared hooks and plugin hooks follow the stable launcher. Runners,
private worker hooks and the parent MCP server resolve physical runtime paths
and use isolated Python. Both `spec.json` and `status.json` record
`install_runtime`, the absolute runtime directory; step 8's cleanup must retain
any directory referenced by a running job, including a surviving worker.

`agt doctor` includes install ownership, app/agt/Python versions and PATH conflict
warnings. `--verbose` adds physical paths, manifest Python/runtime versions and
every executable PATH entry. `--json` includes these in `install`. No other agt
or shell is executed to diagnose it. `agt usage --json` includes the compact
`install: {source: "app"|"uv"|"pipx"|"source", version: "…"}` contract;
Settings reads that field for the uninstall row. Pip, editable and source
checkouts report `source`; only an app manifest grants app uninstall ownership.

All Cargo invocations use `export CARGO_HOME=$PWD/build/cargo-home` from the
repository root and `--locked`. Tools are checked by
`scripts/check_app_tools.sh`; this task installs no tools.

## Web UI build

`macos/ui/` is the web UI's source: the pages `popover.html`, `settings.html`
and `index.html`, their scripts and stylesheets, and `logic/` with its
`node --test` suite. Both windows are React: the popover (`src/popover/`) and
the Settings window (`src/settings/`), built from the shared components in
`src/shared/`. Vite bundles the three pages into the ignored
`build/ui-dist/`, and `tauri.conf.json` embeds that folder (`frontendDist`),
never `macos/ui/` itself. New code is strict TypeScript under `macos/ui/src/`
(`popover/`, `settings/`, `shared/`); `allowJs` lets it import `logic/*.js`
unchanged.

| What | Where |
| --- | --- |
| Node | `24.22.2`, pinned with its SHA256 in `scripts/node-runtime.lock`, unpacked in `build/node/` |
| npm packages | Exact versions in `macos/ui/package.json`, integrity hashes in `macos/ui/package-lock.json` |
| `node_modules` | `npm ci --ignore-scripts` runs in `build/ui-npm/`; `macos/ui/node_modules` is an ignored link to its `node_modules` |
| npm cache and config | `build/npm-cache/`; empty `build/npmrc` files keep `~/.npmrc` out |
| Output | `build/ui-dist/`: the three pages at its root, hashed files in `assets/` |
| Component tests | Vitest, Testing Library and jsdom: `src/**/*.test.tsx`, configured in `vitest.config.ts` |

Each page links one stylesheet, `src/<page>/index.css`, which imports
`tokens.css`, `motion.css` and the page's own sheet in the order the page
used. Vite inlines the imports, so each page keeps its cascade order. CSS is
not minified: the pages read motion tokens such as `--tooltip-delay: 400ms`
with `parseFloat` as milliseconds. The build targets Safari 17. It inlines no
assets as `data:` URIs and uses no preload polyfill; the CSP allows only
`'self'` and is unchanged. `settings-inventory.json` and
`window-permissions.json` stay out of the build; only tests and guards read
them.

```bash
scripts/prepare_app_tools.sh --yes        # once, with consent: Node, npm ci, tauri-cli
python3 scripts/build_app_ui.py           # vite build into build/ui-dist, then check it
python3 scripts/build_app_ui.py --typecheck   # tsc --noEmit
python3 scripts/build_app_ui.py --test        # vitest run: React component tests in jsdom
```

The component tests use a fake popover adapter with the native event
semantics, the committed fixtures, and the real `motion.css` token values;
they never load `window.__TAURI__`.

`build_app_ui.py` never installs. Without the pinned Node or the packages it
stops and names `scripts/prepare_app_tools.sh --yes`. After every build it
checks the output: the three pages exist, and the HTML has no inline
`<script>`, `<style>`, `style=` or `on…=` attribute. Every `src`, `href` and
CSS `url()` must name a file inside the build. `scripts/check_app_tools.sh`
also requires the Node and the packages.

`cargo tauri build` runs the UI build first as `beforeBuildCommand`, so
`build_app.sh`, the self-test and a debug build need no extra step. Raw
`cargo test` and `cargo run` don't run it, and the crate won't compile
without `build/ui-dist`. Build the UI first:

```bash
python3 scripts/build_app_ui.py
cd macos/src-tauri && CARGO_HOME="$PWD/../../build/cargo-home" cargo test --locked
node --test macos/ui/logic                # from the repository root
```

Dev loop, in two terminals. The CSP is unchanged, so use no dev server:

```bash
python3 scripts/build_app_ui.py --watch   # rebuilds build/ui-dist on every change
cd macos/src-tauri && CARGO_HOME="$PWD/../../build/cargo-home" ../../build/tools/bin/cargo-tauri dev \
    --no-dev-server --additional-watch-folders ../../build/ui-dist
```

Tauri rebuilds and restarts the app when `build/ui-dist` changes.

## Test and release builds

Only the released app is called `Agents Router.app`. Every other build is a
test build with its own name and identifier, so Spotlight, Finder and
LaunchServices tell them apart.

| Build | Bundle | Identifier | Made by |
| --- | --- | --- | --- |
| Release | `Agents Router.app` | `io.github.watcharagorn.agents-router` | `scripts/build_app.sh` only, inside `dist/Agents-Router-<version>-arm64.dmg` |
| Test | `Agents Router Test.app` | `io.github.watcharagorn.agents-router.test` | Every raw `cargo tauri dev` or `cargo tauri build` (debug or release), the self-test and the check scripts |

- `macos/src-tauri/tauri.conf.json` holds the test name and identifier.
  `build_app.py` writes `build/release.conf.json`, the only config that sets
  the release ones, also for keyless local builds. `tests/test_app_sources.py`
  rejects the release name or identifier in any other config.
- After packaging, `build_app.py` removes its intermediate
  `build/app-target/aarch64-apple-darwin/release/bundle/macos/Agents Router.app`,
  on success and on failure. Compiled binaries, caches,
  `build/app-build-report.json` and `dist/` stay.
- A test build checks no update feed. A manual check says test builds don't
  update and offers the Releases page. `scripts/check_app_update.py` uses
  `io.github.watcharagorn.agents-router.test.update-<pid>` and its own localhost feed.
- Move to Applications: the release lands as `Agents Router.app`, as before. A
  test build keeps its own bundle name, `Agents Router Test.app`, lands beside
  the release and never replaces it.
- The login item's LaunchAgent label is `<identifier>.app`. The release keeps
  `io.github.watcharagorn.agents-router.app`.
- The builds share no folders. The release keeps `~/Library/Application Support/Agents Router`,
  `~/.local/bin/agt`, `~/.agents-router`, `~/.agt` and its log folder
  `~/Library/Logs/io.github.watcharagorn.agents-router`; a test build gets its own, below,
  and logs to `<app support>/logs/<identifier>` ([Diagnostic log](#diagnostic-log)).

### Test build folders

`app_paths.rs` resolves every folder once at launch. The embedded bundle
identifier decides: `io.github.watcharagorn.agents-router` is the release, every
other identifier (`.test`, `.test.update-<pid>`, optimized or debug) is a test
build. Nothing depends on `debug_assertions`.

| Folder | Flag | Variable | Test default |
| --- | --- | --- | --- |
| App support | `--test-app-support` | `AGT_APP_SUPPORT` | `~/Library/Application Support/Agents Router Test` |
| Bin folder | `--test-bin-dir` | `AGT_BIN_DIR` | `<app support>/cli-bin` |
| agt home | `--test-agt-home` | `AGT_HOME` | `<app support>/agt-home` |
| Settings folder | `--test-config-dir` | `AGT_CONFIG_DIR` | the effective agt home |

- Precedence per folder: flag, then variable, then default. Flags take
  `--flag <folder>` or `--flag=<folder>`; a missing value, a repeat or an
  unknown `--test-*` flag stops the launch.
- Launch with `open -n "<path>/Agents Router Test.app" --args --test-app-support /abs/path`,
  `open -n --env AGT_APP_SUPPORT=/abs/path "<path>/Agents Router Test.app"`, or by running
  `Contents/MacOS/agents-router` directly. Finder and the Dock give the defaults.
- Validation: absolute, non-empty, no `.`/`..`, no control characters. Each
  folder's physical path (symlinks resolved, case-insensitive) must not be,
  contain or sit inside `~/.local/bin`, `~/Library/Application Support/Agents Router`,
  `~/.agents-router`, `~/.agt` or `~/Library/Logs/io.github.watcharagorn.agents-router`, where `~` is both the launch HOME and the
  account's home from the user database (`getpwuid_r`), which no `HOME` or
  `CFFIXED_USER_HOME` changes. A failed lookup stops the launch. A dangling
  symlink is refused, and so is a bin folder equal to `<app support>/bin`, where
  the stable launcher lives. A refusal prints `agt-app … refused: …` and exits 2.
- Nothing a test build makes below its folders may be a symlink: `home`, `bin`,
  `bin/agt`, `handoffs`, `runtimes` and each runtime, `updated`, `plugins`,
  `install.json` and `install.lock` under app support; `state`,
  `state/admission.lock` and `jobs` under the agt home; `settings.toml` in the
  settings folder. `current` must be the installer's `runtimes/<version>` link.
  `previous-cli` may be a link: it keeps a replaced foreign CLI. Launch refuses
  them, and the installer checks again before each write. Its lock files open
  with `O_NOFOLLOW`; the release opens `install.lock` and
  `state/admission.lock` as before.
- The stable launcher stays `<app support>/bin/agt`. A test build's launcher also
  exports `AGT_HOME`, `AGT_CONFIG_DIR`, `HOME=<app support>/home`,
  `CFFIXED_USER_HOME` and `AGT_EXCLUDE_HOMES`, and unsets `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and
  `GEMINI_CLI_HOME`. Terminal, hooks and MCP servers inherit them. `AgtClient`
  and installer children get the same environment, with the private home as
  their working folder. The app keeps the real HOME for Applications
  destinations and the login item. A launcher written for other folders is
  rewritten at launch.
- With the private HOME, agt would no longer know the real HOME isn't a
  workspace, and would read `~/.agt/settings.toml` as workspace settings.
  `AGT_EXCLUDE_HOMES` lists the private HOME, the launch HOME and the account's
  home; agt treats each like its own HOME: never a workspace, and outside a
  checkout the search stops there. That holds with `~/.git` too. Every command
  keeps its folder, so a Settings workspace anywhere works as in the release.
  Terminal handoffs, and their Copy command, start in the private HOME.
- Relaunch: Move to Applications (`open -n <app> --args …`), the post-update
  restart and the login item's LaunchAgent carry every non-default folder as a
  flag, including those that came from variables. An enabled test login item is
  rewritten at each launch.
- The single-instance callback ignores a second launch's arguments: quit the
  running test build before changing its folders.
- Startup logs `test build paths app_support=… (source) … home=… account_home=…` once.
- The tray tooltip, which is also the VoiceOver label, ends with `Test build`.
- `test_build_paths` (Settings capability) returns
  `[{key, path, display, source, origin}]`, or `null` in the release. `key` is
  `app_support`, `bin_dir`, `agt_home` or `config_dir`; `source` is `flag`,
  `env` or `default`; `origin` names the flag or variable; `display` shows HOME
  as `~`. Settings → General's hidden `Test build` disclosure lists them with a
  copy button; search opens it.
- `cargo run --locked --bin agt-app-check -- --resolve-paths <identifier> [flags]`
  prints the resolution for the current environment without writing.

The release reads `AGT_APP_SUPPORT`, `AGT_BIN_DIR`, `AGT_HOME` and
`AGT_CONFIG_DIR` as before, with no overlap checks, and refuses any `--test-*`
argument before touching the filesystem. A dangling symlink among its folders
doesn't stop the launch, as before: the install reports it. The self-test and check scripts set
`AGT_HOME` to `$HOME/agt-home`, because `$HOME/.agents-router` is a production
folder. They swap HOME; the production check still covers the account's own
folders through the user database. Each script builds the app's environment
with `scripts/app_env.py`'s `scratch_env`, which drops every inherited `AGT_*`
variable and sets all four folders to scratch ones.

Test bundles live in these folders:

| Folder | Made by |
| --- | --- |
| `macos/src-tauri/target/debug/bundle/macos/`, `macos/src-tauri/target/release/bundle/macos/` | Raw `cargo tauri build` (`--debug`); `scripts/check_app_native.py` runs the debug one |
| `build/app-target/aarch64-apple-darwin/release/bundle/macos/` | `scripts/run_app_selftest.py` (`ci-selftest`) |
| `build/update-target/debug/bundle/macos/` | `scripts/check_app_update.py` |

Find and clean them:

```sh
mdfind 'kMDItemFSName == "Agents Router Test.app"'   # every test bundle
mdfind 'kMDItemFSName == "Agents Router.app"'        # release-named copies
```

A test build also leaves a login item and data. `<id>` is its bundle
identifier: `io.github.watcharagorn.agents-router.test`, or
`io.github.watcharagorn.agents-router.test.update-<pid>` from
`scripts/check_app_update.py`.

| What | Where |
| --- | --- |
| Login item | `~/Library/LaunchAgents/<id>.app.plist`, launchd label `<id>.app`. `Open at login` writes it. Keep the release's `io.github.watcharagorn.agents-router.app.plist` and agt share's `io.github.watcharagorn.agents-router.share.plist` |
| Data folder | `~/Library/Application Support/Agents Router Test`. By default it also holds the bin folder (`cli-bin`), the agt home and settings (`agt-home`), the private HOME (`home`) and the diagnostic log (`logs/<id>/agents-router.log`) |
| Overridden folders | Each folder given with a `--test-*` flag or `AGT_*` variable ([Test build folders](#test-build-folders)). Settings → General → `Test build` lists all four |
| WebKit, caches, preferences | `~/Library/WebKit/<id>`, `~/Library/Caches/<id>`, `~/Library/Preferences/<id>.plist` |

The scripts swap HOME, so their data folders and login items stay in their
scratch folders. Their WebKit, cache and preference files still land in the
real `~/Library`.

Clean up in this order, so no login item opens a deleted bundle:

1. In the running test build, open Settings → General. Note the folders under
   `Test build`, turn off `Open at login`, click `Uninstall command line tool…`,
   then quit. Uninstall removes `<bin folder>/agt` and puts back any `agt` it
   replaced there.
2. Without the test build, remove its login item by hand. `launchctl bootout`
   fails when the item isn't loaded; go on to `rm`.

   ```sh
   ls ~/Library/LaunchAgents/io.github.watcharagorn.agents-router.test*.app.plist
   label=io.github.watcharagorn.agents-router.test.app
   launchctl bootout "gui/$(id -u)/$label"
   rm -f ~/Library/LaunchAgents/"$label".plist
   ```

3. Delete the data folder. Delete an overridden folder only when the test build
   made it; a folder you already had keeps your own files. A test build refuses
   every production folder, so none of them is the release's.
4. Delete `~/Library/WebKit/<id>` and `~/Library/Caches/<id>`, then run
   `defaults delete <id>`.
5. Move the bundle to the Trash, or `rm -rf` that one bundle path.

Builds from before this rule are still named `Agents Router.app`: delete every
release-named copy under `build/` and `macos/src-tauri/target/`, and keep the
one in `/Applications`. Their update checks used
`io.github.watcharagorn.agents-router.update-test-<pid>`: delete those WebKit
and cache folders and preferences too.

## Release build (step 9)

Run `scripts/build_app.sh` on an Apple Silicon Mac with Command Line Tools.
It checks the prepared tools before creating anything, then requires the Python,
plugin, Cargo and Tauri versions to agree (including Cargo.lock). It downloads
the CPython distribution pinned in `scripts/python-runtime.lock`, checks SHA256,
and builds agt's wheel with pinned build tools. Both build and runtime dependency
installs use hash-locked requirements and universal wheels. Python tooling,
Cargo, pip caches, downloads and staging stay under `build/`.

The runtime contains the executable standard library and shared dependencies,
agt and its dependencies, the complete shipped plugin tree, and `agt-bundle.json`.
Headers, static libraries, Python's test suite and pip are omitted. Portable
checked-hash bytecode is compiled before signing, so the bare
`python3 -I -m agents_router --version` command creates no unsealed resources.
Only `bin/python3` is shipped: Tauri dereferences resource symlinks, so keeping
the `python` and `python3.13` aliases would copy the interpreter three times.
Every Mach-O and nested bundle is signed ad hoc inside-out before Tauri runs.
Only the generated `build/release.conf.json` adds the runtime resource and the
release name and identifier; the base Tauri config stays usable for development
without a staged runtime.

The script writes `dist/Agents-Router-<version>-arm64.dmg`. When
`TAURI_SIGNING_PRIVATE_KEY` is set (a key path or the key value accepted by
Tauri), it also copies `Agents-Router-<version>-arm64.app.tar.gz` and its `.sig`
to `dist/`. The updater feed is
`https://github.com/fizzory/agents-router/releases/latest/download/latest.json`;
its download URLs and Open Releases Page use the same canonical repository.
These release asset names have no spaces; the static feed uses
exactly those names, including the version.
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` supplies the key's password. Real release
keys are created and stored by the maintainer. A signing key means a release
build: it requires the real `plugins.updater.pubkey` in the base config and
rejects `TAURI_SIGNING_PUBLIC_KEY` overrides. It writes `dist/latest.json` with
the version, `AGT_RELEASE_NOTES` (optional), UTC publish date and the signed
darwin-aarch64 archive URL under the matching GitHub release tag.

The build verifies the app and every nested Mach-O with strict codesign,
requires arm64 and minimum OS <= 14.0, compares the shipped plugin bytes, and
runs Python, native-module imports and `agt --version` in a private scratch
home. It mounts the DMG read-only to check the app, its release identity in
`Info.plist` and the `/Applications` link, then detaches it. A signed updater archive is extracted and checked the same
way. Strict signature verification runs again after Python execution.
`build/app-build-report.json` records every binary and apparent file sizes
(symlinks counted once): shell <= 20 MiB, runtime <= 120 MiB, app <= 140 MiB,
DMG <= 50 MiB. A failed check makes the build fail.

## Isolated native smoke check

Step 11's CI check runs locally with the same entry point:

```sh
export CARGO_HOME="$PWD/build/cargo-home"
python3 scripts/run_app_selftest.py
```

It requires an Apple Silicon desktop session and prepared tools. It builds with
`--locked --no-default-features` and the explicit `ci-selftest` feature, using
the production assets, CSP and capabilities. Only that feature compiles the
early page error observer, readiness/layout/fixture checks and named CSS/JS
probes. There are no extra IPC permissions. Both native windows show briefly,
then the app quits. The absolute fake agt, HOME, AGT_HOME and Cocoa home all
point into a fresh private folder under RUNNER_TEMP (the system temp directory
locally). The native deadline is 60 seconds; the external deadline is 90.

Rust writes `report.json` once, alongside native logs and fake-agt call records.
The runner requires both exit 0 and a complete passing report with the OS
version/build, arm64 architecture, WebKit CFBundleVersion, single page loads,
all nine Settings pane layouts and all probes. It reports macOS 14.0 as
unverified: a newer host proves only its own WebKit. Replay this script on a
14.0 machine when one is available. Failure diagnostics survive at the printed
scratch path.

The Settings check clicks each `nav button[data-pane]` in turn. A pane counts
once `body.dataset.pane` and the button's `aria-current="page"` match, and
`#pane-content` holds exactly that pane's config keys and rows. It opens every
`<details>` there before measuring, so file-only rows get a size. Rust splits the
`config show` fixture's keys by their first part:

| Key | Pane | Row selector in `#pane-content` |
|---|---|---|
| `claude.*`, `codex.*`, `antigravity.*`, `kiro.*` | That provider | `[data-setting-key]` |
| `share.*` | Sharing | `.share-item` (and its `[data-setting-key]` rows) |
| Any other | General | `[data-setting-key]` |
| None | Roles, Accounts, Activity | `.role-row`, `.settings-account`, `.activity-row` |

Every fixture key must show up exactly once across the nine panes.

`.github/workflows/release.yml` runs `app-build` on macos-15 arm64 with CLT
selected, Rust 1.95.0 and cached repo-local tools/Cargo. PR builds need no signing
secret. `app-build` tests contracts and isolated CLI operations, builds/mounts
the DMG, installs the CLI from the app copied out of the read-only mounted DMG,
uploads the DMG, then runs the self-test and uploads its report or failure
diagnostics. The job has a 60-minute timeout; the self-test step has 25 minutes.
The self-test builds `Agents Router Test.app`; all release packaging and
artifact uploads still finish before it runs. A strict matching `vX.Y.Z` tag in
`fizzory/agents-router` runs a separate signed build in `draft-release`, using only the
`release` environment's signing secrets, and creates a draft with the DMG,
versioned updater archive, `.sig` and `latest.json`. All external actions are
pinned by commit SHA. Tagging and publishing remain maintainer actions.

`scripts/build_app.sh` always disables default Cargo features. The developer
`agt-app-check` binary requires `app-check` (enabled by default for development),
so release bundles contain only `agents-router`; ordinary
`cargo run --locked --bin agt-app-check` still works. The source guard rejects
`ci-selftest` in release config, build entry points and transitive default
features. The build also rejects inherited TAURI_CONFIG and checks the actual
bundle's binaries for self-test markers. Published builds do not read
AGT_APP_SELFTEST.

Debug builds also accept `AGT_APP_TEST_LAUNCHER`, an absolute path to
`scripts/fake_app_agt.py`. It serves the golden JSON without importing agt or
starting an agent CLI. Use a temporary HOME, AGT_HOME and AGT_CONFIG_DIR. Create
the temporary `state/`, `jobs/` and config directories before launching.
`AGT_APP_TEST_CALLS` optionally records each request outside these watched paths.
`AGT_APP_TEST_UPDATE_VERSION=0.17.1` exercises the waiting-update menu/footer slot.

Run `python3 scripts/check_app_native.py` from the repository root to create
the isolated environment, check the log assertions and clean up automatically.
It passes `--self-test-wake` to the debug executable, which posts the real
`NSWorkspaceDidWakeNotification`, waits for the shared debounce, opens the
panel, resigns its key status, reopens it and dispatches Escape through the
loaded page. It removes the update footer while shown to check content resizing
(animated unless Reduce Motion is on). It quits afterwards. Standard error includes the NSPanel class,
one `usage read reason=debounced refresh=false`, and hide-not-close lines for
`reason=blur` and `reason=Esc`. Settings opens at the `work` account and must
show the Accounts pane with one `.settings-account` row per account in
`accounts-list-locked.json`. FSEvents and a native app run require access to
the macOS window server outside the command sandbox. No quota is used.

## Popover integration contract

`ui/tauri.js` is the only frontend Tauri adapter. The popover alone may invoke:
`get_snapshot`, `refresh_usage`, `hide_popover`, `set_popover_height`, `open_settings`, `login_account`,
`check_account`, `check_for_updates`, and `quit_app`. Account actions take
`runtime` and canonical `account`; Rust verifies membership in its snapshot.
There is no executable, argument array or shell command IPC.

`get_snapshot` returns `{ usage, error, reading, generation, fetched_at,
update_version }`; timestamps are Unix seconds. `usage` is the schema-1 JSON
contract, retained across errors. `usage-updated` carries this envelope only
after a successful, nonsuperseded read, once per generation. Animate meters
only on this event. `popover-status` carries loading/error/update state, with
the last snapshot; it does not indicate a new usage measurement. Subscribe
before fetching the initial snapshot, and reject older generations.

Automatic checks call Rust's `set_pending_update` to update both slots without
an alert. Manual checks open a Settings sheet immediately. `open_settings`
sends a `settings-target` event to Settings with
`{ pane, runtime, account }`; the page and account navigation are step 5/6.
Settings activation is Regular while visible and Accessory after hiding.

The popover groups accounts under provider headings. `logic/rows.js` owns
the pure row/VoiceOver text and footer copy; React keys rows by
`runtime:name`, so refreshes keep their DOM nodes and keyboard focus. The
account list is one Tab stop: ↑/↓ and Home/End move between rows, ←/→ between
a row's Settings target and its inline button (focused first). ⌘R refreshes,
⌘, opens Settings, ⌘Q quits; Escape closes the More menu, otherwise hides the
tooltip and the popover. These listen on `document`, because focus starts on
body. Contracts the native checks read: `#accounts .account-row` (one per
account; provider headings are not rows), `#accounts .row-link` labels,
`#status` and `body[data-active]`. Account strings are React text, never
markup. `motion.css` owns every duration, curve and visual delay; only
`usage-updated` slides the gauge and rolls digits.

The popover reports intrinsic content height through a `ResizeObserver`,
coalesced in a microtask so reporting also works while the retained panel's
animation frames are suspended. `set_popover_height` takes `{ height, durationMs, curve }`,
with motion values read from `motion.css`. Rust clamps to an 80 pt floor and
`min(work-area height - 24 pt, 560 pt)`, then resizes on the main thread with
the top edge anchored. Only the account list scrolls at the cap. First show,
loading to first answer and Reduce Motion snap; Reduce Motion fades content.

Run `node --test macos/ui/logic` for row and accessible-label text, and
`python3 scripts/build_app_ui.py --test` for the popover's states, keyboard,
menu, tooltip, gauge, motion and listener cleanup. Regenerate app/native-envelope inputs with
`PYTHONPATH=src:tests python3 tests/generate_usage_fixture.py`.
`AGT_APP_TEST_USAGE_FIXTURE=usage-popover.json` (or `usage-nothing.json`) selects
an additional golden input in the isolated debug launcher. Install/Repair and
Add account navigate to Settings; their transactions/sheet arrive in steps 7/6.

## Settings integration contract (step 5, PART A)

`settings.html` is a retained second window (640×420). Closing hides it and
restores Accessory activation; gear, Settings… and a second app launch show it
with Regular activation. The menu keeps native Cut/Copy/Paste/Select All.
Custom Undo/Redo (⌘Z/⇧⌘Z) emit `settings-edit` with `{action:"undo"|"redo"}`.
The page returns these to WebKit with `document.execCommand(action)` when a
text field is focused; PART B owns the session settings history otherwise.

Only the Settings capability grants the following fixed-purpose commands;
`ui/tauri.js` remains the sole frontend adapter:

| Adapter | Arguments and result |
| --- | --- |
| `configShow(workspace = null)` | `config show --all --json`, returns the schema-1 rows including every level's value/revision; a broken-file exit 2 still returns the document and `error` |
| `configSet({...})` | `{key,value,level,workspace?,revision,userSaid?}`; value is a CLI value string; revision becomes `--if-unchanged` |
| `configUnset({...})` | Same fields without value; removes at the specified level, guarded by revision |
| `configRevert({...})` | `{entry,workspace?,userSaid?}`; agt's guarded log-entry revert (Activity) |
| `openSettingsFile(file, line = 1)` | Only paths returned by a config read are accepted; default editor with VS Code's URL or the native `kpos` line parameter; unsupported editors may ignore position; failed open reveals in Finder |
| `chooseWorkspace()` | Native folder dialog; canonical absolute path or null on cancel |
| `getOpenAtLogin()` / `setOpenAtLogin(enabled)` | Boolean state; actual autostart plugin LaunchAgent, default off; label `<bundle identifier>.app` (release: `io.github.watcharagorn.agents-router.app`), distinct from `.share` |
| `getSettingsTarget()` | Retained `{pane,runtime?,account?}` target so subscription/startup cannot lose navigation |
| `testBuildPaths()` | `test_build_paths`: a test build's `[{key,path,display,source,origin}]`, `null` in the release ([Test build folders](#test-build-folders)) |

Levels are `user`, `workspace`, `local`. Workspace/local writes require a folder
and run there as cwd. A null workspace uses HOME as cwd, so launching the app
from a repository cannot accidentally select that repository. Config mutations
are neither retried nor coalesced. Success is agt's structured write result
(`previous`, `revision`); refusals return `{ok:false,exit_code:2|3,error:{message,…}}`.
Transport/decoder errors reject the IPC promise. No human undo command is run.

`settings-target` carries new navigation, including General/User on every open.
Settings can use `getSnapshot()` for install state and uninstall blockers;
`onUsageUpdated()` carries successful fresh reads and `onSettingsStatus()`
carries loading/error/update changes. PART B adds scope/navigation, controls,
search, chips, error rows, file-only rows and the serialized undo stack.

`PYTHONPATH=src:tests python3 tests/generate_settings_inventory.py` regenerates
`ui/settings-inventory.json` from real `config show --all --json` in a scratch
home. The Python source gate checks every key and its mapping, including a
missing-key/wrong-control temp-copy regression case. Config exit-3 golden JSON
comes from `tests/generate_settings_fixtures.py`.

`cargo run --locked --bin agt-app-check -- --roundtrip` creates its own isolated
child HOME, AGT_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and fake-CLI PATH. It uses the
repo's real agt and the app's SettingsCore to set/read/undo `max_jobs` at User
and Workspace, and checks that an invalid value returns JSON without writing.
The Cargo autostart test also uses a child temp HOME and the actual plugin;
it writes/removes only that home's plist and never calls launchctl.

## Settings front end (step 5, PART B)

General renders agt's key order, per-level values and accepted levels. The
scope popup keeps chosen folders for the session. Search matches labels,
provider names, keys and descriptions without adding descriptions to rows.
The session undo/redo queue uses only `previous` and `revision`, with one
guarded config mutation per step; text-field undo stays in WebKit. Fields
commit on Enter or blur. File only values are read-only and each Open file
button passes its own resolved file and line to the core.

`show_setting_menu(key)` is Settings-only and opens the native Show history
menu. Its `settings-history` event carries `{key}`; the front end selects
Activity and keeps that key in Search and `body.dataset.historyKey` for step 6.
Roles, Accounts, Sharing and Activity panes are completed in step 6. The
Install/Repair and uninstall transaction actions await step 7; this step
renders their decided state and the uninstall blockers, without inventing
installation ownership when the snapshot does not report it.

## In-app updates (step 10)

The Rust updater checks at launch and after 24 hours; a suspended Mac's missed
checks coalesce, as do overlapping automatic/manual requests. Automatic results
wait in the popover's last row and tray menu. An install problem keeps priority
in the popover. `Check for Updates…` is in the popover's ⋯ menu, the native tray
menu and General. A manual check opens a sheet; only choosing the update starts
the download. Download/install failures offer Retry. Key/signature failures
default to `Open Releases Page`. The download has a progress sheet, and the app
restarts after installation. Every step is in the
[diagnostic log](#diagnostic-log). Test builds (the `.test` identifier) check no
feed; a manual check says so and offers the Releases page. The new runtime is installed on first launch and
the bundled `agt update --no-package` succeeds once per version, recorded under
`$AGT_APP_SUPPORT/updated/<version>` under the install lock. Failed refreshes
remain eligible for retry.

`scripts/check_app_update.py` builds a copied app at the current version and at
the next patch's `-test` version (for 0.17.1, 0.17.2-test) with a
throwaway key, writes both to the scratch folder's `versions.json` for the
self-test (no version is spelled in `update_app.rs`, and the source gate keeps it
so), serves a localhost feed, and exercises the actual updater and
LaunchServices restart in a scratch Applications folder. It checks the running
version, quarantine, strict code signature and once-per-version runtime/plugin
refresh, then quits both instances and stops the server. All keys, changed
versions and insecure transport config are temporary files under `build/`.
Diagnostics stay in `build/app-update-check.log`. The test needs native desktop
and FSEvents access. It never executes a provider CLI. Published builds contain
none of the debug-only rehearsal entry points.

The real updater public key is in the base config. The maintainer has stored
the private key and password as GitHub secrets. Ordinary keyless builds work
and skip updater signing. `check_sources(root, release=True)` in
`tests/test_app_sources.py` rejects the placeholder/missing/malformed key and
public-key release overlays;
`build_app.sh` applies the same release key/endpoint validation before staging
whenever a signing key is supplied. Run the source gate explicitly with
`PYTHONPATH=src:tests python3 tests/test_app_sources.py --release`. Test configs
cannot override a release key.

For future key replacement, the maintainer must perform key generation and
secret storage themselves. Set `AGT_RELEASE_KEY_DIR` to a private directory
outside the checkout, then run:

```sh
export CARGO_HOME="$PWD/build/cargo-home"
export PATH="$PWD/build/tools/bin:$PATH"
cargo tauri signer generate -w "$AGT_RELEASE_KEY_DIR/agents-router.key"
# Choose a nonempty password when prompted; keep both in the password manager
# and an encrypted offline backup. Never commit the private key.
python3 - "$AGT_RELEASE_KEY_DIR/agents-router.key.pub" <<'PY'
import json, pathlib, sys
path = pathlib.Path('macos/src-tauri/tauri.conf.json')
config = json.loads(path.read_text())
config['plugins']['updater']['pubkey'] = pathlib.Path(sys.argv[1]).read_text().strip()
path.write_text(json.dumps(config, indent=2) + '\n')
PY
gh secret set --env release TAURI_SIGNING_PRIVATE_KEY < "$AGT_RELEASE_KEY_DIR/agents-router.key"
gh secret set --env release TAURI_SIGNING_PRIVATE_KEY_PASSWORD
```

Use a GitHub `release` environment restricted to release tags; step 11's release
job alone receives those secrets. The public key is the only key committed.
Tauri's signing and static-feed format follow the
[updater documentation](https://v2.tauri.app/plugin/updater/).

Both windows use the shared `Tooltip`; timing lives only in `motion.css`. Run
`node --test macos/ui/logic` for scope, errors, field commits, serialized
undo/redo, revision refusal and blocker/zero-job states, and
`python3 scripts/build_app_ui.py --test` for the controls, panes and sheets. The
producer-generated `config-show-levels.json`, `config-show-broken.json` and
`usage-idle.json` extend the golden inputs. Light/dark evidence is under
`.mentor/plans/agt-macos-app/evidence/step5-general-*.png`.

## Diagnostic log

The app keeps a file of its update steps, start and exit, and errors. Launched
from Finder, its stderr goes nowhere; this file stays across launches and
updates.

| Build | File |
| --- | --- |
| Release | `~/Library/Logs/io.github.watcharagorn.agents-router/agents-router.log` |
| Test | `<app support>/logs/<identifier>/agents-router.log`, by default `~/Library/Application Support/Agents Router Test/logs/io.github.watcharagorn.agents-router.test/agents-router.log` |

- `~` is the launch HOME. A test build follows `--test-app-support` and
  `AGT_APP_SUPPORT`, so the check scripts' logs stay in their scratch folders.
  `~/Library/Logs/io.github.watcharagorn.agents-router` is a production folder:
  a test build refuses any folder that is, contains or sits inside it.
- The folder is `0700`, the file `0600`. One line per record:
  `2026-11-17T08:15:30.123Z [4242] update check start …`, the UTC time with
  milliseconds, the process id, the message. Control characters are escaped.
  Each line is written and flushed at once, so a restart or exit loses none.
- Rotation: before a write would take the file past 1 MiB, it becomes
  `agents-router.log.1`, replacing the previous `.1`. At most about 2 MiB on
  disk. A record is at most 4 KiB; a longer one ends in `…[truncated]`.
- Logging never changes what the app does. When the folder or file can't be
  opened or written, the app carries on and says so once on stderr.

Two sinks in `app.rs`: `log` writes stderr and the file, `trace` stderr only.
Each call site picks one; the logger never filters by message text.

| Reaches | Lines |
| --- | --- |
| The file and stderr (`log`) | The update flow, plugin refresh done, start, ready, the test build's folders, exit, restart, refusals, panics, and every error, rejection or deferred operation a failure caused |
| stderr only (`trace`) | Usage reads and successful results, popover, Settings, tray and geometry activity, wake notifications, the single-instance callback, prevented exits, the debug fixture launcher, self-test content and success |

Child-process output (agt's or `open`'s stderr, an AppleScript error) is
summarised, never copied: the file gets the operation, the error kind and the
exit status where there is one, and the full text goes to stderr only. A usage
read that fails logs `usage result failed generation=… error=Exit code=1` (or
`TimedOut`, `Execution`, `InvalidResponse`, `UnexpectedResponse`, `Refused`); a
failed plugin refresh or runtime cleanup logs `… deferred; its error is on
stderr only`.

What it records:

| Line | When |
| --- | --- |
| `start version=… identifier=… build=release\|test pid=… exe=…` | Each launch, once the folders are resolved. `std_exe=` follows when the OS reports another executable path |
| `update check start manual=… current=… endpoints=…` | A check starts: at launch, 24 hours after the last one, or on `Check for Updates…`. The 60-second timer's other ticks log nothing |
| `update check skipped: test build` | A manual check in a test build |
| `update check result available version=… current=… date=… target=… url=…` | The feed offers a newer version |
| `update check result current version=…` | Up to date |
| `update check result failed permanent=… error=…` | The check failed |
| `updater DEBUG: …`, `updater ERROR: …` | The updater plugin's own records, filtered as below |
| `update install start version=… current=… exe=… bundle=… bundle_version=…` | `Update to …` chosen: the bundle the install replaces and the version on disk there |
| `update download start url=…`, `update download progress 25%` (50, 75) | The download |
| `update download finished bytes=…; verifying signature` | Download done; the signature check runs next |
| `update signature verified bytes=…` | The signature matched |
| `update install replacing bundle=…` | The install starts |
| `update install ok bundle=… bundle_version=… expected=…` | The install returned. `bundle_version` is read from the bundle's `Contents/Info.plist` afterwards: the old version there means the install missed the bundle that relaunches |
| `update failed stage=check\|download\|install permanent=… error=…` | A failed download or install. A failure between `download finished` and `signature verified` is the signature |
| `update restart relaunch=open -n <bundle> exe=… test_args=…` | The relaunch target, logged before the relaunch |
| `tauri.process ERROR: failed to restart app…`, `tauri.app DEBUG: restart triggered on the main thread` (or `from a separate thread`), `tauri.app ERROR: failed to request exit: …` | Tauri's own restart records, filtered as below |
| `exit code=…`, `refused: …`, `panic thread=… at <file>:<line>: …` | Quit, a launch refusal after the folders resolve, a panic |
| `ready …`, `plugins refreshed version=…`, `usage result failed …`, `usage request rejected: <kind>`, `plugin refresh deferred; …`, `runtime cleanup deferred; …`, `… failed: …` | Setup finished, the plugin refresh after an update, and the failures above |

The updater plugin logs through the `log` crate. The app keeps only records
whose target starts with `tauri_plugin_updater` and whose message starts with
one of: `checking for updates`, `update endpoint returned 204`,
`update endpoint did not respond with a successful status code`,
`failed to check for updates`, `failed to deserialize update response` (quoted
text removed), `Searching for updater target`, `cannot swap the app bundles`,
`app installation needs admin privileges`, `failed to install the update`,
`failed to move the new app into place` (its AppleScript error, the privileged
shell's stderr, removed).

Tauri's restart logs through the same crate. From exactly `tauri::process`, at
Error: `failed to restart app: <io error>` and `failed to restart app with
LaunchServices`, which keeps `open`'s exit status and drops the stderr `open`
wrote. From exactly `tauri::app`: at Debug, `restart triggered on the main
thread` and `restart triggered from a separate thread`; at Error, `failed to
request exit: …`. Nothing else from Tauri.

Never recorded:
- the user info, query and fragment of any URL, also inside error messages;
- request headers and proxy settings;
- the updater public key and update signatures;
- the raw feed JSON: `update response:` and `parsed release response` are dropped.

The plugin exposes no HTTP status code, so the log has none. It shows what the
plugin reports:

| Response | Log |
| --- | --- |
| 2xx with a body | `update check result available …` with the offered version, or `current` |
| 204 | `update endpoint returned 204 No Content`, then `update check result current` |
| Any other status | `update endpoint did not respond with a successful status code`, then `update check result failed`; the code is unknown |
| No response | `failed to check for updates: <transport error>`, then `update check result failed` |

Read it:

```sh
tail -F ~/Library/Logs/io.github.watcharagorn.agents-router/agents-router.log
open -a Console ~/Library/Logs/io.github.watcharagorn.agents-router/agents-router.log
```

It's a file, not the unified log: `log show` doesn't read it.

## Settings pane IPC (step 6, PART A)

The pane UIs remain stubs for PART B. `ui/tauri.js` exports these Settings-only
operations; no new command is granted to the popover:

| Adapter | Arguments and result |
| --- | --- |
| `rolesShow(workspace = null)` | Schema-1 roles, allowed model/effort choices and per-key `sources`; no provider discovery |
| `rolesConfig({edit,level,workspace?,userSaid?})` | Tagged edit below; returns agt's role report or structured refusal |
| `accountsList()` | All runtimes, canonical names, `can_toggle`, `toggle_reason`, `found` and auth |
| `accountsCheck(runtime,account)` | A bounded check of a listed account; also refreshes usage |
| `accountsAddCheck(runtime,account)` | Side-effect-free `accounts add --check`; `ok:false` includes the name error |
| `accountsToggle(runtime,account,enabled)` | Enable/disable a listed, togglable account at User |
| `accountsLogin(runtime,account)` | Login for a listed account through the private `TerminalLauncher` handoff |
| `accountsAdd(runtime,account)` | Rechecks the name immediately, then hands Add account to Terminal |
| `shareList()` / `shareStatus()` | Read-only listing and status (exit 1 with attention remains data) |
| `shareKind(kind,enabled)` | `kinds add/remove` for `mcp` or `hooks`; preserves the result report, including partial failures |
| `sharePick(item,label,included)` | `--in` or `--not-in` for an editable cell from the last listing |
| `activityLog()` | `log --all --limit 200 --json`; newest first across workspaces, with event fields intact |

`edit` is one of `{kind:"enable",role,enabled}`,
`{kind:"reorder",role,providers:["codex","claude:work"]}`,
`{kind:"model",entry:"claude:work",value:"opus"}`,
`{kind:"effort",entry:"claude:work",value:"high"}`, or
`{kind:"restore",undo_from,if_unchanged}`. A null model/effort clears the override.
Model and effort apply to every role ("Applies to every role"), matching agt's entry-wide flags.
Restore uses only `undo_from`, `if_unchanged`, `level` and the original workspace
from a successful report in this app session. Rust rejects invented undo paths
or a different scope; agt refuses a changed file. The human `undo` string is
never executed. Successful restore returns another structured undo for redo.

Share value switches use existing guarded `configSet` at `level:"user"` with
no workspace; MCP servers and Hooks use `shareKind` so they apply immediately.
The top Sharing state comes from `enabled`; the `attention` entries carry
waiting actions, including new items whose names need not appear in `waiting`.
Non-editable cells remain Copy command actions. Picks have no undo queue.

Activity retains `type`, `id`, `time`, key/old/new/level/file, attribution and
sync `changes[].ok`; failed counts come from those changes. Use `configRevert`
with the entry's `id` and owning workspace (null for User). agt checks that the
file and current value still match that log entry. Files reported by roles or
Activity are also eligible for `openSettingsFile`. Search stays in the UI.

Opening Terminal is not success. On reactivation, refresh Accounts and usage
via the existing Settings status events. The debug fake serves the new pane
fixtures; its responses are static. The real-agt isolated `--roundtrip` now
also reorders builder providers, reads them back and applies structured undo.

## Settings panes (step 6, PART B)

The Settings window renders only the selected pane. The sidebar lists General,
Roles, Accounts, Sharing and Activity, then one pane per provider (Claude, Codex,
Antigravity, Kiro). A key's prefix picks its pane: `claude.*` goes to Claude,
`share.*` to Sharing, `roles.*` to Roles, and every other key to General. Rust's
`settings-target` still sends `general` or `accounts`; the frontend maps keys.
Every settings-document row carries `data-setting-key`, file-only rows included.

| Part | Source |
|---|---|
| Shell, scope, search, history, events | `ui/src/settings/SettingsApp.tsx` |
| Panes | `ui/src/settings/panes/*.tsx` |
| Installer, updater and Add account sheets | `ui/src/settings/sheets.tsx` (one `SheetHost`, priority installer, updater, Add account) |
| Controls, rows, menus, sheets | `ui/src/shared/` |
| Pure models | `ui/logic/settings.js`, `ui/logic/panes.js`, `ui/logic/install.js` |

`SettingsHistory` serializes General, provider, Sharing values and structured
Roles restore operations on the same session undo stack. Picks and account
switches stay reversible by toggling again. Activity Search searches only log
events; a row's Show history opens it at the key. New items (`share.auto_copy`)
offers agents and skills: both write `on`, none `off`, one writes its name.

Provider chips are ordered only for edit/full roles. Left/right arrows rove
focus, Option-arrows move providers, and native context menus offer Move to
front/up/down. Model and Effort sections show only agt's allowed choices and
label their entry-wide scope "Applies to every role". The account sheet invalidates its
successful name check immediately on input and ignores superseded replies.

Additional Settings-only operations complete pane actions:
`showPaneMenu(token, actions)` opens a native menu of fixed action names;
`copyText(text)` writes the native pasteboard without a browser gesture;
`setupRoles()` hands `agt setup` to Terminal; `activityUndoSync(entry)` re-reads
Activity, accepts only a successful single agent/skill item with a backup and
no later event for that item, then calls `agt share undo` for that item. A
repository-scoped event stays in Terminal. No frontend command string runs.

`node --test macos/ui/logic` includes the pane contract tests. The golden
producers also emit a full-access role, enabled Sharing configuration and a
backed-up sync/undo with its Activity event. WebKit renders of production HTML
at 640 × 420, using fake-agt responses, are saved under
`.mentor/plans/agt-macos-app/evidence/step6-{roles,accounts,sharing,activity}-{light,dark}.png`.
They verify this Mac's WebKit; the macOS 14.0 baseline and native captures stay
in the plan's later release checks.

## Runtime installation (step 8)

First launch stages `Contents/Resources/runtime` in
`$AGT_APP_SUPPORT/runtimes/.staging-*`, validates the manifest, plugin and
isolated agt version, then renames it and atomically switches `current`.
`install.lock` serializes installs across launches; an interrupted copy leaves
`current` usable. The stable launcher resolves that symlink once with `pwd -P`
and execs the physical Python with `-I`. A foreign public agt stays in place
until Replace; its original file/link and physical target are saved for Restore
previous agt and uninstall. Foreign version detection reads distribution metadata
without running that CLI. An older bundled version requires Downgrade.

`AGT_APP_SUPPORT`, `AGT_BIN_DIR` and `AGT_HOME` override all CLI destinations.
`AGT_APPLICATIONS_DIRS` is an OS path-list (`:` on macOS), ordered system then
user Applications. Production defaults are `/Applications` and `~/Applications`.
Install refuses a translocated or read-only app and apps outside these folders;
Move to Applications copies to the first writable destination as
`Agents Router.app` (a test build: under its own bundle name), opens that copy
and quits. It refuses to overwrite an
existing app.

Settings alone grants `get_install_status` and `install_action`. The latter
accepts a fixed action name and verifies the current dialog; it accepts no path,
executable or arguments. `install-status` carries progress/dialog state. The popover’s Install/Repair
opens Settings with `open_settings`’s optional `install: true`, requesting the
same fixed transaction. The
retained Settings sheet comes forward on first launch or a second launch during
install. Return selects its default (Cancel for Downgrade); Escape cancels.
After the last first-launch sheet closes, the popover opens once. Onboarding
requires positive `plugin.planned`, `roles.planned` or Sharing changes from
`setup --dry-run --json`, with a ten-second deadline. A timeout leaves Setup
status unknown / Check again in General. Set up hands the stable launcher to
Terminal. Subsequent versions refresh plugins using bundled `agt update
--no-package` once per version, recording successful completion under `updated/`.

Uninstall and cleanup take agt's actual `state/admission.lock`, then re-read
`usage --json` while holding it. They use `uninstall_blockers`, including
unassigned jobs, removed accounts and surviving workers. Cleanup keeps current,
previous and every active job's physical runtime from `spec.json`/`status.json`;
an unidentified runtime reference defers cleanup. Uninstall removes owned
launchers, runtimes and derived plugins, and restores a saved CLI only when its
target survives. It keeps settings, AGT_HOME and the saved install lock. Deleting
the app alone leaves its installed CLI and these files behind; use Settings to
uninstall the CLI first.

`cargo run --locked --bin agt-app-check -- --install-cli` creates a child with an
empty environment and scratch HOME, provider folders, PATH and all three install
overrides. It copies the shipped plugin and builds a small `bin/python3` shim:
version validation uses the test version, other calls run the repository's real
agt. Provider executables are `fake_host.py`; no installed agt or real provider
CLI runs. Each `OK:` line names its checked lifecycle case. Move records a
relaunch argv instead of opening a copied app. No runtime payload is bundled
until step 9.

`agt-app-check --install-cli --app <path>` installs from that app's own
`Contents/Resources/runtime` through the same transaction as first launch.
It requires explicit absolute `AGT_APP_SUPPORT`, `AGT_BIN_DIR` and `AGT_HOME`
destinations and prints the installed version and paths. A writable app copied
under `$TMPDIR` is accepted for this check; mounted and translocated apps still
require Move to Applications. It preserves a foreign public CLI.
The checker is a development binary, outside the release app:
`CARGO_HOME="$PWD/build/cargo-home" cargo build --locked --manifest-path macos/src-tauri/Cargo.toml --bin agt-app-check`.

Pipe one JSON document to `agt-app-check --stdin usage` to decode it with the
app's typed decoder. Other kinds use the fixture stems in `models.rs`'s
`FIXTURES` inventory, such as `config-show`, `accounts-list` and `roles-show`.
Success prints one `OK:` line; decode failures print the fixture name and field
path and exit 1. Folder checks use the same decoder and error format.

Step 9 measurement on this Command Line Tools-only Apple Silicon Mac (0.17.0,
CPython 3.13.18+20261026, ad hoc app and throwaway-signed updater):

| Component | Apparent bytes | MiB | Budget (MiB) |
| --- | ---: | ---: | ---: |
| App shell | 16,347,861 | 15.59 | 20 |
| Runtime | 83,927,233 | 80.04 | 120 |
| Complete app | 100,275,094 | 95.63 | 140 |
| DMG | 36,657,480 | 34.96 | 50 |

The provisional runtime and aggregate budgets in plan §8 row 17 are confirmed.
The measurement excludes caches and build tools. `build/app-build-report.json`
also records the architecture and minimum OS versions of every Mach-O file
found in the app.
