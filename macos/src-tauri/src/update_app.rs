//! Fixed-purpose updater controller. No updater URLs or keys cross IPC.
#[cfg(debug_assertions)]
use crate::install_app::InstallController;
use crate::{
    app, diag_log, native,
    update::{self, CheckResult, Dialog, Schedule, Stage},
};
use serde::Serialize;
use std::{
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering::Relaxed},
        Mutex,
    },
    time::Duration,
};
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
        // Not home/.agents-router: a test build refuses the release's agt home.
        ("AGT_HOME", root.join("home/agt-home")),
        ("AGT_CONFIG_DIR", root.join("home/agt-home")),
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
/// The configured feed URLs, read from the app config: never the updater's key,
/// headers or proxy. Each is sanitized on its own, separated by spaces.
fn endpoints(app: &tauri::AppHandle) -> String {
    let config = app.config();
    let list: Vec<&str> = config
        .plugins
        .0
        .get("updater")
        .and_then(|u| u.get("endpoints"))
        .and_then(|e| e.as_array())
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    diag_log::url_list(&list)
}
/// An updater error for the log: never the signature, nor any feed content.
fn describe(error: &tauri_plugin_updater::Error) -> String {
    use tauri_plugin_updater::Error as E;
    match error {
        E::SignatureUtf8(_) => "the update signature could not be decoded".into(),
        E::Serialization(e) => diag_log::json_error(e),
        _ => error.to_string(),
    }
}
fn stage_name(stage: Stage) -> &'static str {
    match stage {
        Stage::Check => "check",
        Stage::Download => "download",
        Stage::Install => "install",
    }
}
fn shown(path: Option<&Path>) -> String {
    path.map_or_else(|| "unknown".into(), |p| p.display().to_string())
}
/// The version on disk at `bundle`, read from its Info.plist after the install.
async fn bundle_version(bundle: Option<&Path>) -> String {
    let Some(bundle) = bundle else {
        return "unknown".into();
    };
    let mut command = tokio::process::Command::new("/usr/bin/plutil");
    command
        .args(["-extract", "CFBundleShortVersionString", "raw", "-o", "-"])
        .arg(bundle.join("Contents/Info.plist"))
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true);
    match tokio::time::timeout(Duration::from_secs(5), command.output()).await {
        Ok(Ok(out)) if out.status.success() => String::from_utf8_lossy(&out.stdout).trim().into(),
        // plutil's stderr stays out of the log: its exit status only.
        Ok(Ok(out)) => format!("unknown (plutil {})", out.status),
        Ok(Err(error)) => format!("unknown ({error})"),
        Err(_) => "unknown (plutil timed out)".into(),
    }
}
pub fn request(app: &tauri::AppHandle, manual: bool) {
    #[cfg(any(debug_assertions, feature = "ci-selftest"))]
    if std::env::var_os("AGT_APP_TEST_LAUNCHER").is_some() {
        return;
    }
    if !update::checks_feed(&app.config().identifier) {
        if manual {
            app::log("update check skipped: test build");
            publish(app, Some(Dialog::test_build()), true);
        }
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
        return; // The 60 s timer's ticks end here until a check is due.
    }
    app::log(&format!(
        "update check start manual={manual} current={} endpoints={}",
        app.package_info().version,
        endpoints(app)
    ));
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
                app::log(&format!(
                    "update check result available version={} current={} date={} target={} url={}",
                    found.version,
                    found.current_version,
                    found.date.map_or_else(|| "none".into(), |d| d.to_string()),
                    found.target,
                    found.download_url
                ));
                let version = found.version.clone();
                *c.pending.lock().unwrap() = Some(found);
                CheckResult::Available(version)
            }
            Ok(None) => {
                app::log(&format!(
                    "update check result current version={}",
                    app.package_info().version
                ));
                *c.pending.lock().unwrap() = None;
                CheckResult::Current
            }
            Err(error) => {
                let permanent = update::permanent(&error);
                app::log(&format!(
                    "update check result failed permanent={permanent} error={}",
                    describe(&error)
                ));
                CheckResult::Failed {
                    permanent,
                    detail: error.to_string(),
                }
            }
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
    if !update::checks_feed(&app.config().identifier) {
        return; // A test build checks no feed; a manual check explains why.
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
    // The bundle the plugin replaces, from the executable the plugin and the
    // restart both use.
    let exe = tauri::utils::platform::current_exe();
    let bundle: Option<PathBuf> = exe
        .as_ref()
        .ok()
        .and_then(|e| tauri_plugin_updater::extract_path_from_executable(e).ok());
    publish(app, Some(Dialog::progress("Downloading update…")), true);
    app::log(&format!(
        "update install start version={} current={} exe={} bundle={} bundle_version={}",
        found.version,
        found.current_version,
        shown(exe.as_deref().ok()),
        shown(bundle.as_deref()),
        bundle_version(bundle.as_deref()).await
    ));
    app::log(&format!("update download start url={}", found.download_url));
    let received = AtomicU64::new(0);
    let quarter = AtomicU64::new(0);
    let bytes = found
        .download(
            |chunk, total| {
                let got = received.fetch_add(chunk as u64, Relaxed) + chunk as u64;
                if let Some(total) = total.filter(|t| *t > 0) {
                    // At most three lines: 25, 50 and 75 %; "finished" is 100.
                    let step = (got.min(total) * 4 / total).min(3);
                    if quarter.fetch_max(step, Relaxed) < step {
                        app::log(&format!(
                            "update download progress {}% bytes={got}/{total}",
                            step * 25
                        ));
                    }
                }
                let status = {
                    let mut s = c.status.lock().unwrap();
                    s.received += chunk as u64;
                    s.total = total;
                    s.clone()
                };
                let _ = app.emit_to("settings", "update-status", status);
            },
            // download() verifies the signature right after this callback.
            || {
                app::log(&format!(
                    "update download finished bytes={}; verifying signature",
                    received.load(Relaxed)
                ))
            },
        )
        .await
        .map_err(|e| (Stage::Download, e))?;
    app::log(&format!("update signature verified bytes={}", bytes.len()));
    publish(app, Some(Dialog::progress("Installing update…")), true);
    app::log(&format!(
        "update install replacing bundle={}",
        shown(bundle.as_deref())
    ));
    found.install(bytes).map_err(|e| (Stage::Install, e))?;
    // What is on disk now: the old version here means the install missed the bundle.
    app::log(&format!(
        "update install ok bundle={} bundle_version={} expected={}",
        shown(bundle.as_deref()),
        bundle_version(bundle.as_deref()).await,
        found.version
    ));
    // Tauri's restart path; a test build also carries its chosen folders as flags.
    app::restart(app);
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
                let permanent = update::permanent(&error);
                app::log(&format!(
                    "update failed stage={} permanent={permanent} error={}",
                    stage_name(stage),
                    describe(&error)
                ));
                publish(
                    &app,
                    Some(Dialog::failure(stage, permanent, error.to_string())),
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
            // scripts/check_app_update.py picks both versions (this checkout's and the next
            // patch's test build) and writes them here; no version is spelled in this file.
            let versions: serde_json::Value = serde_json::from_slice(&std::fs::read(root.join("versions.json")).map_err(|e| format!("versions.json: {e}"))?).map_err(|e| format!("versions.json: {e}"))?;
            let (from, to) = match (versions["from"].as_str(), versions["to"].as_str()) {
                (Some(from), Some(to)) if !from.is_empty() && from != to => (from.to_owned(), to.to_owned()),
                _ => return Err("versions.json needs two different versions, from and to".into()),
            };
            let c = app.state::<InstallController>();
            if c.paths.support != root.join("support") || c.paths.home != root.join("home") { return Err("nonisolated update test".into()); }
            for _ in 0..600 {
                let v = app.package_info().version.to_string();
                if c.paths.version().as_deref() == Some(&v) && (v == from || c.paths.support.join("updated").join(&v).is_file()) { break; }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            c.settled().await;
            let version = app.package_info().version.to_string();
            if c.paths.version().as_deref() != Some(&version) { return Err("runtime not refreshed".into()); }
            if version == from {
                let found = app.updater_builder().timeout(Duration::from_secs(30)).build().map_err(|e| e.to_string())?.check().await.map_err(|e| e.to_string())?.ok_or("no test update")?;
                if found.version != to { return Err(format!("unexpected test version {}, expected {to}", found.version)); }
                *app.state::<UpdateController>().pending.lock().unwrap() = Some(found);
                install(&app).await.map_err(|(_, e)| e.to_string())?;
            } else if version == to {
                // A second attempt must read the refresh record and do no work.
                crate::install::refresh_plugins(&c.paths).await?;
                std::fs::write(root.join("relaunch.json"), serde_json::to_vec(&serde_json::json!({"version":version,"runtime":c.paths.version(),"refresh_record":c.paths.support.join("updated").join(&version).is_file()})).unwrap()).map_err(|e| e.to_string())?;
            } else { return Err(format!("unexpected running version {version}, expected {from} or {to}")); }
            Ok::<(), String>(())
        }.await;
        if let Err(error) = result {
            // The error can be `agt update`'s stderr: the file gets the marker only.
            app::log("update self-test failed; its error is on stderr and in failure.txt");
            app::trace(&format!("update self-test failed: {error}"));
            if let Some(root) = std::env::var_os("AGT_UPDATE_TEST_ROOT") {
                let _ = std::fs::write(std::path::PathBuf::from(root).join("failure.txt"), error);
            }
        }
        app::quit(&app);
    });
}
