# macOS app

Agents Router.app shows account usage and availability in the menu bar. Right-click its icon and choose **Settings…** to change settings, roles, accounts and sharing, or read Activity.

The app needs **macOS 14 or newer on Apple Silicon**. It bundles Python and agt. There is no Intel build: macOS tells an Intel Mac that the app can't be opened. Use the [CLI install](../README.md#install) on an Intel Mac.

## Download and install

1. Open the [fizzory/agents-router release](https://github.com/fizzory/agents-router/releases/latest).
2. Download **`Agents-Router-<version>-arm64.dmg`**, for example `Agents-Router-0.17.0-arm64.dmg`.
3. Open the DMG. Drag **Agents Router.app** to **Applications**.
4. Open the copy in Applications. Approve it as described below.

If you open the app from the DMG or Downloads, its sheet offers **Move to Applications**. It copies to `/Applications`, or to `~/Applications` if `/Applications` is not writable, reopens that copy and quits the original. It refuses to replace an app already at the destination. Move that copy aside first if you need to use this action.

On first launch, the app installs its command line tool. If `~/.local/bin/agt` already belongs to another install, it offers **Keep** or **Replace**. Replace saves the previous entry; **Restore previous agt** in Settings → General puts it back while its target still exists. **Set up**, when offered, opens Terminal for agt setup. Browser logins and reloading CLI sessions remain yours to do.

## Approve the unsigned app

The app has an ad hoc signature, without an Apple Developer ID or notarization. macOS blocks a browser-downloaded copy until you approve it. Approve the copy from the release linked above.

### macOS 14 Sonoma

1. In Finder, open Applications and locate **Agents Router.app**.
2. Hold Control and click the app. Choose **Open** from the shortcut menu.
3. Click **Open** in the warning.

macOS saves an exception for that copy. Later launches use a double-click. These are Apple's [Sonoma instructions](https://support.apple.com/en-gb/guide/mac-help/mh40616/14.0/mac/14.0).

### macOS 15 Sequoia or newer

1. Try opening **Agents Router.app** from Applications, then dismiss the warning.
2. Choose Apple menu → **System Settings** → **Privacy & Security**.
3. Scroll to **Security** and choose **Open Anyway** for Agents Router. On macOS 15, choose **Open** first if it appears before **Open Anyway**.
4. Enter your login password and click **OK** if asked. Confirm **Open** if a further warning appears.

The approval button is available for about an hour after the blocked launch. If it has gone, try opening the app again. Control-click no longer overrides this check on macOS 15 or newer.

Checked against Apple's [Sequoia guide](https://support.apple.com/en-gb/guide/mac-help/mchleab3a043/15.0/mac/15.0), [runtime protection change](https://developer.apple.com/news/?id=saqachfa) and [current app-opening instructions](https://support.apple.com/en-us/102445). The Sonoma and Sequoia steps above were checked against those documents, not a live run on each OS.

## Use agt in a terminal

The app installs these paths by default:

| Path | Purpose |
|---|---|
| `~/.local/bin/agt` | Public link for terminals |
| `~/Library/Application Support/Agents Router/bin/agt` | Stable launcher used by the app |
| `~/Library/Application Support/Agents Router/current` | Link to the current runtime |
| `~/Library/Application Support/Agents Router/runtimes/<version>/` | Bundled Python, agt and plugin |

The app uses its stable launcher directly. Your terminal finds `agt` through `PATH`. If `agt` is not found, put this line in `~/.zshrc` for the default macOS shell, then open a new terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

You can also call `~/.local/bin/agt` directly. `command -v agt` shows which copy a terminal finds. `agt doctor --verbose` reports install ownership, versions and competing copies on PATH. If you chose Keep for an existing CLI, the app still uses its own runtime; your terminal keeps the existing entry.

`AGT_APP_SUPPORT` and `AGT_BIN_DIR` override the support and public-link folders. `AGT_HOME` overrides `~/.agents-router`. See [configuration](configuration.md) for settings files and other overrides.

## Updates

Choose **Check for Updates…** from the icon's right-click menu, the popover's **⋯** menu, or Settings → General. The app also checks at launch and every 24 hours. An automatic check leaves **Update to <version>** in the menu; a manual check opens a sheet.

Choose **Update to <version>** to download and install it. The app verifies the update signature and restarts. On first launch of the new version, it refreshes the CLI runtime and installed plugins. A failed download or install offers **Retry**. A key or signature failure offers **Open Releases Page**.

The in-app updater installs the archive without the browser download's quarantine flag, so it does not need the first-open approval again. If you download a replacement DMG in a browser and macOS blocks the new copy, repeat the approval steps above.

For an app-installed CLI, `agt update` refreshes the shipped plugin and stable launchers locally. Use the app's updater to update the app, Python and agt together.

## Open at login

Turn **Open at login** on or off in Settings → General. It creates or removes this LaunchAgent:

```text
~/Library/LaunchAgents/io.github.watcharagorn.agents-router.app.plist
```

**Turn it off before deleting the app.** It is separate from agt's sharing watcher.

## Uninstall

Deleting Agents Router.app alone leaves the installed CLI, runtimes, settings and job records behind. It also leaves the login LaunchAgent if you enabled it.

1. In Settings → General, turn **Open at login** off.
2. To remove agt from your CLIs too, follow steps 1–6 of the [setup guide's uninstall procedure](../src/agents_router/guides/setup.md#10-uninstall) while the CLI is still installed. These stop sharing and remove agt's rules, plugins, relay agents and hand-added MCP entries.
3. In General, choose **Uninstall command line tool…**, then **Uninstall**. The button stays disabled until no jobs block removal; the installer checks again before removing files.
4. Right-click the menu bar icon, choose **Quit**, then move Agents Router.app from Applications to the Trash.

The command line uninstaller removes the app's owned public link, stable launcher, current-runtime link, install record, runtimes and derived plugin copies. It restores a saved previous CLI when the app still owns the public link and the saved CLI is available. It keeps your settings and job records.

After quitting the app, these files can remain:

| Path | What remains; when to remove it |
|---|---|
| `~/Library/Application Support/Agents Router/` | Install lock, update completion records, Terminal handoffs and any saved previous CLI. Keep a needed backup; otherwise remove this folder after uninstalling the CLI |
| `~/.agents-router/` | Jobs, state, backups and agt-created account folders. Remove only if you no longer use agt; deleting it also removes those accounts' local logins and session history |
| `~/.agt/` | User settings. Remove if you no longer want them |
| `<workspace>/.agt/settings.toml` and `settings.local.toml` | Workspace and local settings. Remove from each workspace if no longer needed |

To remove a remaining folder, use Finder → **Go** → **Go to Folder…**, enter the path and move the folder to the Trash. Use your override paths if you set them. Keep your own provider folders (`~/.claude*`, `~/.codex`, `~/.gemini` and `~/.kiro`).

If you already deleted the app, install it again to turn off Open at login and use its command line uninstaller before removing the remaining files.
