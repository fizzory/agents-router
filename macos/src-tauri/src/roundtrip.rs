//! Developer-only real-agt contract check, always in a child with an isolated HOME.
use crate::{
    client::{AgtClient, CommandOutcome},
    models::{Level, Snapshot},
    settings::SettingsCore,
};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn script(path: &Path, body: &str) -> Result<(), String> {
    fs::write(path, format!("#!/bin/sh\nset -eu\n{body}\n")).map_err(|e| e.to_string())?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())
}

pub fn run() -> Result<(), String> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scratch = Scratch(
        std::env::temp_dir().join(format!("agt-app-roundtrip-{}-{nonce}", std::process::id())),
    );
    fs::create_dir(&scratch.0).map_err(|e| e.to_string())?;
    let home = scratch.0.join("home");
    let agt = home.join(".agents-router");
    let bin = scratch.0.join("bin");
    for path in [
        &home,
        &agt,
        &bin,
        &home.join(".claude"),
        &home.join(".codex"),
        &scratch.0.join("workspace/.agt"),
    ] {
        fs::create_dir_all(path).map_err(|e| e.to_string())?;
    }
    // Resolve Python before replacing PATH. All subsequent executables are pinned or fake.
    let python = std::process::Command::new("python3")
        .args(["-c", "import sys; print(sys.executable)"])
        .output()
        .map_err(|e| e.to_string())?;
    if !python.status.success() {
        return Err("python3 is required for --roundtrip".into());
    }
    let python = String::from_utf8(python.stdout)
        .map_err(|e| e.to_string())?
        .trim()
        .to_owned();
    let quote = crate::terminal::posix_quote;
    script(
        &bin.join("agt"),
        &format!(
            "export PYTHONPATH={}\nexec {} -m agents_router \"$@\"",
            quote(repo.join("src").to_str().unwrap()),
            quote(&python)
        ),
    )?;
    for (name, kind) in [
        ("claude", "claude"),
        ("codex", "codex"),
        ("agy", "agy"),
        ("kiro-cli", "kiro"),
    ] {
        script(
            &bin.join(name),
            &format!(
                "exec {} {} {} \"$@\"",
                quote(&python),
                quote(repo.join("tests/fake_host.py").to_str().unwrap()),
                quote(kind)
            ),
        )?;
    }
    fs::write(agt.join("settings.toml"), "max_jobs = 6\n").map_err(|e| e.to_string())?;
    let mut command =
        std::process::Command::new(std::env::current_exe().map_err(|e| e.to_string())?);
    command
        .env_clear()
        .args(["--roundtrip-isolated"])
        .arg(&scratch.0)
        .current_dir(&home)
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .env("AGT_HOME", &agt)
        .env("AGT_CONFIG_DIR", &agt)
        .env("CLAUDE_CONFIG_DIR", home.join(".claude"))
        .env("CODEX_HOME", home.join(".codex"))
        .env(
            "PATH",
            format!("{}:/usr/bin:/bin:/usr/sbin:/sbin", bin.display()),
        )
        .env("AGT_APP_ROUNDTRIP_ROOT", &scratch.0)
        .env("AGT_SPAWN_RECORD", scratch.0.join("spawns.jsonl"));
    for (variable, kind) in [
        ("AGT_LAUNCHCTL", "launchctl"),
        ("AGT_OSASCRIPT", "osascript"),
    ] {
        command.env(
            variable,
            serde_json::to_string(&[
                &python,
                repo.join("tests/fake_launchd.py").to_str().unwrap(),
                kind,
            ])
            .unwrap(),
        );
    }
    let output = command.output().map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "roundtrip child failed: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    print!("{}", String::from_utf8_lossy(&output.stdout));
    Ok(())
}

pub async fn isolated(root: &Path) -> Result<(), String> {
    // Refuse this internal mode unless the parent supplied the complete scratch environment.
    for (name, expected) in [
        ("AGT_APP_ROUNDTRIP_ROOT", root.to_path_buf()),
        ("HOME", root.join("home")),
        ("AGT_HOME", root.join("home/.agents-router")),
        ("CLAUDE_CONFIG_DIR", root.join("home/.claude")),
        ("CODEX_HOME", root.join("home/.codex")),
    ] {
        if std::env::var_os(name).map(PathBuf::from) != Some(expected) {
            return Err(format!("internal roundtrip requires isolated {name}"));
        }
    }
    let core = SettingsCore::new(
        AgtClient::new(root.join("bin/agt"), Some(root.join("home/.agents-router")))
            .map_err(|e| e.to_string())?,
        root.join("home"),
    );
    for (level, workspace, value, own_file) in [
        (
            Level::User,
            None,
            "2",
            root.join("home/.agents-router/settings.toml"),
        ),
        (
            Level::Workspace,
            Some(root.join("workspace")),
            "1",
            root.join("workspace/.agt/settings.toml"),
        ),
    ] {
        let level_name = match level {
            Level::User => "user",
            _ => "workspace",
        };
        let before = fs::read(&own_file).ok();
        let config = core
            .show(workspace.clone())
            .await
            .map_err(|e| e.to_string())?;
        let original = config
            .settings
            .iter()
            .find(|r| r.key == "max_jobs")
            .ok_or("missing max_jobs")?
            .levels[level_name]
            .clone();
        let write = core
            .write(
                "max_jobs".into(),
                Some(value.into()),
                level.clone(),
                workspace.clone(),
                original.revision.clone(),
                None,
            )
            .await
            .map_err(|e| e.to_string())?;
        let CommandOutcome::Data(data) = write else {
            return Err(format!("{level_name}: write refused"));
        };
        let Snapshot::ConfigWrite(write) = *data else {
            return Err("wrong write model".into());
        };
        let shown = core
            .show(workspace.clone())
            .await
            .map_err(|e| e.to_string())?;
        let row = shown
            .settings
            .iter()
            .find(|r| r.key == "max_jobs")
            .ok_or("missing max_jobs after write")?;
        if row.levels[level_name].value != serde_json::json!(value.parse::<u64>().unwrap()) {
            return Err(format!("{level_name}: readback differs"));
        }
        let previous = write.previous;
        let undo_value = if previous.present {
            Some(
                previous
                    .value
                    .as_u64()
                    .ok_or("max_jobs previous isn't an integer")?
                    .to_string(),
            )
        } else {
            None
        };
        let undo = core
            .write(
                "max_jobs".into(),
                undo_value,
                previous.level,
                workspace.clone(),
                write.revision,
                None,
            )
            .await
            .map_err(|e| e.to_string())?;
        if !matches!(undo, CommandOutcome::Data(_)) {
            return Err(format!("{level_name}: undo refused"));
        }
        let after = core
            .show(workspace.clone())
            .await
            .map_err(|e| e.to_string())?;
        let reverted = &after
            .settings
            .iter()
            .find(|r| r.key == "max_jobs")
            .unwrap()
            .levels[level_name];
        if reverted.present != original.present || reverted.value != original.value {
            return Err(format!("{level_name}: undo differs from previous"));
        }
        if let Some(before) = before {
            if fs::read(&own_file).map_err(|e| e.to_string())? != before {
                return Err("undo changed unrelated bytes".into());
            }
        }
        let bytes = fs::read(&own_file).ok();
        let refused = core
            .write(
                "max_jobs".into(),
                Some("bad-value".into()),
                level,
                workspace,
                reverted.revision.clone(),
                None,
            )
            .await
            .map_err(|e| e.to_string())?;
        if !matches!(refused, CommandOutcome::Refused(ref e) if e.key.as_deref() == Some("max_jobs") && !e.message.is_empty())
        {
            return Err("bad value did not return a JSON error".into());
        }
        if fs::read(&own_file).ok() != bytes {
            return Err("bad value changed settings".into());
        }
        println!("OK: {level_name} max_jobs set, read back, undo from previous; bad value refused without writing");
    }
    println!("OK: isolated real-agt roundtrip (user + workspace)");
    Ok(())
}
