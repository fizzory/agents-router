# Instrument

Agents Router is a finely made pocket instrument. Surfaces stay quiet.
Quota figures and gauges carry the boldest visual weight. Motion feels damped.

## Principles

- Show quota and running work at a glance.
- Use space, alignment and fill to group content.
- Reserve colour for state and the system accent.
- Make every control respond to the action that caused the change.
- Keep labels, keyboard access and hit targets intact.

## Palette

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| `--ink` | Graphite `#1D1F24` | `#ECEDF0` | Primary text; source of neutral fills |
| `--ink-muted` | Pewter `#5E6270` | `#A3A7B3` | Supporting text |
| `--ground` | Porcelain: Graphite 4.5% mixed with white, about `#F5F5F5` | Obsidian `#17181C` | Window and sidebar |
| `--ground-raised` | Plate `#FFFFFF` | Plate `#202227` | Banner, sheet, menu, tooltip |
| `--ground-field` | Ink 5% | Ink 8% | Text, number and search fields |
| `--fill-tag` | Ink 5% | Ink 8% | Tags and badges |
| `--fill-control` | Ink 8% | Ink 12% | Push, inline and popup buttons, steppers, provider chips |
| `--fill-hover` | Ink 12% | Ink 16% | Pointer and row focus feedback |
| `--fill-press` | Ink 16% | Ink 20% | Pressed controls, rows and menu items |
| `--switch-off` | Ink 49% | Ink 40% | Off track |
| `--gauge-track` | Ink 10% | Ink 10% | Gauge track |
| `--gauge-other` | Ink 55% | Ink 55% | Secondary usage bar |
| `--gauge-main` | Ink 85% | Ink 85% | Dominant usage bar |
| `--signal-ok` | `#18843C` | `#3BC865` | Available |
| `--signal-warn` | `#835000` | `#FFB12B` | Busy, recovering, cooling, high usage |
| `--signal-bad` | `#B52D23` | `#FF453A` | Failure, logged out, invalid field |
| `--accent` | `AccentColor` | `AccentColor` | Focus, selection, enabled switch |

“Ink N%” means `color-mix(in srgb, var(--ink) N%, transparent)`.
Fills composite over their surface. The dominant bar uses the warning colour
at its existing threshold. State words accompany status colours.

Keep the existing accent mixes. `--accent-fill` is `AccentColor` 82% mixed
with black. `--accent-text` uses the same mix in light mode and `AccentColor`
70% mixed with white in dark mode. The default button keeps its accent fill.

## Borderless rules and contrast

Keep each existing 1px border width. Set `--edge` and `--hairline` to
transparent. Dividers, the sidebar edge, the toolbar edge and the popover
footer edge become space. Keep the focus ring, the invalid-field ring in
`--signal-bad`, and hollow status-dot rings for unknown and off.

More Contrast restores `--edge` to `#8E8E96` in light mode and `#74747E` in
dark mode. It sets `--hairline` to `var(--edge)`.

Menu keyboard focus uses a 2px ink ring with a -2px outline offset. The ring
stays inside the item and clear of the menu mask. Ink gives a fixed contrast
against the focused fill, independent of the system accent colour.
The ring measures 12.981:1 in light mode and 8.525:1 in dark mode against
the focused menu fill.

The following ratios use WCAG relative luminance after sRGB alpha
compositing. Porcelain uses the unrounded colour mix. The gauge fill
composites over the track, which composites over the named surface.

| Measurement | Light ground | Light Plate | Dark ground | Dark Plate |
| --- | ---: | ---: | ---: | ---: |
| Off track against surface | 3.079:1 | 3.143:1 | 3.441:1 | 3.365:1 |
| Secondary gauge against track | 3.445:1 | 3.538:1 | 4.676:1 | 4.318:1 |
| Muted ink against surface | 5.568:1 | 6.075:1 | 7.379:1 | 6.620:1 |

The off track and secondary gauge exceed 3:1 on both opaque surfaces in
both modes. Muted text exceeds 4.5:1. Light off-track ink is 49% because
48% measures 2.995:1 against Porcelain. These ratios describe enabled,
full-opacity elements. Disabled and stale states retain their existing dimming.

## Type

Use local system fonts. Load no web fonts.

| Token | Value | Role |
| --- | --- | --- |
| `--font-text` | `-apple-system, BlinkMacSystemFont, system-ui, sans-serif` | Interface text |
| `--font-figure` | `ui-rounded, -apple-system, system-ui, sans-serif` | Usage figures |
| `--type-title` | 600, 13px / 16px | Popover and sheet titles |
| `--type-body` | 400, 13px / 16px | Controls and settings labels |
| `--type-row` | 400, 12px / 16px | Row text |
| `--type-small` | 400, 11px / 14px | Supporting text |
| `--type-figure` | 600, 12px / 14px, rounded, tabular | Usage figures only |
| `--type-display` | 600, 15px / 20px, tracking -0.01em | Settings toolbar or pane title only |

Keep the digit strip at 14px. Keep its final `translateY(-14px)`.
Leave the popover title's type unchanged.

## Space

| Token | Value | Token | Value |
| --- | ---: | --- | ---: |
| `--space-1` | 2px | `--space-5` | 12px |
| `--space-2` | 4px | `--space-6` | 16px |
| `--space-3` | 6px | `--space-7` | 24px |
| `--space-4` | 8px | `--space-8` | 32px |

Keep `--hit` at 24px. Keep existing expanded hit areas around small controls.
Popover account rows are 28px tall.

## Shape

| Token | Base radius | Role |
| --- | ---: | --- |
| `--sq-xs` | 6px | Tags, badges, search marks |
| `--sq-s` | 8px | Fields, search field, buttons, popup, stepper, provider chip |
| `--sq-m` | 10px | Rows, nav items, disclosure summary, row link, update action |
| `--sq-l` | 12px | Plates, banners, menus |
| `--sq-xl` | 16px | Sheet and current popover body |
| `--sq-pill` | 999px | Pill tracks |

Each xs–xl radius is `calc(Npx * var(--sq-k))`. The multiplier is 1.
Inside `@supports (corner-shape: squircle)`, it becomes 1.5. Every rule
using that scale sets `corner-shape: squircle`. Pills and circles stay round.

Use concentric corners: inner radius = outer radius − padding. Resolve
the outer radius first. Menu items derive their radius from the menu's
radius minus its 4px padding. Fields add 1px to the s radius for their
transparent hit border.

Inside `@supports not (corner-shape: squircle)`, the sheet Plate and HTML
menu surface use `-webkit-mask-box-image` with nine-slice SVG masks.
Continuous corners extend 1.53 times the base radius. The l slice is
18.36px; the xl slice is 24.48px. The assets are real files under `assets/`,
served from `'self'`. Vite keeps `assetsInlineLimit: 0`.

The sheet's masked Plate sits behind its content, inside the 1px border.
The More Contrast edge stays visible. Popup menus and focus rings remain
unclipped. All other elements use plain border-radius on
Safari 17. The native panel clips the popover's corners with a continuous
16pt radius. The page draws a plain rectangle, with no CSS radius and no
SVG mask.

## Elevation

Only floating surfaces use `--shadow-float`. Use two ambient layers and no
decorative outline.

| Layer | Geometry | Light black opacity | Dark black opacity |
| --- | --- | ---: | ---: |
| Ambient | 0 2px 8px | 6% | 18% |
| Lift | 0 12px 32px | 12% | 36% |

Sheets, menus and tooltips share these layers. Masks clip box shadows.
On the fallback path, sheet and menu wrappers apply the two layers with
`filter: drop-shadow(...)`. Keep the sheet filter off the content ancestor
so fixed menus retain viewport coordinates.

## Motion

`motion.css` owns all durations, delays and curves. Duration tokens use
plain `ms` values. Preserve every existing motion token name and value.
`--ease-out` stays `cubic-bezier(.2, .8, .2, 1)` and
`--panel-resize-time` stays `160ms`. Rust receives the existing four-number
cubic-bezier, never a CSS spring.

| Token | Duration | Role |
| --- | ---: | --- |
| `--t-instant` | 90ms | Discrete control press feedback |
| `--t-quick` | 160ms | Short responses |
| `--t-base` | 240ms | Switch settling |
| `--t-slow` | 360ms | Long settling responses |

`--spring-settle` has about 2% overshoot. `--spring-snap` gives a short,
tighter response. Both start as cubic-bezier fallbacks and upgrade to
`linear()` springs inside a feature query. They are CSS-only. Discrete
controls press to scale(.97). Rows and menu items darken to `--fill-press`.

The only lively moments are:

- Only bars whose value changed spring. Only changed digits roll.
- A solid status mark breathes while a job runs with room for another job.
  Use the 2400ms opacity-and-scale breath. A full account keeps its waiting
  half disc.
- The switch springs.
- Copy morphs to a check over 200ms and holds for 1500ms.
- Refresh turns while reading, cross-fades to a check for 1500ms, then returns.
- Newly appearing rows fade with a 4px rise, 24ms apart, at most 8 rows.
  Refresh never staggers existing rows.
- A setting flashes after a change.

Fresh usage remains the trigger for value motion. Compare each bar and digit
with its previous value before animating. Keep the setting-flash trigger.
The Popover section defines its response timings for Phase 1.

## Reduce Motion

Use cross-fades only. Snap gauges, switch thumbs and chip positions.
Settle digits at their final value. Disable press scaling, breathing,
icon rotation and disclosure rotation transitions. Panel and sheet
entrances fade. Keep the `body[data-active="false"]` animation and
transition kill-switch.

## Friction rules

The popover follows these rules in Phase 1. They govern the Settings
redesign in Phase 2.

- A setting applies instantly, with undo instead of save or confirm.
- Destructive actions undo cannot restore, including log out and remove
  account, keep one confirm sheet.
- Each row's primary action takes one click and is always visible.
- Secondary controls remain keyboard reachable. Compact row controls appear
  on hover or focus; account Settings opens from detail.
- The popover answers “who has quota, what is running” with no clicks.
- An empty state offers the one next action.

## Popover

Build and ship this spec in Phase 1.

The popover answers “can I run now, who has quota, what is running” without
clicks. One bold figure per row carries usage. Mark shape carries readiness;
colour reinforces it. agt already ranks accounts, so there is no best-account
pick. Detail opens in place.

[Approved canvas](https://claude.ai/artifact/BFmyKL7zr3N1odWHuyKtEB#artboard-PopoverModern)
(private to the user). The rules below stand on their own.

### Header and summary

The header is 32px tall. Put the summary on the left in `--type-title`.
Put Refresh, Settings and More icon buttons on the right. Keep the tooltips
`Refresh ⌘R`, `Settings ⌘,` and `More`. Add freshness to Refresh's tooltip:
`Refresh ⌘R · Updated 2m ago`.

| Condition | Summary |
| --- | --- |
| One or more accounts can take a job | `3 ready` |
| None ready; an account has a known return time | `None ready until 20:15` |
| None ready; no known return time | `None ready` |
| None ready while an account check runs | `Checking accounts…` |
| Loading or no accounts | `Agents Router` |
| agt cannot be read | `Can't read agt` |
| agt is missing | `agt isn't installed` |
| agt is too old | `agt <version> is too old` |

The ready count equals the number of solid marks, including breathing solid
marks. Derive both from one rule in `logic/rows.js`. Trouble takes summary
priority. Otherwise loading or no accounts uses the title; a positive count
uses the count; with none ready, a running check takes priority over a return
time, then use the earliest known return or `None ready`.

There is no footer unless an update or trouble row is showing. There is no
popover-wide freshness line. Keep the empty state's `No accounts yet.` and
one `Add account…` action opening Settings.

The first read reserves static skeleton rows. Keep `Loading accounts…` in
the live region and Refresh turning as the only moving loader.
Remember the last successful account row count per viewer, clamped to 1–6;
default to 3.

### Account rows

Group rows by provider: Claude, Codex, Antigravity and Kiro. Each row is 28px
tall with `--sq-m`, 8px horizontal padding and 8px gaps. The 360px popover
leaves 348px for the row. Read left to right:

| Part | Size and treatment |
| --- | --- |
| Status mark | 8px; shape and state label from the table below |
| Name | About 150px, flexible; 12px / 16px, middle ellipsis |
| Gauge | 56 × 10px; two 4px pill bars, 2px apart; 5 hours above week |
| Slot | 96px, right-aligned; window tag, figure and countdown, or state/action |

The figure names the busier usage window. Its bar uses `--gauge-main`, with
`--signal-warn` at 95% or above. The other bar uses `--gauge-other`.
Cooling uses the warning colour on the 5-hour bar. Kiro and Antigravity
reserve the gauge space but show no bars.

| Mark | States | Accessible state label |
| --- | --- | --- |
| Solid | `ok`; `recovering`; ready to take a job | `Available`; `Recovering, available` |
| Breathing solid | `busy` with capacity for another job | `Busy, available` plus the job count |
| Half disc | `cooling`; `busy` with every slot full | `Cooling down` plus the return time; `Busy, full` plus the job count |
| 2px ring | `logged_out`; `unhealthy`; failed account check | `Logged out`; `Unhealthy`; `Check failed` |
| 1px hollow ring | `off`; unknown or never checked | `Off`; `Unknown` |

Dim off rows. Include the state label in the row's accessible name. Keep the
mark decorative to avoid a duplicate announcement. During an automatic
check, announce `Checking…` with the account name. Shape never depends on
colour alone.

### Slot formats

| Part | Format |
| --- | --- |
| Window tag | `5h` or `wk`; 14px column, muted 11px |
| Figure | `42%`; 32px column, `--type-figure`, rounded tabular digits |
| Countdown | 40px column, muted 11px; `42m`, `2h 10m`, `3d 4h`, `<1m` |
| Stale age | Replace the countdown with `4m ago` or `2h ago`; dim figure and bars |
| Unknown since reset | – plus age; dim figure and bars |

For example, show `wk 86% 2d 22h` or `5h 42% 2h 10m`. Countdowns express
time remaining. Use clock times in detail and return states. Age units have
no space before the suffix: `2m ago`, `2h ago`, `3d ago`. This `ageText`
format also applies to Settings rows in Phase 1.

| State | Slot text or action |
| --- | --- |
| Running jobs | `2 of 4 jobs` |
| Cooling or a timed return | `Back 13:05` |
| Recovering without a return time | `Recovering` |
| Disabled | `Off` |
| Account check running | Static skeleton bar; `Checking…` for screen readers |
| Account check failed | `Retry` |
| Logged out, including a successful check with `logged_in:false` | Copy icon plus `Log in…` |
| Ready Kiro or Antigravity | `Ready` |

A row with `problem` set offers no slot action button. A failed check and
a never-checked account are distinct states. Never show a per-row `Check`
button.

### Detail and login

Click, Return or Space toggles detail below the row. One panel is open at a
time. Close all detail when the popover closes. Do not expand on hover.
The row button carries `aria-expanded` and identifies its detail panel.
Closed detail is hidden from focus and screen readers.

Use 11px / 14px lines, indented to the name:

| Line | Example |
| --- | --- |
| Five-hour window | `5 hours  17%  Resets 14:20` |
| Weekly window | `Week  86%  Resets Wed 09:00` |
| Context and freshness | `2 of 4 jobs · Updated 2m ago`, or the account's reason and age |
| No jobs or reason | `Updated 2m ago` |
| Kiro or Antigravity | `No usage reported · Updated 2m ago` |

Put inline `Settings…` at the right of the last line. It opens that account
in Settings. Always include a reset's time; a weekday alone is incomplete.

Logged-out rows keep `Log in…` visible. Put a 24px copy icon to its left,
shown on row hover or `:focus-within`. Its accessible name is
`Copy login command for <account>`. Copying gets the command without opening
Terminal or creating a handoff file, copies it, and morphs the icon to a
check. An sr-only live region announces `Copied`.

`Log in…` opens Terminal in one click. If Terminal cannot open, open the
row's detail with `Couldn't open Terminal.` and a `Copy command` button.
After copying, that button reads `Copied`. Both paths use the same quoted
login command. Invalid accounts reject rather than offer a command.

### Automatic account checks

In Phase 1, check due accounts when the popover opens, including reactivation
after Terminal. The Settings-open trigger and Settings' `Checking…`/`Retry`
UI belong to Phase 2. Keep today's Settings check button and behaviour in
Phase 1. Run at most once per 10 minutes per account. Ordinary usage
polling never triggers an account check. Rust owns the 600-second throttle,
retained results and in-flight dedupe across both windows. Seed the throttle
from cached check timestamps so reopening or restarting does not repeat work.

Show a static slot bar with `Checking…` for screen readers, then the result.
A successful check with `logged_in:false` shows the 2px ring and `Log in…`.
Show `Retry` only when the check itself fails. Explicit Retry bypasses the
throttle after failure and joins an active check. Never-checked rows start
automatically and show the same static bar; they have no Check button.

### Trouble row

Make the summary the problem. Directly below the header, show the full error
sentence and the data's age in muted 11px / 14px text. For example:
`agt usage --json timed out after 10 seconds. The numbers below are from 4 minutes ago.`
Wrap to at most four lines, then scroll. Do not cut off the error.

Put `Retry` at the right. While reading, replace it with `Retrying…` at the
same width. Refresh already turns for that read. For missing or old agt,
use `Install…` or `Repair…`; both open Settings.

Keep the last values and dim the rows as stale. Usage slots show age instead
of countdown, such as `4m ago`. Timed return states keep `Back 13:05`.

### Update row

An available update gets a 28px bottom row: 6px accent dot,
`<version> is available`, and inline `Install and Restart`. One click starts
installation. Show spinner plus `Installing…` during download and install.
Do not open Settings automatically or ask for confirmation. Running agt jobs
use detached runners and retain their pinned physical runtimes across restart.

| Failure | Exact title | Button |
| --- | --- | --- |
| Retryable, download phase | `Couldn't download <version>.` | `Retry` |
| Retryable, install phase | `Couldn't install <version>.` | `Retry` |
| Permanent, download phase | `Couldn't verify <version>.` | `Open Releases Page` |
| Permanent, install phase | `Couldn't install <version>.` | `Open Releases Page` |

The phase and `retryable` together set the title, as `Dialog::failure` in `update.rs` does. `retryable` alone sets the button. Retry runs the
same command as `Install and Restart`, without a confirm, and goes straight
to spinner plus `Installing…`. `Open Releases Page` opens `/releases/latest`
for this repository in the browser and does nothing else.

Failures use the problem-footer layout: text plus inline footer button, no
accent dot. Put Rust's error detail in the tooltip and `aria-describedby`,
not inline. The row remains until the next state replaces it: installation,
success or the next update check. There is no dismiss control.

### More, keyboard and accessibility

More opens a native `NSMenu` at the button through
`showPopoverMenu({x,y})`, using the button's logical coordinates. Its items
are `Check for Updates…`, a separator, and `Quit Agents Router ⌘Q`. Dispatch
directly to the existing update and quit handlers. Keep Settings' native
`showPaneMenu` separate. Remove the in-page popover menu.

The account list remains one Tab stop. Up/Down and Home/End move between
rows without wrapping. Left/Right move between row parts. Add the copy icon
as a third roving part after `[ROW_LINK, ROW_ACTION]`; Right reaches it.
Keep focus by account key across refreshes. Detail controls are reachable
only while open. Keep ⌘R for Refresh, ⌘, for Settings and ⌘Q for Quit.
Escape dismisses the native menu first, then the popover.

Drop WebKit's focus on Refresh while the panel opens, until `--panel-time`
after it shows, unless the person presses a key or pointer in the page.
Keep later focus, including VoiceOver's.

Every button has a visible accent `:focus-visible` outline and its accessible
name. Keep icon-button tooltips and all accessible labels. Announce copy
success in a polite live region. Row state, quota window, figure, reset and
reason remain available to screen readers after row tooltips are removed.

### Popover motion

| Trigger | Response |
| --- | --- |
| Open | `--panel-time` fade with a 4px rise |
| First read | Static skeleton rows fade in after `--loader-delay`; Refresh turns |
| Hover or focus | Row fill and copy icon fade in over 100ms |
| Detail open or close | Native height over 160ms ease-out; content cross-fades over 200ms |
| Fresh usage | Only changed bars spring over 240ms with `--spring-settle`; only changed digits roll |
| New rows | Fade with a 4px rise, 24ms stagger, at most 8 rows |
| Slot state change | 200ms cross-fade |
| Account check running | Static slot bar; result uses the slot cross-fade |
| Refresh | Icon turns while reading, except while `Installing…` shows its spinner; cross-fades to a check for 1500ms (`--copied-delay`), then back |
| Copy | Icon morphs to a check over 200ms, held for 1500ms |
| Running with spare capacity | Solid mark breathes over 2400ms |
| Trouble or update row change | 160ms native resize and 200ms cross-fade |

Interrupt a detail resize from its current height. Never stagger gauges or
existing rows on refresh. Reduce Motion uses cross-fades only: settle bars
and digits immediately, remove rise, breathing and spinners, and keep account
checks as static bars with `Checking…` for screen readers. Keep the
hidden-page animation kill-switch.

### Removed

- The fixed `Agents Router` title, except while loading or with no accounts.
- Day-only resets such as `Wed`.
- Row tooltips.
- The in-page More menu.
- Gauge stagger on every refresh.
- `Update to <version>…` opening Settings.
- `Can't read agt` at the bottom and cut-off error text.
- The wording `No account ready until`.
- Empty slots on ready Kiro and Antigravity rows.
- Per-row `Check` buttons, including on never-checked rows.

### Trade-off and reversal

Opening an account in Settings takes two clicks: open detail, then
`Settings…`. Reading detail is the frequent task; Settings is the rare one.

| Reverse if | Change |
| --- | --- |
| Users open Settings from rows more often than they read detail | Row click opens Settings; move detail to a chevron |
| agt stops ranking accounts itself | Add `Best now: claude-work` |
| Jobs become app-owned or updates stop retaining physical runtimes | Revisit restart safety and confirmation |
| The next 24-hour update check keeps offering a version that failed permanently | Keep its permanent failure row until a different version is found |

## Next phase

### Phase 1: Popover

Implement and ship the popover first. Phase 1 changes Settings only where
shared code forces it:

| Shared change | Settings boundary |
| --- | --- |
| Age text | Shared rows use `2h ago` through `ageText` from step 1. |
| Account checks | The existing Settings check command delegates to step 4's shared controller. Keep today's check button and behaviour. |
| Update status | Publish status to both windows in step 7. Keep Settings' update dialog unchanged. |

The Settings-open trigger for automatic checks and Settings' `Checking…`/
`Retry` UI belong to Phase 2. Settings redesign is deferred to that phase.

Build in this order. Paths below are relative to the repository root.
Each API step updates command registration,
`macos/src-tauri/capabilities/{popover,settings}.json`,
`macos/ui/window-permissions.json`, fake adapters, component tests and
`macos/README.md` alongside its implementation. Cover Rust, JS logic,
React, source guards and isolated native checks as each step requires.

#### 1. Popover UI on today's data

Build the summary and shared solid-mark count rule, mark shapes, window tag,
figure and countdown, in-place detail, trouble row, motion and removals.
Apply the new-row-only 24ms stagger, capped at 8 rows. Only changed bars
spring and changed digits roll; refresh has no stagger. Add the refresh
check. Verify breath against every running-job state: busy with room
breathes; full stays a half disc. Wire any added running states to the same
readiness rule. Keep the native shape and API work in their later steps.

Files: `macos/ui/src/popover/PopoverApp.tsx`, `macos/ui/src/popover/popover.css`,
`macos/ui/src/shared/{AccountRow,StatusDot,UsageGauge,RollingFigure}.tsx`,
`macos/ui/src/shared/{shared.css,types.ts}`, `macos/ui/motion.css`,
`macos/ui/logic/{rows,usage}.js` and their logic/component tests.
`ageText` currently lives in `rows.js`; its `2h ago` format ripples to the
shared Settings rows. Verify those rows too.

Done when: today's rows and trouble follow the rules, counts match marks, and unchanged values stay still.

#### 2. Cached account-list backend prerequisite

Add `accounts list --cached-only --json`, backed by `account_check.py`.
Include additive `login_check` metadata from the existing check record.
Switch the app's list request to it; listing must not call live `auth_status`
or refresh Codex usage outside the app throttle. Leave `usage --json` and
`accounts check --json` shapes unchanged.

Files: `src/agents_router/{cli,tools,account_check}.py`,
`macos/src-tauri/src/{models,panes}.rs`, affected Python/Rust tests,
`tests/generate_{usage_fixture,settings_fixtures,settings_inventory}.py`,
`tests/fixtures/app/` and CLI contract docs. This is a contract change:
regenerate producer fixtures and decode real cached-list output with
`agt-app-check --stdin <kind>`, including null, failed and stale checks.

Done when: listing makes no live checks and the app decodes null, failed and stale check metadata.

#### 3. Login command and copy

Add `getLoginCommand(runtime, account): Promise<string>` and expose existing
`copyText(text)` to the popover. Return
`{status:"opened"|"terminal_failed", copy_command:string}` from `loginAccount`.
Extract a pure Rust command formatter. Keep quoting, account validation and
test-build isolation in Rust; copying creates no handoff file. Share the
result with Settings login. Build the logged-out copy icon, its third roving
part, live announcement and 200ms check morph with a 1500ms hold. Build the
Terminal-failed detail and `Copy command` → `Copied` fallback.

Files: `macos/src-tauri/src/{terminal,app}.rs`, `macos/ui/tauri.js`,
`macos/ui/src/popover/{api.ts,PopoverApp.tsx}`,
`macos/ui/src/shared/{AccountRow,CopyButton}.tsx`, shared API types,
`macos/ui/src/settings/api.ts`,
`macos/ui/src/settings/panes/AccountsPane.tsx` and affected tests.
Test literal quoting, private HOME, failed opening and unused-file cleanup.

Done when: login and keyboard-reachable copy share the command, with a working Terminal-failure fallback.

#### 4. Shared Rust account-check controller

Add `account_checks.rs` with a 600-second per-account throttle, retained
results and in-flight dedupe. Both existing check commands delegate to it;
do not rely on separate webviews or `AgtClient`'s queued reads to share work.
Expose `getAccountChecks()` and `account-checks-status` snapshots containing
`{runtime,account,phase,result}`. Seed from cached check timestamps. Trigger
due checks from native popover opening, including reactivation after Terminal.
Keep ordinary usage polling out of this path. Popover Retry bypasses the
throttle only after failure and still joins an active check. Settings keeps
today's check button and behaviour; only its existing command delegates to
the controller. Add the Settings-open trigger and its `Checking…`/`Retry`
UI in Phase 2.

Files: new `macos/src-tauri/src/account_checks.rs`,
`macos/src-tauri/src/{lib,app,client,panes}.rs`, `macos/ui/tauri.js`,
`macos/ui/src/popover/{api.ts,PopoverApp.tsx}`,
`macos/ui/src/settings/{api.ts,SettingsApp.tsx,panes/AccountsPane.tsx}`,
shared API types and affected tests. Cover concurrent windows, reopen/restart,
the 600-second boundary, failure and Retry. Distinguish logged out from failed
or never checked; never-checked popover rows have no Check button.

Done when: both windows share the controller and results, the popover shows `Checking…` → result and Retry only after failure, and Settings keeps today's check button and behaviour.

#### 5. Native More menu

Add popover-only `showPopoverMenu({x,y}): Promise<void>`. Use `popup_menu_at`
with logical coordinates from the button. Fixed native items dispatch to
existing update/quit handlers; no token/action callback protocol is needed.
Retain Settings-only `showPaneMenu`. Remove the popover's in-page menu.

Files: `macos/src-tauri/src/app.rs`, `macos/ui/tauri.js`,
`macos/ui/src/popover/{api.ts,PopoverApp.tsx,popover.css}` and affected tests.
Check native anchoring, keyboard dismissal and blur handling in isolation.

Done when: More opens the native menu at its button; dismissal preserves focus and popover visibility.

#### 6. Native popover shape

Give the native panel ownership of the shape. Set `hasShadow` explicitly.
Use a layer-backed `contentView` with `cornerRadius` 16, `cornerCurve`
`.continuous` and `masksToBounds`. Call `invalidateShadow` on resize
notifications, in the `NSAnimationContext` completionHandler, and after
snaps or Reduce Motion. Then draw a plain rectangle in the page and remove
the CSS radius on body. Until that lands, keep body's CSS xl radius.

Files: `macos/src-tauri/src/native.rs`, `macos/src-tauri/Cargo.toml`
(`CALayer` feature), `macos/ui/src/popover/popover.css` and affected native
tests. Check rounded clipping, shadow updates during resize and Reduce Motion.

Done when: native corners clip continuously and shadows follow resizes and snaps, with no CSS body radius.

#### 7. One-click update

Add `installUpdate(): Promise<void>` using the existing updater controller
and one shared install guard. Share `getUpdateStatus`/`onUpdateStatus` with
both windows. Publish `{version,phase,received,total,retryable,detail}` alongside
Settings' unchanged dialog. Add a popover open-releases command; the existing dialog's
`update_action` cannot serve it. Render available, Installing… and all four
failure states from the Popover section. Installation never opens Settings.
Restart needs no running-job confirm: `jobs.py` uses `fsutil.spawn_detached`,
and `install.rs::cleanup` retains each job's pinned physical runtime.

Files: `macos/src-tauri/src/{update_app,update,app}.rs`, `macos/ui/tauri.js`,
shared API types, `macos/ui/src/{popover,settings}/api.ts`,
`macos/ui/src/popover/{PopoverApp.tsx,popover.css}`, `macos/ui/logic/rows.js`
and affected tests. Cover duplicate clicks, progress, retryable/permanent
failures and an isolated restart with a surviving fake job.

Done when: install and Retry take one click, permanent failures open Releases, and a fake job survives restart.

#### 8. Ship Phase 1

Ship the Instrument foundation already on this branch together with the
popover.

1. Run the full test suite.
2. Check open PRs' version lines at ship time. Choose the release version
   then and bump the whole `scripts/build_app.py` `versions()` set.
3. Regenerate producer fixtures with `tests/generate_usage_fixture.py`,
   `tests/generate_settings_fixtures.py` and
   `tests/generate_settings_inventory.py`.
4. Open a PR with the release notes in its body.
5. Wait for CI and resolve failures.
6. Merge, then tag and publish on the user's word.

Done when: the release is published and the installed app shows the new popover.

### Phase 2: Settings redesign (deferred)

Settings needs a large redesign. Start with its own design round, the way
the popover did: research, an audit of the current Settings window, a UX
spec, a prototype the user confirms, then its own build plan. No build steps
yet.

The canvas boards “Earlier draft: Settings…” and “Add account sheet” are
superseded. Phase 2 starts fresh.

Phase 2 inherits the Settings-open trigger for automatic checks and its
`Checking…`/`Retry` UI from the shared controller in Phase 1. It also inherits
the friction rules and the existing Settings items below.

Cover General, Accounts, Roles, Sharing and Activity. Apply settings
instantly with undo; keep one confirm sheet for destructive actions undo
cannot restore. Keep primary actions visible
and one click away, secondary actions on hover or focus and keyboard
reachable, and empty states focused on one next action. Carry the copy morph
through shared Settings controls. Verify any additional running-job states
against the breath/readiness rule.

Files: `macos/ui/src/settings/SettingsApp.tsx`,
`macos/ui/src/settings/panes/*.tsx`, `macos/ui/src/settings/settings.css`,
affected shared controls, `macos/ui/logic/{settings,panes}.js` and their tests.

Done when: Settings meets the friction rules, with keyboard access, labels, hit targets and no redundant confirms.
