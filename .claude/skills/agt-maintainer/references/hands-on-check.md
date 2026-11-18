# Hands-on check of an app fix on the person's Mac

Use this when a fix to the macOS app's GUI (the menu bar icon, the popover, a window) has to be seen on the person's Mac and you can't see the screen. Computer use may be unavailable, so the check is the person's click plus the app's log.

The org Mac blocks apps that were copied or downloaded, so the build under test is a release build made on this Mac by `scripts/build_app.sh`. That is the one exception to "every build you make is `Agents Router Test.app`" (SKILL.md §3). The release build replaces the person's copy and runs on their real folders (`~/.agents-router`, `~/.agt`, `~/Library/Application Support/Agents Router`), so ask before installing it.

## 1. Make the app log what the person sees

Before asking for a click, make the app log the outcome the person sees: whether the window is on screen, its frame and its screen. A "shown" call line proves only that the call ran. Twice the log said "shown" and the person saw nothing.

The popover already logs it:
- `popover visibility stage=+100ms … window-server-onscreen= in-window-list= screen= frame=`: the window server's view, 100 ms after the show.
- `popover placed … click-source= screen= frame=`: where the popover went, and which click opened it.

For another window, add a line like these (through `app::trace`) in the same change, before the first click.

Both lines go through `app::trace`, which writes to stderr only. They are not in the diagnostic log file (`macos/README.md`, "Diagnostic log"); read them from the launch log of step 3.

## 2. Build and install

Ask the person first, and get their OK to replace their copy. Then run this as one block from the repository root. It names every file in full: a bare zsh glob that matches nothing exits 1 with "no matches found", even after the install succeeded. The build takes minutes; run the block in the background with its output in a log, and read the end.

```sh
set -eu
APP="/Applications/Agents Router.app"
BIN="$APP/Contents/MacOS/agents-router"
V=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)
D="$PWD/dist/Agents-Router-$V-arm64.dmg"
M=$(mktemp -d)
OLD=$(shasum -a 256 "$BIN" 2>/dev/null | cut -d' ' -f1)
# Build keyless: a signing key in the environment makes it a signed release build.
env -u TAURI_SIGNING_PRIVATE_KEY -u TAURI_SIGNING_PRIVATE_KEY_PASSWORD scripts/build_app.sh
# Quit by bundle id, then wait until the exact executable is gone. Never kill -9.
if pgrep -f "$BIN" >/dev/null; then osascript -e 'quit app id "io.github.watcharagorn.agents-router"'; fi
n=0
while pgrep -f "$BIN" >/dev/null; do
  n=$((n + 1)); [ "$n" -lt 30 ] || { echo "still running after 30 s: stop and ask the person"; exit 1; }
  sleep 1
done
# The old copy goes to the Trash under a unique name.
if [ -e "$APP" ]; then mv "$APP" "$HOME/.Trash/Agents Router $(date +%Y%m%d-%H%M%S).app"; fi
hdiutil attach -readonly -nobrowse -mountpoint "$M" "$D"
ditto "$M/Agents Router.app" "$APP"
hdiutil detach "$M"
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist"
codesign --verify --strict --deep "$APP" && echo "codesign ok"
NEW=$(shasum -a 256 "$BIN" | cut -d' ' -f1)
echo "old $OLD"; echo "new $NEW"
[ "$OLD" != "$NEW" ] || { echo "binary unchanged: this is the old build"; exit 1; }
echo "binary changed"
```

Expected:
- `CFBundleIdentifier` is `io.github.watcharagorn.agents-router`.
- `codesign ok`.
- `binary changed`. The version can't tell two builds apart before a bump, so the binary's sha256 is the build's name in step 4.

To go back, quit the app and move the Trash copy back to `/Applications/Agents Router.app`.

## 3. Launch

```sh
if pgrep -f "Agents Router Test.app/Contents/MacOS/agents-[r]outer" >/dev/null; then
  osascript -e 'quit app id "io.github.watcharagorn.agents-router.test"'
fi
L="$TMPDIR/agt-app-$(date +%Y%m%d-%H%M%S).log"
open --env RUST_BACKTRACE=1 --stdout "$L" --stderr "$L" -a "/Applications/Agents Router.app"
echo "launch log: $L"
pgrep -fl "Agents Router.*/Contents/MacOS/agents-[r]outer"
```

- Keep one copy running. A running `Agents Router Test.app` means two icons, and the person may click the wrong one, so the block quits it first.
- Launch with `open`, never as a background shell child (`"$BIN" 2>"$L" &`): a shell child dies when the Claude Code process exits, and the person loses the app mid-check.
- The trace lines go to stderr, so they land in `$L`. Variables don't carry between tool calls: use the printed path in step 4.
- `open -a` against a copy that still runs only brings it forward: `--env` and the log are ignored, and `$L` stays empty. Step 2's wait prevents that. Never `open -n`: it starts a second copy.
- `pgrep -f` matches whole command lines. A bare `"Agents Router"` also matches agt's runtimes under `Application Support/Agents Router`, and the shell that runs the command; the `[r]` keeps the pattern from matching itself.

Expected: `pgrep -fl` shows one process, `/Applications/Agents Router.app/Contents/MacOS/agents-router`.

## 4. Ask for the click

- Ask only once the build is installed and launched. Never ask for a click while a build runs.
- Name which icon to click: the Agents Router icon in the menu bar, on which display.
- One action per request. In each request, name the build (path and sha), the action and the expected result, and ask "did you see it?". For example: "`/Applications/Agents Router.app`, sha256 `3f9a…`. Click the Agents Router icon in the menu bar of the built-in display. Expected: the popover opens under the icon. Did you see it?"
- A "yes" covers only the action you named.

Then read the outcome lines and convert the `agt-app <epoch>` stamps (they carry milliseconds, which `date -r` doesn't take):

```sh
L="<launch log from step 3>"
grep -E 'popover (placed|visibility)' "$L" | while read -r _ t rest; do echo "$(date -r "${t%.*}" +%T) $rest"; done
```

Expected: the time of the click, `window-server-onscreen=true in-window-list=true`, and the `screen=` of the display the person clicked on. When the log and the person disagree, the log is checking the wrong thing: fix the log line before the next round.

## 5. Record the display layout

Each round, record the display layout: which displays are connected, which one the person clicked on, and the `screen=` the log printed. In the ship request (the PR body), list the layouts not covered, for example "not checked: external display only, lid closed".

## 6. The close paths that remain

When the fix removes a hide or close path, the remaining close paths go on the click list, each as its own request with its expected result:
- a click outside the popover;
- Cmd-Tab to another app;
- a second click on the icon.

Any not checked is named in the ship request as a known risk.

## Known traps

- **An Accessory (menu bar) app is never the active app.** A hide-on-deactivate or a resign-key auto-hide on its panel hides it at once: the log says "shown" and the person sees nothing. The visibility line prints `hides-on-deactivate=` and `app-active=`.
- **On macOS 26, Control Center draws the status item on every display.** The app's own `button.window()` may be the copy on another display, so its frame isn't necessarily the icon the person clicked. Compare `screen=` with the display they named.
