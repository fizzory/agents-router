//! Long-lived native refresh/lifecycle and fixed-purpose popover commands.
use crate::{
    app_paths::{self, AppPaths},
    client::{AgtClient, ClientError, CommandOutcome, ReadReply, Request},
    install_app::{self, InstallController},
    models::{CommandKind, ConfigShow, Level, Snapshot, Usage},
    native,
    panes::RoleEdit,
    settings::{self, SettingsCore},
    terminal::{TerminalAction, TerminalLauncher},
    update_app,
    watcher::{SettingsWatcher, WatchPaths},
};
use objc2::MainThreadMarker;
use serde::Serialize;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, State,
};

pub const TRAY_ID: &str = "agt-tray";
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn log(message: &str) {
    eprintln!("agt-app {:.3} {message}", now());
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct PopoverSnapshot {
    pub usage: Option<Usage>,
    pub error: Option<String>,
    pub reading: bool,
    pub generation: u64,
    pub fetched_at: Option<f64>,
    pub update_version: Option<String>,
}
/// The stable launcher, or in debug and self-test builds the golden fixture CLI.
fn launcher(paths: &AppPaths) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if std::env::args().any(|a| a == "--self-test-wake")
        && std::env::var_os("AGT_APP_TEST_LAUNCHER").is_none()
    {
        return Err("self-test requires the isolated debug fixture launcher".into());
    }
    #[cfg(any(debug_assertions, feature = "ci-selftest"))]
    if let Some(fake) = std::env::var_os("AGT_APP_TEST_LAUNCHER") {
        let fake = PathBuf::from(fake);
        if !fake.is_absolute() || paths.agt_home.source == app_paths::Source::Default {
            return Err(
                "debug launcher requires an absolute path and explicit isolated AGT_HOME".into(),
            );
        }
        log("debug fixture launcher override active");
        return Ok(fake);
    }
    Ok(paths.launcher())
}
pub struct AppState {
    client: AgtClient,
    terminal: TerminalLauncher,
    snapshot: Mutex<PopoverSnapshot>,
    watcher: Mutex<Option<SettingsWatcher>>,
    quitting: AtomicBool,
    pub settings_target: Mutex<serde_json::Value>,
}

fn usage_error(error: ClientError) -> String {
    match error {
        ClientError::Execution(message) if message.contains("os error 2") => "agt not found".into(),
        ClientError::Exit { stderr, .. } if !stderr.is_empty() => {
            stderr.lines().next().unwrap_or("Can't read agt").into()
        }
        ClientError::TimedOut => "Can't read agt: timed out".into(),
        error => format!("Can't read agt: {error}"),
    }
}

pub(crate) fn main_thread(
    app: &tauri::AppHandle,
    f: impl FnOnce(tauri::AppHandle, MainThreadMarker) + Send + 'static,
) {
    let handle = app.clone();
    if let Err(error) = app.run_on_main_thread(move || {
        f(
            handle,
            MainThreadMarker::new().expect("native dispatch on main thread"),
        )
    }) {
        log(&format!("main-thread dispatch failed: {error}"));
    }
}
fn publish_status(app: &tauri::AppHandle, snapshot: PopoverSnapshot, fresh: bool) {
    // The status event can carry a retained old snapshot. Only usage-updated animates data.
    let _ = app.emit_to(
        "popover",
        if fresh {
            "usage-updated"
        } else {
            "popover-status"
        },
        &snapshot,
    );
    let _ = app.emit_to(
        "settings",
        if fresh {
            "usage-updated"
        } else {
            "settings-status"
        },
        &snapshot,
    );
    main_thread(app, move |app, mtm| {
        // Another read can finish between publication and main-thread dispatch.
        let latest = app
            .state::<AppState>()
            .snapshot
            .lock()
            .expect("snapshot")
            .clone();
        native::update_tray(&app, mtm, &latest)
    });
}
pub fn request_usage(app: &tauri::AppHandle, refresh: bool, reason: &'static str) {
    let state = app.state::<AppState>();
    if state.quitting.load(Ordering::Relaxed) {
        return;
    }
    let snapshot = {
        let mut s = state.snapshot.lock().expect("snapshot");
        s.reading = true;
        s.clone()
    };
    publish_status(app, snapshot, false);
    let client = state.client.clone();
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        log(&format!("usage read reason={reason} refresh={refresh}"));
        let mut request = Request::new(CommandKind::Usage, None);
        request.refresh = refresh;
        let reply = match client.read(request).await {
            Ok(rx) => match rx.await {
                Ok(reply) => reply,
                Err(error) => {
                    log(&format!("usage receiver failed: {error}"));
                    return;
                }
            },
            Err(error) => {
                log(&format!("usage request rejected: {error}"));
                return;
            }
        };
        if let ReadReply::Fresh { generation, result } = reply {
            let state = app.state::<AppState>();
            let (snapshot, fresh) = {
                let mut s = state.snapshot.lock().expect("snapshot");
                // A coalesced batch has several waiters but is published only once.
                if generation <= s.generation {
                    return;
                }
                s.generation = generation;
                s.reading = false;
                let fresh = match result {
                    Ok(CommandOutcome::Data(data)) => {
                        if let Snapshot::Usage(usage) = *data {
                            s.usage = Some(usage);
                            s.error = None;
                            s.fetched_at = Some(now());
                            true
                        } else {
                            s.error = Some("Can't read agt: unexpected response".into());
                            false
                        }
                    }
                    Err(error) => {
                        s.error = Some(usage_error(error));
                        false
                    }
                    _ => {
                        s.error = Some("Can't read agt: request refused".into());
                        false
                    }
                };
                (s.clone(), fresh)
            };
            publish_status(&app, snapshot, fresh);
            log(&format!(
                "usage result generation={generation} fresh={fresh}"
            ));
        }
    });
}

#[tauri::command]
fn get_snapshot(state: State<'_, AppState>) -> PopoverSnapshot {
    state.snapshot.lock().expect("snapshot").clone()
}
#[tauri::command]
fn refresh_usage(app: tauri::AppHandle) {
    request_usage(&app, true, "manual");
}
#[tauri::command]
fn hide_popover(app: tauri::AppHandle) {
    main_thread(&app, |app, mtm| native::hide(&app, mtm, "Esc"));
}
#[tauri::command]
fn set_popover_height(
    app: tauri::AppHandle,
    height: f64,
    duration_ms: f64,
    curve: [f32; 4],
) -> Result<(), String> {
    if !height.is_finite()
        || !duration_ms.is_finite()
        || duration_ms < 0.0
        || !curve.iter().all(|v| v.is_finite())
        || !(0.0..=1.0).contains(&curve[0])
        || !(0.0..=1.0).contains(&curve[2])
    {
        return Err("Invalid popover size or motion values".into());
    }
    main_thread(&app, move |app, mtm| {
        native::resize(&app, mtm, height, duration_ms, curve)
    });
    Ok(())
}
#[tauri::command]
fn open_settings(
    app: tauri::AppHandle,
    runtime: Option<String>,
    account: Option<String>,
    install: Option<bool>,
) {
    let target = serde_json::json!({"pane": if account.is_some() { "accounts" } else { "general" }, "runtime": runtime, "account": account});
    main_thread(&app, move |app, mtm| {
        native::open_settings(&app, mtm, target)
    });
    if install == Some(true) {
        install_app::request_install(&app);
    }
}
#[tauri::command]
fn get_settings_target(state: State<'_, AppState>) -> serde_json::Value {
    state
        .settings_target
        .lock()
        .expect("settings target")
        .clone()
}

#[tauri::command]
fn show_setting_menu(window: tauri::WebviewWindow, key: String) -> Result<(), String> {
    if window.label() != "settings"
        || key.is_empty()
        || !key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._".contains(c))
    {
        return Err("Choose a settings row".into());
    }
    let item = MenuItem::with_id(
        &window,
        format!("setting-history:{key}"),
        "Show history",
        true,
        None::<&str>,
    )
    .map_err(|error| error.to_string())?;
    Menu::with_items(&window, &[&item])
        .and_then(|menu| window.popup_menu(&menu))
        .map_err(|error| error.to_string())
}
#[tauri::command]
async fn copy_text(app: tauri::AppHandle, text: String) -> Result<(), String> {
    if text.len() > 1_048_576 {
        return Err("Text is too large to copy".into());
    }
    let (tx, rx) = tokio::sync::oneshot::channel();
    main_thread(&app, move |_app, mtm| {
        let _ = tx.send(native::copy_text(mtm, &text));
    });
    rx.await.map_err(|e| e.to_string())?
}
fn pane_action_label(action: &str) -> Option<&'static str> {
    Some(match action {
        "move-front" => "Move to front",
        "move-up" => "Move up",
        "move-down" => "Move down",
        "login" => "Log in",
        "enable" => "Enable",
        "disable" => "Disable",
        "revert" => "Revert to old value",
        "copy-key" => "Copy key",
        "undo-sync" => "Undo",
        "open-file" => "Open settings.toml",
        _ => return None,
    })
}
#[tauri::command]
fn show_pane_menu(
    window: tauri::WebviewWindow,
    token: String,
    actions: Vec<String>,
) -> Result<(), String> {
    if window.label() != "settings"
        || token.is_empty()
        || token.len() > 20
        || !token.bytes().all(|c| c.is_ascii_digit())
        || actions.is_empty()
        || actions.len() > 10
    {
        return Err("Choose a pane row".into());
    }
    let menu = Menu::new(&window).map_err(|e| e.to_string())?;
    for action in actions {
        let label = pane_action_label(&action).ok_or("Unknown pane action")?;
        let item = MenuItem::with_id(
            &window,
            format!("pane-action:{token}:{action}"),
            label,
            true,
            None::<&str>,
        )
        .map_err(|e| e.to_string())?;
        menu.append(&item).map_err(|e| e.to_string())?;
    }
    window.popup_menu(&menu).map_err(|e| e.to_string())
}
#[tauri::command]
async fn activity_undo_sync(
    state: State<'_, SettingsCore>,
    entry: String,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .activity_undo_sync(entry)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn config_show(
    state: State<'_, SettingsCore>,
    workspace: Option<PathBuf>,
) -> Result<ConfigShow, String> {
    state.show(workspace).await.map_err(|e| e.to_string())
}
#[tauri::command]
async fn config_set(
    state: State<'_, SettingsCore>,
    key: String,
    value: String,
    level: Level,
    workspace: Option<PathBuf>,
    revision: String,
    user_said: Option<String>,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .write(key, Some(value), level, workspace, revision, user_said)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn config_unset(
    state: State<'_, SettingsCore>,
    key: String,
    level: Level,
    workspace: Option<PathBuf>,
    revision: String,
    user_said: Option<String>,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .write(key, None, level, workspace, revision, user_said)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn config_revert(
    state: State<'_, SettingsCore>,
    entry: String,
    workspace: Option<PathBuf>,
    user_said: Option<String>,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .revert(entry, workspace, user_said)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn roles_show(
    state: State<'_, SettingsCore>,
    workspace: Option<PathBuf>,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .roles_show(workspace)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn roles_config(
    state: State<'_, SettingsCore>,
    edit: RoleEdit,
    level: Level,
    workspace: Option<PathBuf>,
    user_said: Option<String>,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .roles_config(edit, level, workspace, user_said)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn accounts_list(state: State<'_, SettingsCore>) -> Result<serde_json::Value, String> {
    settings::outcome_json(state.accounts_list().await.map_err(|e| e.to_string())?)
}
#[tauri::command]
async fn accounts_check(
    app: tauri::AppHandle,
    state: State<'_, SettingsCore>,
    runtime: String,
    account: String,
) -> Result<serde_json::Value, String> {
    let result = state
        .accounts_check(runtime, account)
        .await
        .map_err(|e| e.to_string());
    request_usage(&app, false, "settings-account-check");
    settings::outcome_json(result?)
}
#[tauri::command]
async fn accounts_add_check(
    state: State<'_, SettingsCore>,
    runtime: String,
    account: String,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .accounts_add_check(runtime, account)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn accounts_toggle(
    app: tauri::AppHandle,
    state: State<'_, SettingsCore>,
    runtime: String,
    account: String,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    let result = state
        .accounts_toggle(runtime, account, enabled)
        .await
        .map_err(|e| e.to_string());
    request_usage(&app, false, "settings-account-toggle");
    settings::outcome_json(result?)
}
pub(crate) async fn terminal_handoff(
    state: &AppState,
    action: TerminalAction,
) -> Result<(), String> {
    let handoff = state.terminal.prepare(&action).map_err(|e| e.to_string())?;
    state
        .terminal
        .open(&handoff)
        .await
        .map_err(|e| format!("{e}. Copy: {}", handoff.copy_command))
}
#[tauri::command]
async fn accounts_login(
    state: State<'_, AppState>,
    settings: State<'_, SettingsCore>,
    runtime: String,
    account: String,
) -> Result<(), String> {
    terminal_handoff(
        &state,
        settings
            .account_action(&runtime, &account, false)
            .map_err(|e| e.to_string())?,
    )
    .await
}
#[tauri::command]
async fn setup_roles(state: State<'_, AppState>) -> Result<(), String> {
    terminal_handoff(&state, TerminalAction::Setup).await
}
#[tauri::command]
async fn accounts_add(
    state: State<'_, AppState>,
    settings: State<'_, SettingsCore>,
    runtime: String,
    account: String,
) -> Result<(), String> {
    terminal_handoff(
        &state,
        settings
            .add_account_action(runtime, account)
            .await
            .map_err(|e| e.to_string())?,
    )
    .await
}
#[tauri::command]
async fn share_list(state: State<'_, SettingsCore>) -> Result<serde_json::Value, String> {
    settings::outcome_json(state.share_list().await.map_err(|e| e.to_string())?)
}
#[tauri::command]
async fn share_status(state: State<'_, SettingsCore>) -> Result<serde_json::Value, String> {
    settings::outcome_json(state.share_status().await.map_err(|e| e.to_string())?)
}
#[tauri::command]
async fn share_kind(
    state: State<'_, SettingsCore>,
    kind: String,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .share_kind(kind, enabled)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn share_pick(
    state: State<'_, SettingsCore>,
    item: String,
    label: String,
    included: bool,
) -> Result<serde_json::Value, String> {
    settings::outcome_json(
        state
            .share_pick(item, label, included)
            .await
            .map_err(|e| e.to_string())?,
    )
}
#[tauri::command]
async fn activity_log(state: State<'_, SettingsCore>) -> Result<serde_json::Value, String> {
    settings::outcome_json(state.activity_log().await.map_err(|e| e.to_string())?)
}
#[tauri::command]
async fn open_settings_file(
    app: tauri::AppHandle,
    state: State<'_, SettingsCore>,
    file: String,
    line: u64,
) -> Result<(), String> {
    let path = state.checked_file(&file, line)?;
    let (tx, rx) = tokio::sync::oneshot::channel();
    main_thread(&app, move |_app, mtm| {
        native::open_settings_file(mtm, path, line, tx)
    });
    rx.await.map_err(|e| e.to_string())?
}
#[tauri::command]
async fn choose_workspace(app: tauri::AppHandle) -> Result<Option<PathBuf>, String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = tokio::sync::oneshot::channel();
    let mut dialog = app.dialog().file();
    if let Some(window) = app.get_webview_window("settings") {
        dialog = dialog.set_parent(&window);
    }
    dialog.pick_folder(move |folder| {
        let _ = tx.send(folder);
    });
    rx.await
        .map_err(|e| e.to_string())?
        .map(|folder| {
            folder
                .into_path()
                .map_err(|e| e.to_string())?
                .canonicalize()
                .map_err(|e| e.to_string())
        })
        .transpose()
}
/// Settings → General's Test build rows; `null` in the release.
#[tauri::command]
fn test_build_paths(paths: State<'_, AppPaths>) -> Option<Vec<app_paths::Row>> {
    paths.rows()
}
#[tauri::command]
fn get_open_at_login(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}
#[tauri::command]
fn set_open_at_login(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    if enabled {
        manager.enable()
    } else {
        manager.disable()
    }
    .map_err(|e| e.to_string())?;
    manager.is_enabled().map_err(|e| e.to_string())
}
fn known_account(state: &AppState, runtime: &str, account: &str) -> Result<(), String> {
    if account.starts_with('-') || !matches!(runtime, "claude" | "codex" | "kiro" | "antigravity") {
        return Err("Unknown account".into());
    }
    state
        .snapshot
        .lock()
        .expect("snapshot")
        .usage
        .as_ref()
        .and_then(|u| u.providers.iter().find(|p| p.runtime == runtime))
        .filter(|p| p.accounts.iter().any(|a| a.name == account))
        .map(|_| ())
        .ok_or_else(|| "Account is no longer in the snapshot".into())
}
#[tauri::command]
async fn login_account(
    state: State<'_, AppState>,
    runtime: String,
    account: String,
) -> Result<(), String> {
    known_account(&state, &runtime, &account)?;
    let handoff = state
        .terminal
        .prepare(&TerminalAction::Login { account, runtime })
        .map_err(|e| e.to_string())?;
    state
        .terminal
        .open(&handoff)
        .await
        .map_err(|e| format!("{e}. Copy: {}", handoff.copy_command))
}
#[tauri::command]
async fn check_account(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    runtime: String,
    account: String,
) -> Result<serde_json::Value, String> {
    known_account(&state, &runtime, &account)?;
    let mut request = Request::new(CommandKind::AccountsCheck, None);
    request.arguments = vec![account, "--runtime".into(), runtime];
    let reply = state
        .client
        .read(request)
        .await
        .map_err(|e| e.to_string())?
        .await
        .map_err(|e| e.to_string())?;
    let result = match reply {
        ReadReply::Fresh {
            result: Ok(CommandOutcome::Data(data)),
            ..
        } => serde_json::to_value(data).map_err(|e| e.to_string()),
        ReadReply::Fresh {
            result: Err(error), ..
        } => Err(error.to_string()),
        ReadReply::Superseded { .. } => Err("A newer account check is running".into()),
        _ => Err("Account check refused".into()),
    };
    request_usage(&app, false, "account-check");
    result
}
#[tauri::command]
fn check_for_updates(app: tauri::AppHandle) {
    update_requested(&app);
}
fn update_requested(app: &tauri::AppHandle) {
    update_app::request(app, true);
}
#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    quit(&app);
}
pub(crate) fn quit(app: &tauri::AppHandle) {
    exit(app, 0);
}
pub(crate) fn exit(app: &tauri::AppHandle, code: i32) {
    app.state::<AppState>()
        .quitting
        .store(true, Ordering::Relaxed);
    app.exit(code);
}

fn tray_menu(app: &tauri::AppHandle, update: Option<&str>) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    if let Some(version) = update {
        menu.append(&MenuItem::with_id(
            app,
            "install-update",
            format!("Update to {version}"),
            true,
            None::<&str>,
        )?)?;
        menu.append(&PredefinedMenuItem::separator(app)?)?;
    }
    menu.append(&MenuItem::with_id(
        app,
        "settings",
        "Settings…",
        true,
        None::<&str>,
    )?)?;
    menu.append(&MenuItem::with_id(
        app,
        "check-updates",
        "Check for Updates…",
        true,
        None::<&str>,
    )?)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?)?;
    Ok(menu)
}

pub(crate) fn settings_menu(app: &tauri::AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    Menu::with_items(
        app,
        &[
            &Submenu::with_items(
                app,
                "Agents Router",
                true,
                &[
                    &PredefinedMenuItem::about(app, Some("About Agents Router"), None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::hide(app, None)?,
                    &PredefinedMenuItem::hide_others(app, None)?,
                    &PredefinedMenuItem::show_all(app, None)?,
                    &PredefinedMenuItem::separator(app)?,
                    &MenuItem::with_id(
                        app,
                        "quit",
                        "Quit Agents Router",
                        true,
                        Some("CmdOrCtrl+Q"),
                    )?,
                ],
            )?,
            &Submenu::with_items(
                app,
                "Edit",
                true,
                &[
                    &MenuItem::with_id(app, "settings-undo", "Undo", true, Some("CmdOrCtrl+Z"))?,
                    &MenuItem::with_id(
                        app,
                        "settings-redo",
                        "Redo",
                        true,
                        Some("CmdOrCtrl+Shift+Z"),
                    )?,
                    &PredefinedMenuItem::separator(app)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?,
            &Submenu::with_items(
                app,
                "Window",
                true,
                &[
                    &PredefinedMenuItem::minimize(app, None)?,
                    &PredefinedMenuItem::close_window(app, None)?,
                ],
            )?,
        ],
    )
}
/// Publish the waiting update in both slots without taking focus.
pub fn set_pending_update(app: &tauri::AppHandle, version: Option<String>) {
    let snapshot = {
        let state = app.state::<AppState>();
        let mut s = state.snapshot.lock().expect("snapshot");
        s.update_version = version.clone();
        s.clone()
    };
    publish_status(app, snapshot, false);
    main_thread(app, move |app, _mtm| {
        if let (Some(tray), Ok(menu)) =
            (app.tray_by_id(TRAY_ID), tray_menu(&app, version.as_deref()))
        {
            let _ = tray.set_menu(Some(menu));
        }
    });
}

/// Startup refusals (a release given --test-* flags, an unsafe test folder) end
/// the launch with a message instead of a crash report.
fn refuse(message: String) -> ! {
    eprintln!("agt-app {:.3} refused: {message}", now());
    std::process::exit(2);
}
/// Restart after an update. A test build carries its chosen folders as flags,
/// including ones that came from environment variables LaunchServices drops.
pub fn restart(app: &tauri::AppHandle) -> ! {
    let relaunch = app.state::<AppPaths>().relaunch_args();
    if relaunch.is_empty() {
        app.restart();
    }
    let mut env = app.env();
    let args = env.args_os.split_off(env.args_os.len().min(1));
    env.args_os
        .extend(app_paths::relaunch_argv(&args, &relaunch));
    log(&format!(
        "restarting with {} test-build arguments",
        relaunch.len()
    ));
    // Like Tauri's own restart: clean up and relaunch on the main thread only.
    if MainThreadMarker::new().is_some() {
        app.cleanup_before_exit();
        tauri::process::restart(&env);
    }
    let handle = app.clone();
    if let Err(error) = app.run_on_main_thread(move || {
        handle.cleanup_before_exit();
        tauri::process::restart(&env)
    }) {
        log(&format!("main-thread restart failed: {error}"));
        app.restart();
    }
    loop {
        std::thread::sleep(Duration::MAX);
    }
}

pub fn run() {
    // The identifier embedded in the bundle decides release or test build;
    // resolve every folder from it before any filesystem activity.
    let context = tauri::generate_context!();
    let identifier = context.config().identifier.clone();
    let paths = AppPaths::from_process(&identifier).unwrap_or_else(|e| refuse(e));
    if paths.test {
        paths.prepare().unwrap_or_else(|e| refuse(e));
    }
    let launcher = launcher(&paths).unwrap_or_else(|e| refuse(e));
    let client = AgtClient::with_env(
        launcher.clone(),
        paths.child_env(),
        paths.test.then(|| paths.child_home()),
    )
    .expect("absolute launcher");
    let settings = SettingsCore::new(client.clone(), paths.child_home());
    let terminal =
        TerminalLauncher::new(launcher, paths.handoffs()).expect("absolute handoff path");
    let terminal = if paths.test {
        terminal
            .starting_in(paths.child_home())
            .expect("absolute private HOME")
    } else {
        terminal
    };
    let state = AppState {
        client,
        terminal,
        snapshot: Mutex::new(PopoverSnapshot {
            reading: true,
            ..Default::default()
        }),
        watcher: Mutex::new(None),
        quitting: AtomicBool::new(false),
        settings_target: Mutex::new(serde_json::json!({"pane":"general"})),
    };
    // The login item follows the bundle identifier, so a test build never replaces
    // the release's, and it reopens a test build with its chosen folders.
    let autostart = settings::autostart_plugin(&identifier, &paths.relaunch_args());
    let installer = InstallController::new(&paths).unwrap_or_else(|e| refuse(e));
    let watch = WatchPaths {
        state: paths.agt_home.path.join("state"),
        jobs: paths.agt_home.path.join("jobs"),
        settings: vec![paths.settings_file()],
    };
    let builder = tauri::Builder::default();
    #[cfg(feature = "ci-selftest")]
    let builder = crate::selftest::instrument(builder);
    let app = builder
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            main_thread(app, |app, mtm| {
                log("single-instance callback on main thread");
                native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}));
            });
        }))
        .plugin(tauri_nspanel::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .manage(update_app::UpdateController::default())
        .plugin(autostart)
        .manage(state)
        .manage(settings)
        .manage(installer)
        .manage(paths)
        .invoke_handler(tauri::generate_handler![
            get_snapshot,
            install_app::get_install_status,
            install_app::install_action,
            refresh_usage,
            hide_popover,
            set_popover_height,
            open_settings,
            get_settings_target,
            show_setting_menu,
            show_pane_menu,
            copy_text,
            setup_roles,
            activity_undo_sync,
            config_show,
            config_set,
            config_unset,
            config_revert,
            roles_show,
            roles_config,
            accounts_list,
            accounts_check,
            accounts_add_check,
            accounts_toggle,
            accounts_login,
            accounts_add,
            share_list,
            share_status,
            share_kind,
            share_pick,
            activity_log,
            open_settings_file,
            choose_workspace,
            get_open_at_login,
            set_open_at_login,
            login_account,
            check_account,
            check_for_updates,
            update_app::get_update_status,
            update_app::update_action,
            test_build_paths,
            quit_app
        ])
        .on_menu_event(|app, event| match event.id.as_ref() {
            id if id.starts_with("pane-action:") => {
                if let Some((token, action)) = id["pane-action:".len()..].split_once(':') {
                    if pane_action_label(action).is_some() {
                        let _ = app.emit_to(
                            "settings",
                            "pane-menu",
                            serde_json::json!({"token":token,"action":action}),
                        );
                    }
                }
            }
            id if id.starts_with("setting-history:") => {
                let key = &id["setting-history:".len()..];
                let _ = app.emit_to(
                    "settings",
                    "settings-history",
                    serde_json::json!({"key":key}),
                );
            }
            "settings-undo" | "settings-redo" => {
                if app
                    .get_webview_window("settings")
                    .is_some_and(|w| w.is_visible().unwrap_or(false))
                {
                    let action = if event.id.as_ref() == "settings-undo" {
                        "undo"
                    } else {
                        "redo"
                    };
                    let _ = app.emit_to(
                        "settings",
                        "settings-edit",
                        serde_json::json!({"action":action}),
                    );
                }
            }
            "quit" => quit(app),
            _ => (),
        })
        .on_window_event(|window, event| {
            let app = window.app_handle();
            match event {
                tauri::WindowEvent::Focused(false) if window.label() == "popover" => {
                    main_thread(app, |app, mtm| native::hide(&app, mtm, "blur"))
                }
                tauri::WindowEvent::Focused(true) if window.label() == "settings" => {
                    request_usage(app, true, "reactivation")
                }
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    if window.label() == "settings" && app.state::<InstallController>().pending() {
                        main_thread(app, |app, mtm| {
                            native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}))
                        });
                        return;
                    }
                    if window.label() == "popover" {
                        main_thread(app, |app, mtm| native::hide(&app, mtm, "close-request"));
                    } else {
                        let _ = window.hide();
                        main_thread(app, |app, _mtm| {
                            let _ = app.remove_menu();
                            let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                            log("settings hidden activation=Accessory; webview retained");
                        });
                    }
                }
                _ => (),
            }
        })
        .setup(move |app| {
            let mtm = MainThreadMarker::new().expect("setup on main thread");
            // Here, not before the builder: a second launch exits in the
            // single-instance plugin first, so only the running app logs it.
            if app.state::<AppPaths>().test {
                log(&app.state::<AppPaths>().describe());
            }
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            native::create_panel(app, mtm)?;
            TrayIconBuilder::with_id(TRAY_ID)
                .menu(&tray_menu(app.handle(), None)?)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "settings" => main_thread(app, |app, mtm| {
                        native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}))
                    }),
                    "check-updates" | "install-update" => update_requested(app),
                    "quit" => quit(app),
                    _ => (),
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        position,
                        rect,
                        ..
                    } = event
                    {
                        // Tauri queues tray events, so the pointer read here may
                        // already be on another display. tray-icon read it in its
                        // own mouseUp handler, before the queue: use that reading.
                        let click = native::tray_event(position, rect);
                        main_thread(tray.app_handle(), move |app, mtm| {
                            native::toggle(&app, mtm, click)
                        });
                    }
                })
                .build(app)?;
            native::update_tray(
                app.handle(),
                mtm,
                &app.state::<AppState>().snapshot.lock().expect("snapshot"),
            );
            let handle = app.handle().clone();
            let watcher =
                SettingsWatcher::start(watch, move || request_usage(&handle, false, "debounced"))?;
            if app.state::<AppPaths>().test {
                // Rewrite an enabled login item so it reopens with this launch's folders.
                use tauri_plugin_autostart::ManagerExt;
                if app.autolaunch().is_enabled().unwrap_or(false) {
                    if let Err(error) = app.autolaunch().enable() {
                        log(&format!("login item refresh failed: {error}"));
                    }
                }
            }
            native::observe_wake(mtm, watcher.wake_trigger());
            *app.state::<AppState>().watcher.lock().expect("watcher") = Some(watcher);
            request_usage(app.handle(), true, "startup");
            install_app::start(app.handle());
            update_app::start(app.handle());
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut timer = tokio::time::interval_at(
                    tokio::time::Instant::now() + Duration::from_secs(60),
                    Duration::from_secs(60),
                );
                timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                loop {
                    timer.tick().await;
                    request_usage(&handle, true, "timer");
                }
            });
            #[cfg(debug_assertions)]
            {
                if let Ok(version) = std::env::var("AGT_APP_TEST_UPDATE_VERSION") {
                    set_pending_update(app.handle(), Some(version));
                }
                debug_self_test(app.handle());
            }
            #[cfg(feature = "ci-selftest")]
            crate::selftest::start(app.handle());
            log("ready activation=Accessory left-click=popover right-click=native-menu");
            Ok(())
        })
        .build(context)
        .expect("could not build Agents Router");
    app.run(|app, event| match event {
        tauri::RunEvent::ExitRequested { api, code, .. }
            if !app.state::<AppState>().quitting.load(Ordering::Relaxed)
                && code != Some(tauri::RESTART_EXIT_CODE) =>
        {
            api.prevent_exit();
            log("prevent_exit applied: tray app remains running");
        }
        tauri::RunEvent::Resumed => {
            request_usage(app, true, "reactivation");
            update_app::request(app, false);
        }
        tauri::RunEvent::Exit => {
            if let Some(mtm) = MainThreadMarker::new() {
                native::stop_observing(mtm);
            }
        }
        _ => (),
    });
}

#[cfg(debug_assertions)]
fn debug_self_test(app: &tauri::AppHandle) {
    if !std::env::args().any(|a| a == "--self-test-wake") {
        return;
    }
    assert!(
        std::env::var_os("AGT_APP_TEST_LAUNCHER").is_some(),
        "self-test requires fake launcher"
    );
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_secs(2)).await;
        main_thread(&app, |_app, mtm| native::post_wake(mtm));
        tokio::time::sleep(Duration::from_secs(3)).await;
        main_thread(&app, |app, mtm| native::show(&app, mtm));
        tokio::time::sleep(Duration::from_millis(200)).await;
        // Let the first-show snap finish, then exercise a real DOM/frame resize.
        main_thread(&app, |app, _mtm| {
            if let Some(window) = app.get_webview_window("popover") {
                let _ = window.eval_with_callback(
                    "JSON.stringify({rows:document.querySelectorAll('#accounts .account-row').length,status:document.querySelector('#status').textContent,height:innerHeight,active:document.body.dataset.active})",
                    |value| log(&format!("self-test popover-content={value}")),
                );
            }
            set_pending_update(&app, None);
        });
        tokio::time::sleep(Duration::from_secs(1)).await;
        main_thread(&app, |app, _mtm| {
            use tauri_nspanel::ManagerExt;
            if let Ok(panel) = app.get_webview_panel("popover") {
                panel.resign_key_window();
            }
        });
        tokio::time::sleep(Duration::from_secs(1)).await;
        main_thread(&app, |app, mtm| {
            native::show(&app, mtm);
            if let Some(window) = app.get_webview_window("popover") {
                let _ = window.eval("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))");
            }
        });
        tokio::time::sleep(Duration::from_secs(2)).await;
        main_thread(&app, |app, mtm| {
            native::open_settings(
                &app,
                mtm,
                serde_json::json!({"pane":"accounts","account":"work","runtime":"claude"}),
            );
            let edit = app
                .menu()
                .expect("native menu")
                .items()
                .expect("menu items")
                .into_iter()
                .find_map(|item| {
                    item.as_submenu()
                        .filter(|submenu| submenu.text().ok().as_deref() == Some("Edit"))
                        .cloned()
                })
                .expect("native Edit menu");
            assert!(edit.get("settings-undo").is_some());
            assert!(edit.get("settings-redo").is_some());
        });
        tokio::time::sleep(Duration::from_secs(1)).await;
        main_thread(&app, |app, _mtm| {
            if let Some(window) = app.get_webview_window("settings") {
                let _ = window.eval_with_callback(
                    "document.body.dataset.retained='yes'; JSON.stringify({pane:document.body.dataset.pane,rows:document.querySelectorAll('.settings-account').length})",
                    |value| log(&format!("self-test settings-content={value}")),
                );
                let _ = window.close();
            }
        });
        tokio::time::sleep(Duration::from_secs(1)).await;
        // Exercise the actual single-instance plugin callback with the same
        // isolated debug environment. This never starts agt or a provider CLI.
        let secondary =
            tokio::process::Command::new(std::env::current_exe().expect("app executable"))
                .arg("--self-test-secondary")
                .kill_on_drop(true)
                .status()
                .await;
        log(&format!(
            "self-test second-launch-success={}",
            secondary.is_ok_and(|s| s.success())
        ));
        tokio::time::sleep(Duration::from_secs(1)).await;
        main_thread(&app, |app, _mtm| {
            if let Some(window) = app.get_webview_window("settings") {
                log(&format!(
                    "self-test settings-reopened-visible={}",
                    window.is_visible().unwrap_or(false)
                ));
                let _ = window.eval_with_callback(
                    "JSON.stringify({pane:document.body.dataset.pane,retained:document.body.dataset.retained})",
                    |value| log(&format!("self-test settings-retained={value}")),
                );
            }
        });
        tokio::time::sleep(Duration::from_secs(1)).await;
        log("self-test complete; quitting");
        quit(&app);
    });
}
