//! This entire module (including the injected observer) is absent from releases.
use crate::{app, native};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::Write,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};
use tauri::{webview::PageLoadEvent, Manager};

#[derive(Default)]
struct State {
    loads: Mutex<BTreeMap<String, usize>>,
    pages: Mutex<Value>,
    written: AtomicBool,
}
fn enabled() -> bool {
    std::env::var_os("AGT_APP_SELFTEST").is_some()
}

pub fn instrument(builder: tauri::Builder<tauri::Wry>) -> tauri::Builder<tauri::Wry> {
    if !enabled() {
        return builder;
    }
    let usage: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/app/usage-popover.json"
    ))
    .unwrap();
    let config: Value =
        serde_json::from_str(include_str!("../../../tests/fixtures/app/config-show.json")).unwrap();
    let roles: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/app/roles-show-panes.json"
    ))
    .unwrap();
    let accounts: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/app/accounts-list-locked.json"
    ))
    .unwrap();
    let sharing: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/app/share-list-kinds.json"
    ))
    .unwrap();
    let activity: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/app/activity-log.json"
    ))
    .unwrap();
    let expected = json!({
        "popover": usage["providers"].as_array().unwrap().iter().flat_map(|p| p["accounts"].as_array().unwrap()).map(|a| a["name"].clone()).collect::<Vec<_>>(),
        "general": config["settings"].as_array().unwrap().len(),
        "generalVisible": config["settings"].as_array().unwrap().iter().filter(|r| r["kind"] != "fixed" && !r["key"].as_str().unwrap().starts_with("share.")).count(),
        "roles": roles["roles"].as_array().unwrap().iter().map(|r| r["name"].clone()).collect::<Vec<_>>(),
        "accounts": accounts["runtimes"].as_array().unwrap().iter().map(|r| r["accounts"].as_array().unwrap().len()).sum::<usize>(),
        "sharing": sharing["items"].as_array().unwrap().iter().map(|i| i["name"].clone()).collect::<Vec<_>>(),
        "activity": activity["entries"].as_array().unwrap().len(),
    });
    builder
        .manage(State::default())
        .append_invoke_initialization_script(
            include_str!("selftest.js").replace("__AGT_EXPECTED__", &expected.to_string()),
        )
        .on_page_load(|view, payload| {
            if payload.event() == PageLoadEvent::Started {
                *view
                    .state::<State>()
                    .loads
                    .lock()
                    .unwrap()
                    .entry(view.label().into())
                    .or_default() += 1;
            }
        })
        .on_web_content_process_terminate(|view| {
            if let Ok(path) = report_path() {
                finish(
                    view.app_handle(),
                    &path,
                    Value::Null,
                    false,
                    Some(format!("{} WebKit process terminated", view.label())),
                );
            }
        })
}

fn output(program: &str, args: &[&str]) -> String {
    Command::new(program)
        .args(args)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
        .unwrap_or_default()
}
fn report_path() -> Result<PathBuf, String> {
    let path = PathBuf::from(std::env::var_os("AGT_APP_SELFTEST").ok_or("missing report path")?);
    let parent = path.parent().ok_or("missing private folder")?;
    let temp = PathBuf::from(std::env::var_os("RUNNER_TEMP").ok_or("missing RUNNER_TEMP")?)
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let metadata = fs::symlink_metadata(parent).map_err(|e| e.to_string())?;
    if !path.is_absolute()
        || metadata.file_type().is_symlink()
        || metadata.permissions().mode() & 0o777 != 0o700
        || !parent
            .canonicalize()
            .map_err(|e| e.to_string())?
            .starts_with(temp)
        || path.exists()
    {
        return Err("ci-selftest report needs a fresh private RUNNER_TEMP folder".into());
    }
    let home = PathBuf::from(std::env::var_os("HOME").ok_or("missing isolated HOME")?);
    let agt = PathBuf::from(std::env::var_os("AGT_HOME").ok_or("missing isolated AGT_HOME")?);
    let fake = PathBuf::from(std::env::var_os("AGT_APP_TEST_LAUNCHER").ok_or("missing fake agt")?);
    if !home.starts_with(parent)
        || !agt.starts_with(&home)
        || !fake.is_absolute()
        || !fake.is_file()
    {
        return Err("ci-selftest requires isolated HOME/AGT_HOME and an absolute fake agt".into());
    }
    Ok(path)
}
fn finish(
    handle: &tauri::AppHandle,
    path: &std::path::Path,
    pages: Value,
    shown: bool,
    error: Option<String>,
) {
    if handle.state::<State>().written.swap(true, Ordering::SeqCst) {
        return;
    }
    let loads = handle.state::<State>().loads.lock().unwrap().clone();
    let os_version = output("/usr/bin/sw_vers", &["-productVersion"]);
    let os_build = output("/usr/bin/sw_vers", &["-buildVersion"]);
    let arch = output("/usr/bin/uname", &["-m"]);
    let webkit = output(
        "/usr/libexec/PlistBuddy",
        &[
            "-c",
            "Print :CFBundleVersion",
            "/System/Library/Frameworks/WebKit.framework/Resources/Info.plist",
        ],
    );
    let pass = error.is_none()
        && shown
        && loads.len() == 2
        && loads.values().all(|n| *n == 1)
        && !os_version.is_empty()
        && !os_build.is_empty()
        && arch == "arm64"
        && !webkit.is_empty()
        && ["popover", "settings"]
            .iter()
            .all(|name| valid_page(&pages[name], *name == "settings"));
    let pages = if pages.is_null() {
        handle.state::<State>().pages.lock().unwrap().clone()
    } else {
        pages
    };
    let report = json!({"schema_version":1,"pass":pass,"os_version":os_version,"os_build":os_build,"architecture":arch,"webkit_cf_bundle_version":webkit,
        "baseline_macos_14_0":"unverified; this run proves only the reported host WebKit", "windows_shown":shown,"loads":loads,"pages":pages,"error":error});
    let written = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .and_then(|mut f| {
            f.write_all(serde_json::to_string_pretty(&report).unwrap().as_bytes())?;
            f.sync_all()
        });
    eprintln!(
        "ci-selftest {}: {}",
        if pass && written.is_ok() {
            "PASS"
        } else {
            "FAIL"
        },
        path.display()
    );
    app::exit(handle, if pass && written.is_ok() { 0 } else { 1 });
}
fn valid_page(page: &Value, settings: bool) -> bool {
    page["ready"] == true
        && page["fixture"]["ok"] == true
        && page["errors"].as_array().is_some_and(|v| v.is_empty())
        && page["probes"]
            .as_object()
            .is_some_and(|v| v.len() == 10 && v.values().all(|v| *v == true))
        && if settings {
            ["general", "roles", "accounts", "sharing", "activity"]
                .iter()
                .all(|pane| {
                    let p = &page["panes"][pane];
                    p["ok"] == true
                        && p["fixture"] == true
                        && p["rows"] == true
                        && p["count"].as_u64().is_some_and(|n| n > 0)
                })
        } else {
            page["layout"]["ok"] == true
        }
}
async fn read_page(handle: &tauri::AppHandle, label: &str) -> Result<Value, String> {
    let window = handle.get_webview_window(label).ok_or("window missing")?;
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    window
        .eval_with_callback(
            "JSON.stringify(window.__AGT_CI_SELFTEST || null)",
            move |reply| {
                if let Some(tx) = tx.lock().unwrap().take() {
                    let _ = tx.send(reply);
                }
            },
        )
        .map_err(|e| e.to_string())?;
    let reply = tokio::time::timeout(Duration::from_secs(2), rx)
        .await
        .map_err(|_| "WebKit callback deadline")?
        .map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_str(&reply).map_err(|e| e.to_string())?;
    if let Some(text) = value.as_str() {
        serde_json::from_str(text).map_err(|e| e.to_string())
    } else {
        Ok(value)
    }
}
pub fn start(handle: &tauri::AppHandle) {
    if !enabled() {
        return;
    }
    let path = match report_path() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("{error}");
            app::exit(handle, 1);
            return;
        }
    };
    let timer = handle.clone();
    let deadline_path = path.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(60));
        finish(
            &timer,
            &deadline_path,
            Value::Null,
            false,
            Some("60 s native deadline".into()),
        );
    });
    let handle = handle.clone();
    tauri::async_runtime::spawn(async move {
        app::main_thread(&handle, |app, mtm| native::show(&app, mtm));
        tokio::time::sleep(Duration::from_millis(500)).await;
        // NSPanel visibility is checked before opening Settings hides it on blur.
        use tauri_nspanel::ManagerExt;
        let popover_shown = handle
            .get_webview_panel("popover")
            .is_ok_and(|p| p.is_visible());
        app::main_thread(&handle, |app, mtm| {
            native::open_settings(&app, mtm, json!({"pane":"general"}))
        });
        tokio::time::sleep(Duration::from_millis(500)).await;
        let shown = popover_shown
            && handle
                .get_webview_window("settings")
                .is_some_and(|w| w.is_visible().unwrap_or(false));
        if let Some(window) = handle.get_webview_window("settings") {
            if let Err(error) = window.eval("window.__AGT_CI_BEGIN = true") {
                finish(&handle, &path, Value::Null, shown, Some(error.to_string()));
                return;
            }
        }
        let mut pages = json!({});
        loop {
            for label in ["popover", "settings"] {
                match read_page(&handle, label).await {
                    Ok(page) => pages[label] = page,
                    Err(error) => {
                        finish(&handle, &path, pages, shown, Some(error));
                        return;
                    }
                }
            }
            *handle.state::<State>().pages.lock().unwrap() = pages.clone();
            if ["popover", "settings"]
                .iter()
                .all(|label| pages[label]["ready"] == true)
            {
                // Observe late resource/rejection errors and unexpected reloads too.
                tokio::time::sleep(Duration::from_millis(500)).await;
                for label in ["popover", "settings"] {
                    match read_page(&handle, label).await {
                        Ok(page) => pages[label] = page,
                        Err(error) => {
                            finish(&handle, &path, pages, shown, Some(error));
                            return;
                        }
                    }
                }
                finish(&handle, &path, pages, shown, None);
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });
}
