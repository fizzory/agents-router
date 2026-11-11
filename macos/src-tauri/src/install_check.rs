//! Real agt lifecycle checks. The parent clears the environment; all writes stay in scratch.
use crate::install::{self, InstallPaths};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::{symlink, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};
fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}
type Result<T> = std::result::Result<T, String>;
struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn script(path: &Path, body: &str) -> Result<()> {
    fs::write(path, format!("#!/bin/sh\nset -eu\n{body}\n")).map_err(err)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).map_err(err)
}
fn require(value: bool, message: &str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(message.into())
    }
}
/// Unlike app first launch, a developer check must opt into every destination.
pub async fn from_app(app: &Path) -> Result<()> {
    for name in ["AGT_APP_SUPPORT", "AGT_BIN_DIR", "AGT_HOME"] {
        require(
            std::env::var_os(name).is_some(),
            &format!("--install-cli --app requires explicit {name}"),
        )?;
    }
    let mut paths = InstallPaths::discover()?;
    let app = app
        .canonicalize()
        .map_err(|e| format!("Missing app bundle {}: {e}", app.display()))?;
    // The checker also accepts a writable copied app under the temporary root.
    // Production app location checks and mounted/translocated refusal stay intact.
    let temporary = std::env::temp_dir().canonicalize().map_err(err)?;
    if app.starts_with(temporary) {
        paths
            .applications
            .push(app.parent().ok_or("App has no parent")?.to_path_buf());
    }
    let installed = install::install_bundle(&paths, &app, false).await?;
    println!(
        "OK: installed agt {} from {}",
        installed.version,
        app.display()
    );
    println!(
        "runtime: {}",
        paths.current().ok_or("No installed runtime")?.display()
    );
    println!("launcher: {}", paths.launcher().display());
    println!("public: {}", paths.public().display());
    println!("AGT_HOME: {}", paths.agt_home.display());
    if let Some(foreign) = installed.foreign {
        println!("Kept foreign public CLI: {}", foreign.target.display());
    }
    Ok(())
}
pub fn run() -> Result<()> {
    let root = Scratch(std::env::temp_dir().join(format!(
        "agt-app-install-{}-{}",
        std::process::id(),
        crate::app::now()
    )));
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(err)?;
    let python = std::process::Command::new("python3")
        .args(["-c", "import sys; print(sys.executable)"])
        .output()
        .map_err(err)?;
    require(python.status.success(), "python3 is required")?;
    let python = String::from_utf8(python.stdout)
        .map_err(err)?
        .trim()
        .to_owned();
    for dir in [
        "home/.agents-router/state",
        "home/.agents-router/jobs",
        "home/.claude",
        "home/.codex",
        "Applications",
        "User Applications",
        "fake-bin",
    ] {
        fs::create_dir_all(root.0.join(dir)).map_err(err)?;
    }
    let quote = crate::terminal::posix_quote;
    for (name, kind) in [
        ("claude", "claude"),
        ("codex", "codex"),
        ("agy", "agy"),
        ("kiro-cli", "kiro"),
    ] {
        script(
            &root.0.join("fake-bin").join(name),
            &format!(
                "exec {} {} {} \"$@\"",
                quote(&python),
                quote(&repo.join("tests/fake_host.py").to_string_lossy()),
                quote(kind)
            ),
        )?;
    }
    let output = std::process::Command::new(std::env::current_exe().map_err(err)?)
        .env_clear()
        .arg("--install-cli-isolated")
        .arg(&root.0)
        .current_dir(root.0.join("home"))
        .env("HOME", root.0.join("home"))
        .env("USERPROFILE", root.0.join("home"))
        .env("AGT_HOME", root.0.join("home/.agents-router"))
        .env("AGT_CONFIG_DIR", root.0.join("home/.agents-router"))
        .env("CLAUDE_CONFIG_DIR", root.0.join("home/.claude"))
        .env("CODEX_HOME", root.0.join("home/.codex"))
        .env("AGT_APP_SUPPORT", root.0.join("support"))
        .env("AGT_BIN_DIR", root.0.join("public-bin"))
        .env(
            "AGT_APPLICATIONS_DIRS",
            std::env::join_paths([
                root.0.join("Applications"),
                root.0.join("User Applications"),
            ])
            .map_err(err)?,
        )
        .env("AGT_APP_INSTALL_ROOT", &root.0)
        .env("AGT_APP_CHECK_PYTHON", python)
        .env(
            "PATH",
            format!("{}:/usr/bin:/bin", root.0.join("fake-bin").display()),
        )
        .env("AGT_SPAWN_RECORD", root.0.join("spawns.jsonl"))
        .output()
        .map_err(err)?;
    if !output.status.success() {
        return Err(format!(
            "install check failed: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    print!("{}", String::from_utf8_lossy(&output.stdout));
    Ok(())
}
fn payload(root: &Path, version: &str) -> Result<PathBuf> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(err)?;
    let runtime = root.join(format!("payload-{version}"));
    fs::create_dir_all(runtime.join("bin")).map_err(err)?;
    for dir in ["agents", "skills", "commands", "hooks", ".claude-plugin"] {
        install::copy_tree(&repo.join(dir), &runtime.join("plugin").join(dir))
            .or_else(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    fs::create_dir_all(runtime.join("plugin"))?;
                    install::copy_tree(&repo.join(dir), &runtime.join("plugin").join(dir))
                } else {
                    Err(e)
                }
            })
            .map_err(err)?;
    }
    for file in ["plugin.json", "mcp_config.json"] {
        fs::copy(repo.join(file), runtime.join("plugin").join(file)).map_err(err)?;
    }
    let meta = runtime.join("plugin/.claude-plugin/plugin.json");
    let mut data: Value = serde_json::from_slice(&fs::read(&meta).map_err(err)?).map_err(err)?;
    data["version"] = version.into();
    fs::write(meta, serde_json::to_vec(&data).unwrap()).map_err(err)?;
    fs::write(runtime.join("agt-bundle.json"),serde_json::to_vec(&json!({"schema_version":1,"source":"app","app_version":version,"runtime_version":version,"python_version":"3.13.0","plugin_path":"plugin"})).unwrap()).map_err(err)?;
    // The shim alone handles version validation; all other commands use the repo's real agt.
    let python = std::env::var("AGT_APP_CHECK_PYTHON").map_err(err)?;
    let quote = crate::terminal::posix_quote;
    script(&runtime.join("bin/python3"),&format!("if [ \"${{4-}}\" = --version ]; then printf 'agt {version}\\n'; exit 0; fi\n[ \"$1\" = -I ] && shift\nexport PYTHONPATH={}\nexec {} \"$@\"",quote(&repo.join("src").to_string_lossy()),quote(&python)))?;
    Ok(runtime)
}
fn job(
    paths: &InstallPaths,
    id: &str,
    state: &str,
    account: Option<&str>,
    runtime: &Path,
) -> Result<()> {
    let dir = paths.agt_home.join("jobs").join(id);
    fs::create_dir_all(&dir).map_err(err)?;
    let doc = json!({"job_id":id,"state":state,"runtime":"codex","account":account,"assigned_account":account,"updated_at":crate::app::now(),"runner_pid":std::process::id(),"install_runtime":runtime});
    for file in ["status.json", "spec.json"] {
        fs::write(dir.join(file), serde_json::to_vec(&doc).unwrap()).map_err(err)?;
    }
    Ok(())
}
pub async fn isolated(root: &Path) -> Result<()> {
    for (name, path) in [
        ("HOME", root.join("home")),
        ("AGT_HOME", root.join("home/.agents-router")),
        ("AGT_APP_SUPPORT", root.join("support")),
        ("AGT_BIN_DIR", root.join("public-bin")),
        ("AGT_APP_INSTALL_ROOT", root.to_path_buf()),
    ] {
        require(
            std::env::var_os(name).map(PathBuf::from) == Some(path),
            "Internal check requires isolated paths",
        )?;
    }
    let paths = InstallPaths::discover()?;
    let app = root.join("Applications/Agents Router.app");
    fs::create_dir_all(app.join("Contents/Resources")).map_err(err)?;
    let one = payload(root, "0.16.3")?;
    let two = payload(root, "0.17.0")?;
    let first = install::install(&paths, &app, &one, false).await?;
    require(
        first.first && install::owns_public(&paths),
        "fresh install missing launcher",
    )?;
    require(
        install::usage(&paths).await?["uninstall_blockers"]
            .as_array()
            .unwrap()
            .is_empty(),
        "fresh usage failed",
    )?;
    println!("OK: fresh install; stable launcher runs real agt with preserved arguments");
    require(
        install::setup_status(&paths, Duration::from_secs(10)).await?,
        "real setup dry-run didn't report planned steps",
    )?;
    require(
        install::first_launch_pending(&paths),
        "first-launch state lost",
    )?;
    install::finish_first_launch(&paths)?;
    require(
        !install::first_launch_pending(&paths),
        "first-launch completion not retained",
    )?;
    println!("OK: real setup dry-run reports planned steps; onboarding eligibility recorded only on first install");
    let old = paths.current().unwrap();
    install::install(&paths, &app, &two, false).await?;
    require(
        paths.version().as_deref() == Some("0.17.0") && old.exists(),
        "version switch lost old runtime",
    )?;
    println!("OK: second version staged, validated, current switched atomically; old runtime kept");
    let current = paths.current().unwrap();
    let interrupted = payload(root, "0.18.0")?;
    symlink(root.join("home"), interrupted.join("external-link")).map_err(err)?;
    require(
        install::install(&paths, &app, &interrupted, false)
            .await
            .is_err()
            && paths.current() == Some(current.clone()),
        "failed copy changed current",
    )?;
    install::usage(&paths).await?;
    let partial = paths.support.join("runtimes/.staging-interrupted");
    fs::create_dir_all(&partial).map_err(err)?;
    fs::write(partial.join("half"), "incomplete").map_err(err)?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .read(true)
        .open(paths.support.join("install.lock"))
        .map_err(err)?;
    lock.lock().map_err(err)?;
    require(
        install::install(&paths, &app, &two, false).await.is_err()
            && paths.current() == Some(current.clone()),
        "concurrent launch mutated install",
    )?;
    install::usage(&paths).await?;
    drop(lock);
    install::install(&paths, &app, &two, false).await?;
    require(!partial.exists(), "interrupted copy not recovered")?;
    println!("OK: interrupted copy and concurrent launch leave old state usable; stale staging recovered");
    require(
        install::install(&paths, &app, &one, false).await.is_err()
            && paths.current() == Some(current.clone()),
        "downgrade accepted",
    )?;
    install::install(&paths, &app, &one, true).await?;
    install::install(&paths, &app, &two, false).await?;
    println!("OK: downgrade refused without confirmation; confirmed downgrade succeeds");
    let trans = root.join("AppTranslocation/Agents Router.app");
    fs::create_dir_all(&trans).map_err(err)?;
    require(
        install::install(&paths, &trans, &two, false).await.is_err(),
        "translocated install accepted",
    )?;
    fs::set_permissions(&app, fs::Permissions::from_mode(0o555)).map_err(err)?;
    require(
        install::install(&paths, &app, &two, false).await.is_err(),
        "read-only app accepted",
    )?;
    fs::set_permissions(&app, fs::Permissions::from_mode(0o755)).map_err(err)?;
    println!("OK: read-only and translocated launch refused before CLI changes");
    fs::remove_file(paths.public()).map_err(err)?;
    let foreign = root.join("uv/tools/agt/bin/agt");
    fs::create_dir_all(foreign.parent().unwrap()).map_err(err)?;
    fs::create_dir(
        foreign
            .parent()
            .unwrap()
            .join("agents_router-0.16.dist-info"),
    )
    .map_err(err)?;
    script(&foreign, "exit 99")?;
    symlink(&foreign, paths.public()).map_err(err)?;
    let installed = install::install(&paths, &app, &two, false).await?;
    let found = installed.foreign.ok_or("foreign CLI not found")?;
    require(
        fs::read_link(paths.public()).map_err(err)? == foreign
            && found.source == "uv"
            && found.version == "0.16",
        "foreign CLI touched or misidentified",
    )?;
    install::replace_foreign(&paths, &found).await?;
    install::restore_cli(&paths).await?;
    require(
        fs::read_link(paths.public()).map_err(err)? == foreign,
        "explicit restore failed",
    )?;
    install::replace_foreign(&paths, &found).await?;
    require(install::owns_public(&paths), "replace failed")?;
    println!("OK: foreign uv agt left untouched; explicit Replace saves its previous target");
    let pinned = paths.support.join("runtimes/0.15.0");
    install::copy_tree(&one, &pinned).map_err(err)?;
    let redundant = paths.support.join("runtimes/0.14.0");
    install::copy_tree(&one, &redundant).map_err(err)?;
    job(&paths, "unassigned", "queued", None, &pinned)?;
    job(
        &paths,
        "removed-account",
        "running",
        Some("removed"),
        &pinned,
    )?;
    let report = install::usage(&paths).await?;
    require(
        report["jobs"]["unassigned"].as_u64().unwrap_or(0) > 0
            && report["jobs"]["removed_accounts"].as_u64().unwrap_or(0) > 0,
        "authoritative job fixtures invalid",
    )?;
    require(
        install::uninstall(&paths).await.is_err() && paths.current().is_some(),
        "uninstall ignored jobs",
    )?;
    let removed = install::cleanup(&paths).await?;
    require(
        current.exists() && old.exists() && pinned.exists() && removed.contains(&redundant),
        "cleanup deleted referenced/current/previous runtime",
    )?;
    println!("OK: uninstall consumes authoritative blockers for unassigned jobs and removed accounts; cleanup keeps current, previous, referenced runtimes");
    fs::remove_dir_all(paths.agt_home.join("jobs/unassigned")).map_err(err)?;
    fs::remove_dir_all(paths.agt_home.join("jobs/removed-account")).map_err(err)?;
    // An occupied worker lease outlives a terminal job record.
    let lease = paths.agt_home.join("state/leases/codex/test/0.lock");
    fs::create_dir_all(lease.parent().unwrap()).map_err(err)?;
    let lease_lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .read(true)
        .open(&lease)
        .map_err(err)?;
    lease_lock.lock().map_err(err)?;
    fs::write(
        lease.with_extension("owner.json"),
        serde_json::to_vec(
            &json!({"job_id":"survivor","runtime":"codex","pid":std::process::id()}),
        )
        .unwrap(),
    )
    .map_err(err)?;
    job(&paths, "survivor", "done", Some("removed"), &pinned)?;
    require(
        install::usage(&paths).await?["uninstall_blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["kind"] == "surviving_worker"),
        "surviving worker fixture invalid",
    )?;
    require(
        install::uninstall(&paths).await.is_err(),
        "uninstall ignored surviving worker",
    )?;
    drop(lease_lock);
    fs::remove_dir_all(paths.agt_home.join("state/leases")).map_err(err)?;
    println!("OK: uninstall refuses a surviving worker after its runner finishes");
    let empty = install::usage(&paths).await?;
    require(
        empty["uninstall_blockers"].as_array().unwrap().is_empty(),
        "not empty before race",
    )?;
    let admission = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(paths.agt_home.join("state/admission.lock"))
        .map_err(err)?;
    admission.lock().map_err(err)?;
    let p = paths.clone();
    let task = tokio::spawn(async move { install::uninstall(&p).await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    job(&paths, "raced", "queued", None, &current)?;
    drop(admission);
    require(
        task.await.map_err(err)?.is_err() && paths.current().is_some(),
        "uninstall didn't recheck under admission lock",
    )?;
    fs::remove_dir_all(paths.agt_home.join("jobs/raced")).map_err(err)?;
    println!("OK: uninstall rechecks under admission lock; job admitted after cached check prevents removal");
    install::uninstall(&paths).await?;
    require(
        fs::read_link(paths.public()).map_err(err)? == foreign
            && !paths.support.join("runtimes").exists()
            && paths.agt_home.join("jobs").exists(),
        "restore or uninstall failed",
    )?;
    println!("OK: uninstall without blockers removes app runtime and restores previous foreign agt; settings and AGT_HOME kept");
    let mut move_paths = paths.clone();
    move_paths.applications = vec![
        root.join("Move Applications"),
        root.join("Move User Applications"),
    ];
    fs::create_dir_all(&move_paths.applications[0]).map_err(err)?;
    let intent = install::move_application(&app, &move_paths)?;
    require(
        Path::new(&intent[2]).join("Contents").exists(),
        "move copy missing",
    )?;
    fs::remove_dir_all(&move_paths.applications[0].join("Agents Router.app")).map_err(err)?;
    fs::set_permissions(
        &move_paths.applications[0],
        fs::Permissions::from_mode(0o555),
    )
    .map_err(err)?;
    let fallback = install::move_application(&app, &move_paths)?;
    require(
        fallback[2].contains("Move User Applications"),
        "move did not use fallback",
    )?;
    fs::set_permissions(
        &move_paths.applications[0],
        fs::Permissions::from_mode(0o755),
    )
    .map_err(err)?;
    println!("OK: Move to Applications copies app and records relaunch argv; unwritable target falls back to user Applications");
    install::install(&paths, &app, &two, false).await?;
    // A real child that blocks verifies deadline handling, not a mocked return value.
    let runtime = paths.current().unwrap();
    script(&runtime.join("bin/python3"), "exec /bin/sleep 30")?;
    require(
        install::setup_status(&paths, Duration::from_millis(50))
            .await
            .is_err()
            && install::setup_unknown(&paths),
        "dry-run timeout not retained",
    )?;
    let mut failed_paths = paths.clone();
    failed_paths.support = root.join("failed-support");
    failed_paths.bin = root.join("failed-public-bin");
    let failure = install::install(&failed_paths, &app, &root.join("missing-payload"), false)
        .await
        .expect_err("broken first payload accepted");
    require(
        failure.starts_with("Couldn't install agt.")
            && failed_paths.current().is_none()
            && !failed_paths.public().exists(),
        "failed first install did not explain failure or changed CLI",
    )?;
    require(
        install::install_error("os error 28", true, Some(12345))
            .contains("Couldn't install agt. Free at least 12345 bytes"),
        "disk error missing space",
    )?;
    println!("OK: setup dry-run deadline shows no sheet and retains Setup status unknown; failed first install and full disk name action and bytes needed");
    Ok(())
}
