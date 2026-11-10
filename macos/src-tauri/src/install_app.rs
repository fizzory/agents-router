//! Fixed-purpose first-launch and Settings actions around the installer.
use crate::{
    app::{self, AppState},
    install::{self, InstallPaths},
    native,
};
use serde::Serialize;
use std::{path::PathBuf, sync::Mutex, time::Duration};
use tauri::{Emitter, Manager, State};
#[derive(Clone, Default, Serialize)]
pub struct InstallStatus {
    pub dialog: Option<String>,
    pub message: Option<String>,
    pub version: Option<String>,
    pub foreign: Option<install::ForeignCli>,
    pub bundle_version: Option<String>,
    pub busy: bool,
    pub setup_unknown: bool,
    pub can_restore: bool,
}
pub struct InstallController {
    pub paths: InstallPaths,
    app: PathBuf,
    payload: PathBuf,
    status: Mutex<InstallStatus>,
    operation: tokio::sync::Mutex<()>,
    first: Mutex<bool>,
}
impl InstallController {
    #[cfg(debug_assertions)]
    pub async fn settled(&self) {
        let _guard = self.operation.lock().await;
    }
    pub fn new() -> Result<Self, String> {
        let paths = InstallPaths::discover()?;
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let app = exe
            .ancestors()
            .find(|p| p.extension().is_some_and(|s| s == "app"))
            .unwrap_or(&exe)
            .to_path_buf();
        let payload = app.join("Contents/Resources/runtime");
        Ok(Self {
            first: Mutex::new(install::first_launch_pending(&paths)),
            status: Mutex::new(InstallStatus {
                version: paths.version(),
                setup_unknown: install::setup_unknown(&paths),
                can_restore: install::owns_public(&paths)
                    && install::saved_foreign(&paths).is_some(),
                ..Default::default()
            }),
            operation: tokio::sync::Mutex::new(()),
            paths,
            app,
            payload,
        })
    }
    pub fn pending(&self) -> bool {
        self.status.lock().unwrap().dialog.is_some()
    }
    pub fn snapshot(&self) -> InstallStatus {
        self.status.lock().unwrap().clone()
    }
    fn publish(&self, app: &tauri::AppHandle, dialog: Option<&str>, message: Option<String>) {
        let snapshot = {
            let mut s = self.status.lock().unwrap();
            s.dialog = dialog.map(str::to_owned);
            s.message = message;
            s.busy = dialog == Some("progress");
            s.version = self.paths.version();
            s.bundle_version = install::manifest(&self.payload)
                .ok()
                .map(|m| m.runtime_version);
            s.setup_unknown = install::setup_unknown(&self.paths);
            s.can_restore =
                install::owns_public(&self.paths) && install::saved_foreign(&self.paths).is_some();
            s.clone()
        };
        let _ = app.emit_to("settings", "install-status", snapshot);
        if dialog.is_some() {
            app::main_thread(app, |app, mtm| {
                native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}))
            });
        }
    }
    fn finish(&self, app: &tauri::AppHandle) {
        self.publish(app, None, None);
        let first = {
            let mut first = self.first.lock().unwrap();
            let result = *first;
            *first = false;
            result
        };
        if first {
            let _ = install::finish_first_launch(&self.paths);
            app::main_thread(app, |app, mtm| {
                if let Some(w) = app.get_webview_window("settings") {
                    let _ = w.hide();
                }
                let _ = app.remove_menu();
                let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                native::show(&app, mtm);
            });
        }
    }
    async fn after_install(&self, app: &tauri::AppHandle) {
        if *self.first.lock().unwrap() {
            self.publish(app, Some("progress"), Some("Checking setup…".into()));
            match install::setup_status(&self.paths, Duration::from_secs(10)).await {
                Ok(true) => self.publish(app, Some("ready"), None),
                _ => self.finish(app),
            }
        } else {
            self.finish(app);
        }
    }
    async fn perform_install(&self, app: &tauri::AppHandle, downgrade: bool) {
        if !install::valid_location(&self.app, &self.paths) {
            self.publish(app, Some("move"), None);
            return;
        }
        self.publish(app, Some("progress"), Some("Installing agt…".into()));
        match install::install(&self.paths, &self.app, &self.payload, downgrade).await {
            Ok(result) => {
                if !result.first {
                    if let Err(e) = install::refresh_plugins(&self.paths).await {
                        self.publish(
                            app,
                            Some("error"),
                            Some(install::install_error(&e, false, None)),
                        );
                        return;
                    }
                }
                // Cleanup is conservative on an unidentified job or an unreadable usage report.
                if let Err(e) = install::cleanup(&self.paths).await {
                    app::log(&format!("runtime cleanup deferred: {e}"));
                }
                app::request_usage(app, false, "installed");
                if let Some(f) = result.foreign {
                    self.status.lock().unwrap().foreign = Some(f);
                    self.publish(app, Some("replace"), None);
                } else {
                    self.after_install(app).await;
                }
            }
            Err(e) if e == "Downgrade requires confirmation" => {
                self.publish(app, Some("downgrade"), None)
            }
            Err(e) => {
                let message = if e.starts_with("Couldn't install agt.")
                    || e.starts_with("Install didn't finish.")
                {
                    e
                } else {
                    install::install_error(&e, self.paths.current().is_none(), None)
                };
                self.publish(app, Some("error"), Some(message));
            }
        }
    }
}
pub fn request_install(app: &tauri::AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let controller = app.state::<InstallController>();
        if let Ok(_guard) = controller.operation.try_lock() {
            controller.perform_install(&app, false).await;
        };
    });
}
pub fn start(app: &tauri::AppHandle) {
    // Native smoke checks have a deliberately payload-free bundle and golden CLI.
    #[cfg(debug_assertions)]
    if std::env::var_os("AGT_APP_TEST_LAUNCHER").is_some() {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let c = app.state::<InstallController>();
        let _guard = c.operation.lock().await;
        let version = install::manifest(&c.payload)
            .ok()
            .map(|m| m.runtime_version);
        if !install::valid_location(&c.app, &c.paths)
            || version.is_none()
            || c.paths.version() != version
        {
            c.perform_install(&app, false).await;
        } else if *c.first.lock().unwrap() {
            c.after_install(&app).await;
        } else if let Err(e) = install::refresh_plugins(&c.paths).await {
            app::log(&format!("plugin refresh deferred: {e}"));
        }
    });
}
#[tauri::command]
pub fn get_install_status(state: State<'_, InstallController>) -> InstallStatus {
    state.snapshot()
}
#[tauri::command]
pub async fn install_action(
    app: tauri::AppHandle,
    state: State<'_, InstallController>,
    action: String,
) -> Result<(), String> {
    let _guard = state
        .operation
        .try_lock()
        .map_err(|_| "An install operation is already running")?;
    let s = state.snapshot();
    match (action.as_str(), s.dialog.as_deref()) {
        ("install" | "repair", None) | ("retry", Some("error")) => {
            state.perform_install(&app, false).await
        }
        ("downgrade", Some("downgrade")) => state.perform_install(&app, true).await,
        ("replace", Some("replace")) => {
            install::replace_foreign(&state.paths, s.foreign.as_ref().ok_or("No CLI to replace")?)?;
            state.after_install(&app).await;
        }
        ("keep", Some("replace")) => state.after_install(&app).await,
        ("setup", Some("ready")) => {
            app::terminal_handoff(
                &app.state::<AppState>(),
                crate::terminal::TerminalAction::Setup,
            )
            .await?;
            state.finish(&app);
        }
        ("later", Some("ready"))
        | ("close", Some("error"))
        | ("cancel", Some("downgrade" | "uninstall")) => state.finish(&app),
        ("check-setup", None) => {
            let _ = install::setup_status(&state.paths, Duration::from_secs(10)).await;
            state.publish(&app, None, None);
        }
        ("uninstall", None) => state.publish(&app, Some("uninstall"), None),
        ("confirm-uninstall", Some("uninstall")) => {
            install::uninstall(&state.paths).await?;
            state.finish(&app);
            app::request_usage(&app, false, "uninstalled");
        }
        ("restore", None) => {
            install::restore_cli(&state.paths)?;
            state.publish(&app, None, None);
        }
        ("move", Some("move")) => {
            let args = install::move_application(&state.app, &state.paths)?;
            let out = tokio::process::Command::new(&args[0])
                .args(&args[1..])
                .output()
                .await
                .map_err(|e| e.to_string())?;
            if !out.status.success() {
                return Err("Couldn't reopen Agents Router. Open it from Applications.".into());
            }
            app::quit(&app);
        }
        ("quit", Some("move")) => app::quit(&app),
        _ => return Err("This install action is no longer available. Check again.".into()),
    }
    Ok(())
}
