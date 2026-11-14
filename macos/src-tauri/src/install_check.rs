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
    let intent = install::move_application(&app, &move_paths, &[])?;
    let moved = move_paths.applications[0].join("Agents Router.app");
    require(
        Path::new(&intent[2]) == moved && moved.join("Contents").exists(),
        "move copy missing or renamed",
    )?;
    fs::write(moved.join("Contents/existing"), "kept").map_err(err)?;
    let staging = |dir: &Path| -> Result<bool> {
        Ok(fs::read_dir(dir)
            .map_err(err)?
            .filter_map(|e| e.ok())
            .any(|e| {
                e.file_name()
                    .to_string_lossy()
                    .starts_with(".Agents-Router-")
            }))
    };
    require(
        install::move_application(&app, &move_paths, &[])
            .is_err_and(|e| e.contains("already exists"))
            && fs::read_to_string(moved.join("Contents/existing")).map_err(err)? == "kept"
            && !staging(&move_paths.applications[0])?,
        "move replaced an existing app or left staging behind",
    )?;
    // A test build keeps its own name, beside the release app, and is refused the same way.
    let test_app = root.join("Applications/Agents Router Test.app");
    fs::create_dir_all(test_app.join("Contents/Resources")).map_err(err)?;
    let mut test_move = move_paths.clone();
    test_move.isolated = Some(install::Isolated {
        home: root.join("move test home"),
        config: root.join("move test config"),
        exclude: root.join("move test home").into_os_string(),
    });
    // Its chosen folders travel as launch arguments; the release's argv has none.
    let relaunch = [
        "--test-agt-home".to_string(),
        "/scratch/agt home".to_string(),
    ];
    let test_intent = install::move_application(&test_app, &test_move, &relaunch)?;
    let test_moved = move_paths.applications[0].join("Agents Router Test.app");
    require(
        intent.len() == 3 && test_intent[3..] == ["--args", "--test-agt-home", "/scratch/agt home"],
        "move relaunch argv lost or added test-build folders",
    )?;
    require(
        Path::new(&test_intent[2]) == test_moved
            && test_moved.join("Contents").exists()
            && moved.join("Contents/existing").exists(),
        "test build did not move under its own name",
    )?;
    require(
        install::move_application(&test_app, &test_move, &[])
            .is_err_and(|e| e.contains("already exists"))
            && !staging(&move_paths.applications[0])?,
        "move replaced an existing test app",
    )?;
    require(
        install::move_application(&root.join("Applications"), &test_move, &[])
            .is_err_and(|e| e == "Choose the app bundle"),
        "test build move accepted a folder that isn't an app bundle",
    )?;
    // The release lands as "Agents Router.app" whatever its bundle is called, as before.
    fs::remove_dir_all(&moved).map_err(err)?;
    let release_intent = install::move_application(&test_app, &move_paths, &[])?;
    require(
        Path::new(&release_intent[2]) == moved
            && release_intent.len() == 3
            && moved.join("Contents/Resources").exists(),
        "release move changed its destination name",
    )?;
    for path in [&moved, &test_moved, &test_app] {
        fs::remove_dir_all(path).map_err(err)?;
    }
    println!("OK: Move to Applications keeps the release's name Agents Router.app and a test build's own (Agents Router Test.app), and refuses an existing app without staging leftovers");
    fs::set_permissions(
        &move_paths.applications[0],
        fs::Permissions::from_mode(0o555),
    )
    .map_err(err)?;
    let fallback = install::move_application(&app, &move_paths, &[])?;
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
    test_build(root, &paths, &app, &two).await?;
    println!("OK: a test build installs into its own folders; its launcher exports every folder and a private HOME and drops provider folders");
    Ok(())
}
/// A test build resolved through the app's own resolver, with partial flag and env overrides.
async fn test_build(root: &Path, release: &InstallPaths, app: &Path, payload: &Path) -> Result<()> {
    let base = crate::app_paths::physical(root)?;
    let support = base.join("test-support");
    let agt_home = base.join("test agt home");
    let applications = std::env::join_paths(&release.applications).map_err(err)?;
    let lookup = |name: &str| match name {
        "HOME" => Some(base.join("home").into_os_string()),
        "AGT_HOME" => Some(agt_home.clone().into_os_string()),
        "AGT_APPLICATIONS_DIRS" => Some(applications.clone()),
        _ => None,
    };
    let args = [
        "--test-app-support".into(),
        support.clone().into_os_string(),
    ];
    let resolved =
        crate::app_paths::AppPaths::resolve(crate::app_paths::TEST_IDENTIFIER, &args, lookup)?;
    // The release scratch install's own agt home is the production one for this HOME.
    require(
        crate::app_paths::AppPaths::resolve(
            crate::app_paths::TEST_IDENTIFIER,
            &[
                "--test-agt-home".into(),
                base.join("home/.agents-router").into_os_string(),
            ],
            lookup,
        )
        .is_err_and(|e| e.contains("overlaps")),
        "test build accepted the production agt home",
    )?;
    resolved.prepare()?;
    let paths = InstallPaths::from_app(&resolved)?;
    require(
        paths.bin == support.join("cli-bin")
            && paths.agt_home == agt_home
            && paths.isolated
                == Some(install::Isolated {
                    home: support.join("home"),
                    config: agt_home.clone(),
                    // The private HOME, the launch HOME and the account's home.
                    exclude: std::env::join_paths([
                        support.join("home"),
                        base.join("home"),
                        crate::app_paths::physical(&crate::app_paths::account_home()?)?,
                    ])
                    .map_err(err)?,
                }),
        "test build folders differ from the resolver",
    )?;
    let release_link = fs::read_link(release.public()).ok();
    install::install(&paths, app, payload, false).await?;
    require(
        install::owns_public(&paths)
            && install::launcher_current(&paths)
            && fs::read_link(release.public()).ok() == release_link,
        "test build install missed its launcher or touched the release CLI",
    )?;
    // agt sees only the private HOME, so the launcher's AGT_EXCLUDE_HOMES keeps
    // the person's own HOME and its .agt/ from becoming a workspace. The
    // caller's folder stays: below that HOME, a folder is its own workspace.
    let person = base.join("home");
    fs::create_dir_all(person.join(".agt")).map_err(err)?;
    fs::create_dir_all(person.join("Documents")).map_err(err)?;
    fs::write(person.join(".agt/settings.toml"), "max_jobs = 7\n").map_err(err)?;
    fs::create_dir_all(&agt_home).map_err(err)?;
    fs::write(agt_home.join("settings.toml"), "max_jobs = 3\n").map_err(err)?;
    let theirs = person.join(".agt/settings.toml");
    let ours = agt_home.join("settings.toml");
    let documents = person.join("Documents");
    let show = |cwd: &Path| -> Result<(Value, Vec<String>)> {
        let out = std::process::Command::new(paths.public())
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", &person)
            .current_dir(cwd)
            .args(["config", "show", "--json"])
            .output()
            .map_err(err)?;
        let doc: Value = serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("{e}: {}", String::from_utf8_lossy(&out.stderr)))?;
        let files = doc["files"]
            .as_array()
            .ok_or("config show has no files")?
            .iter()
            .filter_map(|f| f["file"].as_str().map(String::from))
            .collect();
        require(
            out.status.success(),
            &format!("config show failed in {}", cwd.display()),
        )?;
        Ok((doc, files))
    };
    let s = |p: &Path| p.to_string_lossy().into_owned();
    let (doc, files) = show(&person)?;
    require(
        doc["workspace"].is_null() && files == vec![s(&ours)],
        &format!("test launcher in the person's HOME read other settings: {doc}"),
    )?;
    let (doc, files) = show(&documents)?;
    require(
        doc["workspace"] == s(&documents).as_str()
            && files.first() == Some(&s(&ours))
            && !files.contains(&s(&theirs)),
        &format!("test launcher below the person's HOME read its .agt/: {doc}"),
    )?;
    // A Settings workspace write runs there too: it lands in that folder's .agt/.
    let set = std::process::Command::new(paths.public())
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", &person)
        .current_dir(&documents)
        .args([
            "config",
            "set",
            "subagents.prefer_roles",
            "false",
            "--workspace",
        ])
        .output()
        .map_err(err)?;
    require(
        set.status.success()
            && fs::read_to_string(documents.join(".agt/settings.toml")).map_err(err)?
                == "[subagents]\nprefer_roles = false\n"
            && fs::read_to_string(&theirs).map_err(err)? == "max_jobs = 7\n",
        &format!(
            "test launcher wrote a workspace setting elsewhere: {}",
            String::from_utf8_lossy(&set.stderr)
        ),
    )?;
    fs::remove_dir_all(documents.join(".agt")).map_err(err)?;
    // A dotfiles checkout at that HOME makes it the checkout's root: still never a workspace.
    fs::create_dir_all(person.join(".git")).map_err(err)?;
    for cwd in [&person, &documents] {
        let (doc, files) = show(cwd)?;
        require(
            doc["workspace"].is_null() && files == vec![s(&ours)],
            &format!(
                "test launcher in {} with ~/.git read other settings: {doc}",
                cwd.display()
            ),
        )?;
    }
    fs::remove_dir(person.join(".git")).map_err(err)?;
    fs::remove_file(person.join(".agt/settings.toml")).map_err(err)?;
    // A link planted under the support folder after launch stops the install
    // before it writes through it.
    let outside = base.join("outside-bin");
    fs::create_dir_all(&outside).map_err(err)?;
    let real_bin = paths.support.join("bin-kept");
    fs::rename(paths.support.join("bin"), &real_bin).map_err(err)?;
    symlink(&outside, paths.support.join("bin")).map_err(err)?;
    require(
        install::install(&paths, app, payload, false)
            .await
            .is_err_and(|e| e.contains("is a symlink"))
            && fs::read_dir(&outside).map_err(err)?.next().is_none(),
        "test build install wrote through a linked support/bin",
    )?;
    fs::remove_file(paths.support.join("bin")).map_err(err)?;
    fs::rename(&real_bin, paths.support.join("bin")).map_err(err)?;
    script(
        &paths.current().ok_or("No test runtime")?.join("bin/python3"),
        "printf '%s\\n' \"$AGT_APP_SUPPORT\" \"$AGT_BIN_DIR\" \"$AGT_HOME\" \"$AGT_CONFIG_DIR\" \"$HOME\" \"$CFFIXED_USER_HOME\" \"${CLAUDE_CONFIG_DIR-unset}\" \"${CODEX_HOME-unset}\" \"${GEMINI_CLI_HOME-unset}\" \"$*\"",
    )?;
    let out = std::process::Command::new(paths.public())
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", base.join("home"))
        .env("AGT_HOME", base.join("home/.agents-router"))
        .env("CLAUDE_CONFIG_DIR", base.join("home/.claude"))
        .env("CODEX_HOME", base.join("home/.codex"))
        .env("GEMINI_CLI_HOME", base.join("home/.gemini"))
        .args(["usage", "--json"])
        .output()
        .map_err(err)?;
    let text = String::from_utf8_lossy(&out.stdout);
    let s = |p: &Path| p.to_string_lossy().into_owned();
    let expected = [
        s(&support),
        s(&support.join("cli-bin")),
        s(&agt_home),
        s(&agt_home),
        s(&support.join("home")),
        s(&support.join("home")),
        "unset".into(),
        "unset".into(),
        "unset".into(),
        "-I -m agents_router usage --json".into(),
    ];
    require(
        out.status.success() && text.lines().collect::<Vec<_>>() == expected,
        &format!("test launcher environment differs: {text}"),
    )?;
    let mut moved = paths.clone();
    moved.agt_home = base.join("other agt home");
    require(
        !install::launcher_current(&moved),
        "a launcher with other baked folders counts as current",
    )?;
    Ok(())
}
