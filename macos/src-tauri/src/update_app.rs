//! Fixed-purpose updater controller. No updater URLs or keys cross IPC.
#[cfg(debug_assertions)]
use crate::install_app::InstallController;
use crate::{
    app, native,
    update::{self, CheckResult, Dialog, Schedule, Stage},
};
use serde::Serialize;
use std::{sync::Mutex, time::Duration};
use tauri::{Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};

#[cfg(debug_assertions)]
pub fn configure_self_test() {
    let args: Vec<_> = std::env::args_os().collect();
    let Some(index) = args.iter().position(|a| a == "--self-test-update") else {
        return;
    };
    let root = std::path::PathBuf::from(args.get(index + 1).expect("scratch root argument"));
    let root = root.canonicalize().expect("scratch root exists");
    assert!(
        root.join("test-only").is_file()
            && std::env::current_exe()
                .unwrap()
                .starts_with(root.join("Applications")),
        "self-test must run in its scratch Applications folder"
    );
    // LaunchServices need not preserve the environment across restart. Establish
    // isolation before Paths::discover or any CLI/installer code on EVERY launch.
    for (name, path) in [
        ("HOME", root.join("home")),
        ("AGT_HOME", root.join("home/.agents-router")),
        ("AGT_CONFIG_DIR", root.join("home/.agents-router")),
        ("AGT_APP_SUPPORT", root.join("support")),
        ("AGT_BIN_DIR", root.join("public-bin")),
        ("AGT_APPLICATIONS_DIRS", root.join("Applications")),
        ("CLAUDE_CONFIG_DIR", root.join("home/.claude")),
        ("CODEX_HOME", root.join("home/.codex")),
        ("GEMINI_CLI_HOME", root.join("home/.gemini")),
        ("AGT_UPDATE_TEST_ROOT", root.clone()),
    ] {
        std::env::set_var(name, path);
    }
    for name in ["PYTHONPATH", "PYTHONHOME", "AGT_APP_TEST_LAUNCHER"] {
        std::env::remove_var(name);
    }
    std::env::set_var("PATH", "/usr/bin:/bin"); // No real provider CLI can be reached.
}

#[derive(Clone, Default, Serialize)]
pub struct UpdateStatus {
    pub dialog: Option<Dialog>,
    pub received: u64,
    pub total: Option<u64>,
}
#[derive(Default)]
pub struct UpdateController {
    schedule: Mutex<Schedule>,
    pending: Mutex<Option<Update>>,
    status: Mutex<UpdateStatus>,
    installing: Mutex<bool>,
}
fn publish(app: &tauri::AppHandle, dialog: Option<Dialog>, foreground: bool) {
    let c = app.state::<UpdateController>();
    let status = {
        let mut s = c.status.lock().unwrap();
        s.dialog = dialog;
        s.received = 0;
        s.total = None;
        s.clone()
    };
    let _ = app.emit_to("settings", "update-status", status);
    if foreground {
        app::main_thread(app, |app, mtm| {
            native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}))
        });
    }
}
pub fn request(app: &tauri::AppHandle, manual: bool) {
    #[cfg(debug_assertions)]
    if std::env::var_os("AGT_APP_TEST_LAUNCHER").is_some() {
        return;
    }
    let c = app.state::<UpdateController>();
    if *c.installing.lock().unwrap() {
        return;
    }
    // Don't overwrite an offer or an error while the person is acting on it.
    if !manual && c.status.lock().unwrap().dialog.is_some() {
        return;
    }
    let start = c.schedule.lock().unwrap().begin(app::now() as u64, manual);
    if manual {
        publish(app, Some(Dialog::progress("Checking for updates…")), true);
    }
    if !start {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = match app
            .updater_builder()
            .timeout(Duration::from_secs(30))
            .build()
        {
            Ok(updater) => updater.check().await,
            Err(error) => Err(error),
        };
        let c = app.state::<UpdateController>();
        let manual = c.schedule.lock().unwrap().finish(app::now() as u64);
        let result = match result {
            Ok(Some(found)) => {
                let version = found.version.clone();
                *c.pending.lock().unwrap() = Some(found);
                CheckResult::Available(version)
            }
            Ok(None) => {
                *c.pending.lock().unwrap() = None;
                CheckResult::Current
            }
            Err(error) => CheckResult::Failed {
                permanent: update::permanent(&error),
                detail: error.to_string(),
            },
        };
        let r = update::route(result, manual, false);
        // A failed recheck retains the previously verified offer for later retry.
        if r.pending.is_some() || c.pending.lock().unwrap().is_none() {
            app::set_pending_update(&app, r.pending);
        }
        publish(&app, r.dialog, manual);
    });
}
pub fn start(app: &tauri::AppHandle) {
    #[cfg(debug_assertions)]
    if std::env::var_os("AGT_APP_TEST_LAUNCHER").is_some() {
        return;
    }
    #[cfg(debug_assertions)]
    if std::env::args().any(|a| a == "--self-test-update") {
        self_test(app);
        return;
    }
    request(app, false);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut timer = tokio::time::interval(Duration::from_secs(60));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            timer.tick().await;
            request(&app, false);
        }
    });
}
async fn install(app: &tauri::AppHandle) -> Result<(), (Stage, tauri_plugin_updater::Error)> {
    let c = app.state::<UpdateController>();
    let found = c
        .pending
        .lock()
        .unwrap()
        .clone()
        .ok_or((Stage::Check, tauri_plugin_updater::Error::ReleaseNotFound))?;
    publish(app, Some(Dialog::progress("Downloading update…")), true);
    let bytes = found
        .download(
            |chunk, total| {
                let status = {
                    let mut s = c.status.lock().unwrap();
                    s.received += chunk as u64;
                    s.total = total;
                    s.clone()
                };
                let _ = app.emit_to("settings", "update-status", status);
            },
            || {},
        )
        .await
        .map_err(|e| (Stage::Download, e))?;
    publish(app, Some(Dialog::progress("Installing update…")), true);
    found.install(bytes).map_err(|e| (Stage::Install, e))?;
    // Tauri's process plugin uses the same restart path and preserves launch args.
    app.restart();
}
#[tauri::command]
pub fn get_update_status(state: State<'_, UpdateController>) -> UpdateStatus {
    state.status.lock().unwrap().clone()
}
#[tauri::command]
pub async fn update_action(app: tauri::AppHandle, action: String) -> Result<(), String> {
    let c = app.state::<UpdateController>();
    let allowed = c
        .status
        .lock()
        .unwrap()
        .dialog
        .as_ref()
        .is_some_and(|d| d.buttons.iter().any(|(_, a)| a == &action));
    if !allowed {
        return Err("This update action is no longer available.".into());
    }
    match action.as_str() {
        "close" => publish(&app, None, false),
        "check" => {
            publish(&app, None, false);
            request(&app, true);
        }
        "releases" => {
            let status = tokio::process::Command::new("/usr/bin/open")
                .arg(update::RELEASES)
                .status()
                .await
                .map_err(|e| e.to_string())?;
            if !status.success() {
                return Err("Couldn't open the releases page.".into());
            }
            publish(&app, None, false);
        }
        "install" => {
            {
                let mut busy = c.installing.lock().unwrap();
                if *busy {
                    return Err("An update is already installing.".into());
                }
                *busy = true;
            }
            if let Err((stage, error)) = install(&app).await {
                publish(
                    &app,
                    Some(Dialog::failure(
                        stage,
                        update::permanent(&error),
                        error.to_string(),
                    )),
                    true,
                );
            }
            *c.installing.lock().unwrap() = false;
        }
        _ => return Err("Unknown update action".into()),
    }
    Ok(())
}

// A debug-only rehearsal in a copied bundle; never accepts a runtime URL/key.
// The script embeds those into its scratch config before compiling.
#[cfg(debug_assertions)]
fn self_test(app: &tauri::AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = async {
            let root = std::path::PathBuf::from(std::env::var_os("AGT_UPDATE_TEST_ROOT").ok_or("missing scratch root")?);
            if !root.is_absolute() || !root.join("test-only").is_file() { return Err("invalid scratch root".into()); }
            let c = app.state::<InstallController>();
            if c.paths.support != root.join("support") || c.paths.home != root.join("home") { return Err("nonisolated update test".into()); }
            for _ in 0..600 {
                let v = app.package_info().version.to_string();
                if c.paths.version().as_deref() == Some(&v) && (v == "0.17.0" || c.paths.support.join("updated").join(&v).is_file()) { break; }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            c.settled().await;
            let version = app.package_info().version.to_string();
            if c.paths.version().as_deref() != Some(&version) { return Err("runtime not refreshed".into()); }
            if version == "0.17.0" {
                let found = app.updater_builder().timeout(Duration::from_secs(30)).build().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())?.ok_or("no test update")?;
                if found.version != "0.17.1-test" { return Err("unexpected test version".into()); }
                *app.state::<UpdateController>().pending.lock().unwrap() = Some(found);
                install(&app).await.map_err(|(_, e)| e.to_string())?;
            } else if version == "0.17.1-test" {
                // A second attempt must read the refresh record and do no work.
                crate::install::refresh_plugins(&c.paths).await?;
                std::fs::write(root.join("relaunch.json"), serde_json::to_vec(&serde_json::json!({"version":version,"runtime":c.paths.version(),"refresh_record":c.paths.support.join("updated").join(&version).is_file()})).unwrap()).map_err(|e| e.to_string())?;
            } else { return Err("unexpected running version".into()); }
            Ok::<(), String>(())
        }.await;
        if let Err(error) = result {
            app::log(&format!("update self-test failed: {error}"));
            if let Some(root) = std::env::var_os("AGT_UPDATE_TEST_ROOT") {
                let _ = std::fs::write(std::path::PathBuf::from(root).join("failure.txt"), error);
            }
        }
        app::quit(&app);
    });
}
