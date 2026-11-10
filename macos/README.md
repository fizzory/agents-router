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
Only the generated `build/release.conf.json` adds the runtime resource; the base
Tauri config stays usable for development without a staged runtime.

The script writes `dist/Agents-Router-<version>-arm64.dmg`. When
`TAURI_SIGNING_PRIVATE_KEY` is set (a key path or the key value accepted by
Tauri), it also copies `Agents Router.app.tar.gz` and its `.sig` to `dist/`.
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` supplies the key's password. Real release
keys are created and stored by the maintainer; the step 9 smoke check uses only
a throwaway key under `build/`. For that check, `TAURI_SIGNING_PUBLIC_KEY`
accepts the throwaway public key value or `.pub` path and overrides only the
generated overlay. Production builds use `plugins.updater.pubkey` in the base
config (step 10); the build refuses a signing key without a public key.

The build verifies the app and every nested Mach-O with strict codesign,
requires arm64 and minimum OS <= 14.0, compares the shipped plugin bytes, and
runs Python, native-module imports and `agt --version` in a private scratch
home. It mounts the DMG read-only to check the app and `/Applications` link,
then detaches it. A signed updater archive is extracted and checked the same
way. Strict signature verification runs again after Python execution.
`build/app-build-report.json` records every binary and apparent file sizes
(symlinks counted once): shell <= 20 MiB, runtime <= 120 MiB, app <= 140 MiB,
DMG <= 50 MiB. A failed check makes the build fail.

## Isolated native smoke check

Debug builds alone accept `AGT_APP_TEST_LAUNCHER`, an absolute path to
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
`reason=blur` and `reason=Esc`. FSEvents and a native app run require access to
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

`update-requested` is the hook for step 10's manual check/install alert;
automatic checks call Rust's `set_pending_update` to update both slots without
an alert. `open_settings` sends a `settings-target` event to Settings with
`{ pane, runtime, account }`; the page and account navigation are step 5/6.
Settings activation is Regular while visible and Accessory after hiding.

The popover uses the decided compact row anatomy. `logic/rows.js` owns pure
row/VoiceOver text and footer priority; `row-view.js` retains DOM nodes so
refreshes preserve keyboard focus. Each account has one sequential tab stop:
the inline action when present, otherwise the Settings row button. All account
strings enter the DOM through `textContent`. `motion.css` owns every motion
duration, curve and visual delay; only `usage-updated` animates meter values.

The popover reports intrinsic content height through a `ResizeObserver`,
coalesced in a microtask so reporting also works while the retained panel's
animation frames are suspended. `set_popover_height` takes `{ height, durationMs, curve }`,
with motion values read from `motion.css`. Rust clamps to an 80 pt floor and
`min(work-area height - 24 pt, 560 pt)`, then resizes on the main thread with
the top edge anchored. Only the account list scrolls at the cap. First show,
loading to first answer and Reduce Motion snap; Reduce Motion fades content.

Run `node --test macos/ui/logic` for row, accessible-label and DOM presentation
checks. Regenerate app/native-envelope inputs with
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
| `getOpenAtLogin()` / `setOpenAtLogin(enabled)` | Boolean state; actual autostart plugin LaunchAgent, default off; label `io.github.watcharagorn.agents-router.app`, distinct from `.share` |
| `getSettingsTarget()` | Retained `{pane,runtime?,account?}` target so subscription/startup cannot lose navigation |

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

Both windows use `tooltip.js`; timing lives only in `motion.css`. Run
`node --test macos/ui/logic` for controls, scope, errors, field commits,
serialized undo/redo, revision refusal and blocker/zero-job states. The
producer-generated `config-show-levels.json`, `config-show-broken.json` and
`usage-idle.json` extend the golden inputs. Light/dark evidence is under
`.mentor/plans/agt-macos-app/evidence/step5-general-*.png`.

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
Model and effort apply to **All roles**, matching agt's entry-wide flags.
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

`ui/panes.js` renders Roles, Accounts, Sharing and Activity inside the retained
Settings window. Pure models and name-check sequencing live in
`ui/logic/panes.js`; `SettingsHistory` serializes General, Sharing values and
structured Roles restore operations on the same session undo stack. Picks and
account switches stay reversible by toggling again. Activity Search searches
only log events; setting-row Show history opens it at the key.

Provider chips are ordered only for edit/full roles. Left/right arrows rove
focus, Option-arrows move providers, and native context menus offer Move to
front/up/down. Model and Effort sections show only agt's allowed choices and
label their entry-wide scope All roles. The account sheet invalidates its
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
Move to Applications copies to the first writable destination, opens that copy
and quits. It refuses to overwrite an existing app.

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
also records all 12 Mach-O architectures and minimum OS versions.
