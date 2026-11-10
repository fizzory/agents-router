# macOS app development

The production executable is the stable launcher at
`~/Library/Application Support/Agents Router/bin/agt`. The app never searches
PATH. Runtime installation is implemented in a later plan step.

All Cargo invocations use `export CARGO_HOME=$PWD/build/cargo-home` from the
repository root and `--locked`. Tools are checked by
`scripts/check_app_tools.sh`; this task installs no tools.

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
