#![cfg(feature = "app-check")]
//! Exercise the actual checker arguments, exit codes and isolated bundle install.
use std::{
    fs,
    io::Write,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::{Command, Output, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "agt-check-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(root.join("home")).unwrap();
        Self(root.canonicalize().unwrap())
    }
    fn command(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_agt-app-check"));
        c.env_clear()
            .current_dir(&self.0)
            .env("HOME", self.0.join("home"))
            .env("PATH", "/usr/bin:/bin")
            .env("AGT_HOME", self.0.join("agt"))
            .env("AGT_BIN_DIR", self.0.join("bin"))
            .env("AGT_APP_SUPPORT", self.0.join("support"));
        c
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn stdin(bytes: &[u8]) -> Output {
    let root = Scratch::new();
    let mut c = root
        .command()
        .args(["--stdin", "usage"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    c.stdin.take().unwrap().write_all(bytes).unwrap();
    c.wait_with_output().unwrap()
}

#[test]
fn stdin_mode_reports_success_and_decode_failure_exit_codes() {
    let good = stdin(include_bytes!("../../../tests/fixtures/app/usage.json"));
    assert!(
        good.status.success(),
        "{}",
        String::from_utf8_lossy(&good.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&good.stdout).lines().count(), 1);
    assert!(String::from_utf8_lossy(&good.stdout).starts_with("OK: decoded usage"));
    let bad = stdin(br#"{"schema_version":1}"#);
    assert_eq!(bad.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&bad.stderr).contains("agt_version: missing field"));
}

#[test]
fn stdin_usage_accepts_an_occupied_lease_without_a_job_id() {
    let mut usage: serde_json::Value =
        serde_json::from_slice(include_bytes!("../../../tests/fixtures/app/usage.json")).unwrap();
    usage["uninstall_blockers"] = serde_json::json!([{
        "kind": "occupied_lease", "job_id": null,
        "runtime": "claude", "account": "claude-work"
    }]);
    let result = stdin(&serde_json::to_vec(&usage).unwrap());
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).starts_with("OK: decoded usage"));
}

#[test]
fn app_mode_requires_explicit_destinations_and_an_existing_bundle() {
    let root = Scratch::new();
    for name in ["AGT_APP_SUPPORT", "AGT_BIN_DIR", "AGT_HOME"] {
        let result = root
            .command()
            .env_remove(name)
            .args(["--install-cli", "--app"])
            .arg(root.0.join("Missing.app"))
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(1));
        assert!(
            String::from_utf8_lossy(&result.stderr).contains(&format!("requires explicit {name}"))
        );
    }
    let missing = root
        .command()
        .args(["--install-cli", "--app"])
        .arg(root.0.join("Missing.app"))
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&missing.stderr).contains("Missing app bundle"));
    assert!(!root.0.join("support").exists());
    let internal = root
        .command()
        .arg("--install-cli-isolated")
        .arg(&root.0)
        .output()
        .unwrap();
    assert_eq!(internal.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&internal.stderr)
        .contains("Internal check requires isolated paths"));
}

#[test]
fn app_mode_installs_only_the_supplied_scratch_bundles_runtime() {
    let root = Scratch::new();
    let app = root.0.join("Agents Router.app");
    let runtime = app.join("Contents/Resources/runtime");
    for name in [
        "bin/python3",
        "plugin/.claude-plugin/plugin.json",
        "plugin/.claude-plugin/marketplace.json",
        "plugin/hooks/hooks.json",
    ] {
        let p = runtime.join(name);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, "{}").unwrap();
    }
    fs::write(
        runtime.join("plugin/.claude-plugin/plugin.json"),
        r#"{"version":"0.17.0"}"#,
    )
    .unwrap();
    fs::write(runtime.join("agt-bundle.json"), r#"{"schema_version":1,"source":"app","app_version":"0.17.0","runtime_version":"0.17.0","python_version":"3.13.18","plugin_path":"plugin"}"#).unwrap();
    fs::write(runtime.join("bin/python3"), "#!/bin/sh\n[ \"$*\" = '-I -m agents_router --version' ] || exit 99\nprintf 'agents-router 0.17.0\\n'\n").unwrap();
    fs::set_permissions(
        runtime.join("bin/python3"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let result = root
        .command()
        .args(["--install-cli", "--app"])
        .arg(&app)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let output = String::from_utf8_lossy(&result.stdout);
    assert!(output.contains("OK: installed agt 0.17.0"));
    for path in [
        "support/runtimes/0.17.0",
        "support/bin/agt",
        "bin/agt",
        "agt",
    ] {
        assert!(output.contains(root.0.join(path).to_str().unwrap()));
    }
    assert_eq!(
        fs::read_link(root.0.join("bin/agt")).unwrap(),
        root.0.join("support/bin/agt")
    );
    let version = Command::new(root.0.join("bin/agt"))
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", root.0.join("home"))
        .env("AGT_HOME", root.0.join("agt"))
        .arg("--version")
        .output()
        .unwrap();
    assert!(version.status.success());
    assert_eq!(version.stdout, b"agents-router 0.17.0\n");
    assert!(!root.0.join("home/.local").exists());
    assert!(!root.0.join("home/Library").exists());
}

fn resolve(root: &Scratch, identifier: &str, args: &[&str]) -> Output {
    root.command()
        .arg("--resolve-paths")
        .arg(identifier)
        .args(args)
        .output()
        .unwrap()
}
const RELEASE: &str = "io.github.watcharagorn.agents-router";
const TEST: &str = "io.github.watcharagorn.agents-router.test";

#[test]
fn release_refuses_test_flags_and_keeps_its_environment() {
    let root = Scratch::new();
    let refused = resolve(&root, RELEASE, &["--test-app-support", "/tmp/agt-test"]);
    assert_eq!(refused.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&refused.stderr)
        .contains("--test-app-support works only in Agents Router Test.app"));
    let release = resolve(&root, RELEASE, &[]);
    assert!(
        release.status.success(),
        "{}",
        String::from_utf8_lossy(&release.stderr)
    );
    let doc: serde_json::Value = serde_json::from_slice(&release.stdout).unwrap();
    assert_eq!(doc["test"], false);
    assert!(doc["rows"].is_null());
    assert_eq!(
        doc["launcher"],
        root.0.join("support/bin/agt").to_str().unwrap()
    );
    assert_eq!(
        doc["settings"],
        root.0.join("agt/settings.toml").to_str().unwrap()
    );
    assert_eq!(
        doc["child_env"]["AGT_HOME"],
        root.0.join("agt").to_str().unwrap()
    );
    assert!(doc["child_env"].get("HOME").is_none());
    assert!(doc["child_env"].get("AGT_EXCLUDE_HOMES").is_none());
    assert!(doc["account_home"].is_null());
    assert_eq!(doc["relaunch"], serde_json::json!([]));
    // Resolving creates nothing.
    assert_eq!(fs::read_dir(&root.0).unwrap().count(), 1);
}

#[test]
fn test_build_takes_flag_over_env_over_default_and_refuses_production_folders() {
    let root = Scratch::new();
    let flag_home = root.0.join("flag agt home");
    let result = resolve(
        &root,
        TEST,
        &["--test-agt-home", flag_home.to_str().unwrap()],
    );
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let doc: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(doc["test"], true);
    let rows = doc["rows"].as_array().unwrap();
    let row = |key: &str| rows.iter().find(|r| r["key"] == key).unwrap().clone();
    assert_eq!(row("app_support")["source"], "env");
    assert_eq!(row("app_support")["origin"], "AGT_APP_SUPPORT");
    assert_eq!(row("bin_dir")["path"], root.0.join("bin").to_str().unwrap());
    assert_eq!(row("agt_home")["source"], "flag");
    assert_eq!(row("agt_home")["origin"], "--test-agt-home");
    assert_eq!(row("config_dir")["source"], "default");
    assert_eq!(row("config_dir")["path"], flag_home.to_str().unwrap());
    let private = root.0.join("support/home");
    assert_eq!(doc["child_home"], private.to_str().unwrap());
    assert_eq!(doc["child_env"]["HOME"], private.to_str().unwrap());
    assert_eq!(
        doc["child_env"]["AGT_CONFIG_DIR"],
        flag_home.to_str().unwrap()
    );
    assert!(doc["child_env"]["CLAUDE_CONFIG_DIR"].is_null());
    // The account's home comes from the user database, never from HOME.
    let account = agents_router_app::app_paths::account_home().unwrap();
    assert_eq!(doc["account_home"], account.to_str().unwrap());
    assert_ne!(account, root.0.join("home"));
    let physical = agents_router_app::app_paths::physical(&account).unwrap();
    assert_eq!(
        doc["child_env"]["AGT_EXCLUDE_HOMES"],
        format!(
            "{}:{}:{}",
            private.display(),
            root.0.join("home").display(),
            physical.display()
        )
    );
    assert_eq!(doc["relaunch"].as_array().unwrap().len(), 6);
    let swapped = root
        .command()
        .env("CFFIXED_USER_HOME", root.0.join("home"))
        .arg("--resolve-paths")
        .arg(TEST)
        .output()
        .unwrap();
    assert!(swapped.status.success());
    let doc: serde_json::Value = serde_json::from_slice(&swapped.stdout).unwrap();
    assert_eq!(doc["account_home"], account.to_str().unwrap());
    // The account's own production folders are refused whatever HOME says.
    for path in [physical.join(".agents-router"), physical.join(".agt/x")] {
        let refused = resolve(&root, TEST, &["--test-agt-home", path.to_str().unwrap()]);
        assert_eq!(refused.status.code(), Some(1), "{}", path.display());
        assert!(String::from_utf8_lossy(&refused.stderr).contains("overlaps the release's"));
    }
    for (name, path) in [
        ("AGT_HOME", root.0.join("home/.agents-router")),
        ("AGT_BIN_DIR", root.0.join("home/.local/bin")),
        (
            "AGT_APP_SUPPORT",
            root.0
                .join("home/Library/Application Support/Agents Router"),
        ),
    ] {
        let refused = root
            .command()
            .env(name, &path)
            .arg("--resolve-paths")
            .arg(TEST)
            .output()
            .unwrap();
        assert_eq!(refused.status.code(), Some(1), "{name}");
        assert!(
            String::from_utf8_lossy(&refused.stderr).contains("overlaps the release's"),
            "{name}"
        );
    }
    assert_eq!(fs::read_dir(&root.0).unwrap().count(), 1);
}
