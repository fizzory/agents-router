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
