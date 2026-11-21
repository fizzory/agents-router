//! Fixed-purpose updater controller. No updater URLs or keys cross IPC.
#[cfg(debug_assertions)]
use crate::install_app::InstallController;
use crate::{
    app, diag_log, native,
    update::{self, CheckResult, Dialog, Phase, Schedule, Stage},
};
use serde::Serialize;
use std::{
    future::Future,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering::Relaxed},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, Runtime, State};
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

/// Settings' dialog, and for both windows the version and where it stands.
#[derive(Clone, Debug, Default, Serialize)]
pub struct UpdateStatus {
    pub dialog: Option<Dialog>,
    pub received: u64,
    pub total: Option<u64>,
    pub version: Option<String>,
    pub phase: Phase,
    pub retryable: bool,
    /// Rust's error text for a failed install. The popover shows it only as a tooltip.
    pub detail: String,
}
pub struct UpdateController<R: Runtime = tauri::Wry> {
    schedule: Mutex<Schedule>,
    pending: Mutex<Option<Update>>,
    status: Mutex<UpdateStatus>,
    /// The one install guard, shared by the popover's row and Settings' dialog.
    installing: Mutex<bool>,
    /// Brings Settings forward for its own dialog. Tests count the calls.
    foreground: fn(&AppHandle<R>),
}
impl Default for UpdateController {
    fn default() -> Self {
        Self {
            schedule: Default::default(),
            pending: Default::default(),
            status: Default::default(),
            installing: Default::default(),
            foreground: open_settings,
        }
    }
}
fn open_settings(app: &tauri::AppHandle) {
    app::main_thread(app, |app, mtm| {
        native::open_settings(&app, mtm, serde_json::json!({"pane":"general"}))
    });
}
/// Both windows get every status: Settings' dialog and the popover's update row.
fn emit<R: Runtime>(app: &AppHandle<R>, status: &UpdateStatus) {
    for window in ["settings", "popover"] {
        let _ = app.emit_to(window, "update-status", status.clone());
    }
}
fn publish<R: Runtime>(app: &AppHandle<R>, dialog: Option<Dialog>, foreground: bool) {
    let c = app.state::<UpdateController<R>>();
    let status = {
        let mut s = c.status.lock().unwrap();
        s.dialog = dialog;
        s.received = 0;
        s.total = None;
        s.clone()
    };
    emit(app, &status);
    if foreground {
        (c.foreground)(app);
    }
}
/// A finished check's offer for the popover's row. It replaces a failure row,
/// never an install in progress.
pub fn offer<R: Runtime>(app: &AppHandle<R>, version: Option<String>) {
    let c = app.state::<UpdateController<R>>();
    // Held while the status changes, so an install can't start in between.
    let installing = c.installing.lock().unwrap();
    if *installing {
        return;
    }
    let status = {
        let mut s = c.status.lock().unwrap();
        s.phase = if version.is_some() {
            Phase::Available
        } else {
            Phase::Idle
        };
        s.version = version;
        s.retryable = false;
        s.detail.clear();
        s.clone()
    };
    drop(installing);
    emit(app, &status);
}
/// An install step for both windows. Only Settings' own install shows its
/// progress dialog and brings Settings forward; the popover's never does.
fn progress<R: Runtime>(app: &AppHandle<R>, version: &str, phase: Phase, settings: bool) {
    let c = app.state::<UpdateController<R>>();
    let status = {
        let mut s = c.status.lock().unwrap();
        s.version = Some(version.into());
        s.phase = phase;
        s.retryable = false;
        s.detail.clear();
        s.received = 0;
        s.total = None;
        if settings {
            s.dialog = Some(Dialog::progress(if phase == Phase::Downloading {
                "Downloading update…"
            } else {
                "Installing update…"
            }));
        }
        s.clone()
    };
    emit(app, &status);
    if settings {
        (c.foreground)(app);
    }
}
/// Records a failed install while the guard is still held. Settings' install
/// also gets `Dialog::failure`, its wording unchanged. The returned closure
/// shows the failure once the guard is free.
fn failed<R: Runtime>(
    app: &AppHandle<R>,
    stage: Stage,
    error: tauri_plugin_updater::Error,
    settings: bool,
) -> impl FnOnce() {
    let permanent = update::permanent(&error);
    app::log(&format!(
        "update failed stage={} permanent={permanent} error={}",
        stage_name(stage),
        describe(&error)
    ));
    let c = app.state::<UpdateController<R>>();
    let status = {
        let mut s = c.status.lock().unwrap();
        s.phase = stage.failed();
        s.received = 0;
        s.total = None;
        if s.phase == Phase::Idle {
            // Nothing was waiting to install.
            s.version = None;
            s.retryable = false;
            s.detail.clear();
        } else {
            s.retryable = !permanent;
            s.detail = error.to_string();
        }
        if settings {
            s.dialog = Some(Dialog::failure(stage, permanent, error.to_string()));
        }
        s.clone()
    };
    let app = app.clone();
    let foreground = c.foreground;
    move || {
        emit(&app, &status);
        if settings {
            foreground(&app);
        }
    }
}
const BUSY: &str = "An update is already installing.";
/// One install at a time from either window. A busy guard never starts `run`.
/// `fail` records a failure with the guard held; what it returns runs after the
/// guard is free, so a Retry shown by it takes one click.
async fn attempt<E, A: FnOnce()>(
    guard: &Mutex<bool>,
    run: impl Future<Output = Result<(), (Stage, E)>>,
    fail: impl FnOnce(Stage, E) -> A,
) -> Result<(), &'static str> {
    {
        let mut busy = guard.lock().unwrap();
        if *busy {
            return Err(BUSY);
        }
        *busy = true;
    }
    struct Release<'a>(&'a Mutex<bool>);
    impl Drop for Release<'_> {
        fn drop(&mut self) {
            *self.0.lock().unwrap_or_else(|e| e.into_inner()) = false;
        }
    }
    let release = Release(guard);
    let show = run.await.err().map(|(stage, error)| fail(stage, error));
    drop(release);
    if let Some(show) = show {
        show();
    }
    Ok(())
}
/// Settings' dialog Install and the popover's Install and Restart or Retry.
/// `run` is `install`; a busy guard drops it unpolled.
async fn run_install<R: Runtime>(
    app: &AppHandle<R>,
    settings: bool,
    run: impl Future<Output = Result<(), (Stage, tauri_plugin_updater::Error)>>,
) -> Result<(), String> {
    let c = app.state::<UpdateController<R>>();
    attempt(&c.installing, run, |stage, error| {
        failed(app, stage, error, settings)
    })
    .await
    .map_err(String::from)
}
async fn open_releases_page() -> Result<(), String> {
    let status = tokio::process::Command::new("/usr/bin/open")
        .arg(update::RELEASES)
        .status()
        .await
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("Couldn't open the releases page.".into());
    }
    Ok(())
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
        // Every finished check replaces the popover's row, a failure row included.
        let offered = c.pending.lock().unwrap().as_ref().map(|u| u.version.clone());
        offer(&app, offered);
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
async fn install(
    app: &tauri::AppHandle,
    settings: bool,
) -> Result<(), (Stage, tauri_plugin_updater::Error)> {
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
    progress(app, &found.version, Phase::Downloading, settings);
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
                emit(app, &status);
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
    progress(app, &found.version, Phase::Installing, settings);
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
            open_releases_page().await?;
            publish(&app, None, false);
        }
        "install" => run_install(&app, true, install(&app, true)).await?,
        _ => return Err("Unknown update action".into()),
    }
    Ok(())
}
/// The popover's Install and Restart, and its Retry: Settings' guard, without
/// Settings' dialog and without opening Settings.
#[tauri::command]
pub async fn install_update(app: tauri::AppHandle) -> Result<(), String> {
    run_install(&app, false, install(&app, false)).await
}
/// The popover's Open Releases Page: the browser, and nothing else.
#[tauri::command]
pub async fn open_releases() -> Result<(), String> {
    open_releases_page().await
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
                install(&app, true).await.map_err(|(_, e)| e.to_string())?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::Cell,
        pin::pin,
        sync::{atomic::AtomicBool, Arc},
        task::{Context, Poll, Waker},
    };
    use tauri::{test::MockRuntime, Listener};
    use tauri_plugin_updater::Error;

    // The offered update; versions are never spelled in this file.
    const NEXT: &str = "next";
    thread_local! {
        static SETTINGS_OPENED: Cell<usize> = const { Cell::new(0) };
    }
    fn count_settings(_: &AppHandle<MockRuntime>) {
        SETTINGS_OPENED.with(|c| c.set(c.get() + 1));
    }
    fn mock_app() -> tauri::App<MockRuntime> {
        tauri::test::mock_builder()
            .manage(UpdateController::<MockRuntime> {
                schedule: Default::default(),
                pending: Default::default(),
                status: Default::default(),
                installing: Default::default(),
                foreground: count_settings,
            })
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap()
    }
    fn poll<F: Future>(future: std::pin::Pin<&mut F>) -> Poll<F::Output> {
        future.poll(&mut Context::from_waker(Waker::noop()))
    }

    #[test]
    fn attempt_runs_one_install_and_frees_the_guard_before_showing_a_failure() {
        for stage in [Stage::Download, Stage::Install] {
            let guard = Mutex::new(false);
            let (tx, rx) = tokio::sync::oneshot::channel::<()>();
            let shown = Cell::new(false);
            let mut first = pin!(attempt(
                &guard,
                async {
                    rx.await.unwrap();
                    Err((stage, "boom"))
                },
                |failed, error| {
                    assert_eq!((failed, error), (stage, "boom"));
                    assert!(*guard.lock().unwrap(), "recorded while the guard is held");
                    || {
                        assert!(!*guard.lock().unwrap(), "shown once the guard is free");
                        shown.set(true);
                    }
                }
            ));
            assert!(poll(first.as_mut()).is_pending());
            assert!(*guard.lock().unwrap());
            // A second click while the first install runs starts nothing.
            let started = Cell::new(false);
            let second = pin!(attempt(
                &guard,
                async {
                    started.set(true);
                    Ok::<(), (Stage, &str)>(())
                },
                |_, _| || {}
            ));
            assert_eq!(poll(second), Poll::Ready(Err(BUSY)));
            assert!(!started.get());
            tx.send(()).unwrap();
            assert_eq!(poll(first), Poll::Ready(Ok(())));
            assert!(shown.get());
            // The failure left the guard free: Retry starts a new install.
            let retried = Cell::new(false);
            let retry = pin!(attempt(
                &guard,
                async {
                    retried.set(true);
                    Ok::<(), (Stage, &str)>(())
                },
                |_, _| || {}
            ));
            assert_eq!(poll(retry), Poll::Ready(Ok(())));
            assert!(retried.get(), "{stage:?}: a failed install must free the guard");
        }
    }

    #[test]
    fn popover_and_settings_share_one_guard_and_only_settings_comes_forward() {
        let app = mock_app();
        let h = app.handle().clone();
        let c = app.state::<UpdateController<MockRuntime>>();
        let events = Arc::new(Mutex::new(Vec::<serde_json::Value>::new()));
        let sink = events.clone();
        h.listen_any("update-status", move |e| {
            sink.lock()
                .unwrap()
                .push(serde_json::from_str(e.payload()).unwrap());
        });
        let cases = [
            // (first install from Settings?, failing stage, error, phase, retryable)
            (true, Stage::Download, Error::ReleaseNotFound, "download-failed", true),
            (false, Stage::Download, Error::UnsupportedArch, "download-failed", false),
            (true, Stage::Install, Error::EmptyEndpoints, "install-failed", false),
            (false, Stage::Install, Error::ReleaseNotFound, "install-failed", true),
        ];
        for (settings, stage, error, phase, retryable) in cases {
            SETTINGS_OPENED.with(|n| n.set(0));
            *c.status.lock().unwrap() = UpdateStatus::default();
            offer(&h, Some(NEXT.into()));
            assert_eq!(c.status.lock().unwrap().phase, Phase::Available);
            let detail = error.to_string();
            let permanent = update::permanent(&error);
            let (tx, rx) = tokio::sync::oneshot::channel::<()>();
            let mut first = pin!(run_install(&h, settings, async {
                progress(&h, NEXT, Phase::Downloading, settings);
                rx.await.unwrap();
                progress(&h, NEXT, Phase::Installing, settings);
                Err((stage, error))
            }));
            assert!(poll(first.as_mut()).is_pending());
            {
                let s = c.status.lock().unwrap();
                assert_eq!(s.phase, Phase::Downloading);
                assert_eq!(s.dialog.is_some(), settings, "only Settings' install shows its dialog");
            }
            // Both windows hear every status.
            events.lock().unwrap().clear();
            offer(&h, None);
            assert_eq!(events.lock().unwrap().len(), 0, "a check never replaces an install");
            emit(&h, &c.status.lock().unwrap());
            assert_eq!(events.lock().unwrap().len(), 2);
            // A popover click, rapid or while Settings installs, starts nothing.
            for again in [false, true] {
                let started = AtomicBool::new(false);
                let busy = pin!(run_install(&h, again, async {
                    started.store(true, Relaxed);
                    Ok(())
                }));
                assert_eq!(poll(busy), Poll::Ready(Err(BUSY.to_string())));
                assert!(!started.load(Relaxed));
            }
            tx.send(()).unwrap();
            assert_eq!(poll(first), Poll::Ready(Ok(())));
            let s = c.status.lock().unwrap().clone();
            assert_eq!(serde_json::to_value(s.phase).unwrap(), phase);
            assert_eq!((s.retryable, permanent), (retryable, !retryable));
            assert_eq!(s.detail, detail);
            assert_eq!(s.version.as_deref(), Some(NEXT));
            assert_eq!(
                s.dialog,
                settings.then(|| Dialog::failure(stage, permanent, detail.clone())),
                "Settings' wording stays Dialog::failure; the popover gets no dialog"
            );
            // Settings' own install comes forward for progress, progress and failure.
            assert_eq!(SETTINGS_OPENED.with(Cell::get), if settings { 3 } else { 0 });
            assert!(!*c.installing.lock().unwrap());
            // Retry: the same command starts a new install at once.
            let retried = AtomicBool::new(false);
            let retry = pin!(run_install(&h, false, async {
                retried.store(true, Relaxed);
                Err((Stage::Check, Error::ReleaseNotFound))
            }));
            assert_eq!(poll(retry), Poll::Ready(Ok(())));
            assert!(retried.load(Relaxed));
            let s = c.status.lock().unwrap().clone();
            assert_eq!((s.phase, s.version.as_deref()), (Phase::Idle, None), "nothing to install hides the row");
        }
    }
}
