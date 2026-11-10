//! Settings operations share the app's argument-array client. No shell or executable IPC.
use crate::{
    client::{AgtClient, ClientError, CommandOutcome, ReadReply, Request},
    models::{CommandKind, ConfigShow, Level, Snapshot},
};
use serde_json::{json, Value};
use std::{collections::HashSet, path::PathBuf, sync::Mutex};

pub const AUTOSTART_LABEL: &str = "io.github.watcharagorn.agents-router.app";

pub fn autostart_plugin<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    tauri_plugin_autostart::Builder::new()
        .app_name(AUTOSTART_LABEL)
        .macos_launcher(tauri_plugin_autostart::MacosLauncher::LaunchAgent)
        .build()
}

pub struct SettingsCore {
    client: AgtClient,
    home: PathBuf,
    files: Mutex<HashSet<PathBuf>>,
}
impl SettingsCore {
    pub fn new(client: AgtClient, home: PathBuf) -> Self {
        Self {
            client,
            home,
            files: Mutex::new(HashSet::new()),
        }
    }
    fn cwd(&self, workspace: Option<PathBuf>) -> Result<PathBuf, ClientError> {
        let path = workspace.unwrap_or_else(|| self.home.clone());
        if !path.is_absolute() || !path.is_dir() {
            return Err(ClientError::InvalidRequest(
                "Choose an existing absolute workspace folder".into(),
            ));
        }
        path.canonicalize()
            .map_err(|e| ClientError::InvalidRequest(e.to_string()))
    }
    pub async fn show(&self, workspace: Option<PathBuf>) -> Result<ConfigShow, ClientError> {
        let mut request = Request::new(CommandKind::ConfigShow, Some(self.cwd(workspace)?));
        request.arguments.push("--all".into());
        let reply = self
            .client
            .read(request)
            .await?
            .await
            .map_err(|e| ClientError::Execution(e.to_string()))?;
        match reply {
            ReadReply::Fresh {
                result: Ok(CommandOutcome::Data(data)),
                ..
            } => {
                if let Snapshot::Config(config) = *data {
                    let mut files = self.files.lock().expect("settings files");
                    files.extend(config.files.iter().map(|file| PathBuf::from(&file.file)));
                    for row in &config.settings {
                        files.extend(row.file.iter().map(PathBuf::from));
                        files.extend(row.levels.values().map(|v| PathBuf::from(&v.file)));
                    }
                    Ok(config)
                } else {
                    Err(ClientError::InvalidResponse("Expected config rows".into()))
                }
            }
            ReadReply::Fresh {
                result: Err(error), ..
            } => Err(error),
            ReadReply::Superseded { .. } => Err(ClientError::InvalidRequest(
                "A newer settings read is running".into(),
            )),
            _ => Err(ClientError::InvalidResponse("Settings read refused".into())),
        }
    }
    pub async fn write(
        &self,
        key: String,
        value: Option<String>,
        level: Level,
        workspace: Option<PathBuf>,
        revision: String,
        user_said: Option<String>,
    ) -> Result<CommandOutcome, ClientError> {
        if key.is_empty()
            || key.starts_with('-')
            || !key
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "._".contains(c))
        {
            return Err(ClientError::InvalidRequest("Invalid settings key".into()));
        }
        if !matches!(level, Level::User) && workspace.is_none() {
            return Err(ClientError::InvalidRequest(
                "Choose a workspace before editing this level".into(),
            ));
        }
        if revision.is_empty() {
            return Err(ClientError::InvalidRequest(
                "Read the file revision before saving".into(),
            ));
        }
        let mut request = Request::new(
            if value.is_some() {
                CommandKind::ConfigSet
            } else {
                CommandKind::ConfigUnset
            },
            Some(self.cwd(workspace)?),
        );
        request.arguments.push(key);
        request.arguments.push(
            match level {
                Level::User => "--user",
                Level::Workspace => "--workspace",
                Level::Local => "--local",
            }
            .into(),
        );
        request
            .arguments
            .extend(["--if-unchanged".into(), revision]);
        if let Some(words) = user_said {
            request.arguments.extend(["--user-said".into(), words]);
        }
        if let Some(value) = value {
            request.arguments.insert(1, value);
        }
        self.client.mutate(request).await
    }
    pub async fn revert(
        &self,
        entry: String,
        workspace: Option<PathBuf>,
        user_said: Option<String>,
    ) -> Result<CommandOutcome, ClientError> {
        if entry.starts_with('-') || entry.is_empty() {
            return Err(ClientError::InvalidRequest("Invalid log entry".into()));
        }
        let mut request = Request::new(CommandKind::ConfigRevert, Some(self.cwd(workspace)?));
        request.arguments.push(entry);
        if let Some(words) = user_said {
            request.arguments.extend(["--user-said".into(), words]);
        }
        self.client.mutate(request).await
    }
    pub fn checked_file(&self, file: &str, line: u64) -> Result<PathBuf, String> {
        let path = PathBuf::from(file);
        if line == 0
            || !path.is_absolute()
            || !self.files.lock().expect("settings files").contains(&path)
        {
            return Err(
                "Read settings first and choose an agt-resolved file and positive line".into(),
            );
        }
        Ok(path)
    }
}

/// Keep agt's messages (including exit 3) as data; transport failures are IPC errors.
pub fn outcome_json(outcome: CommandOutcome) -> Result<Value, String> {
    match outcome {
        CommandOutcome::Data(data) => serde_json::to_value(data).map_err(|e| e.to_string()),
        CommandOutcome::Refused(error) => Ok(json!({"ok":false, "exit_code":2, "error":error})),
        CommandOutcome::NeedsUserWords(error) => {
            Ok(json!({"ok":false, "exit_code":3, "error":error}))
        }
        CommandOutcome::NeedsChoice(choice) => {
            serde_json::to_value(choice).map_err(|e| e.to_string())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    use std::{fs, os::unix::fs::PermissionsExt, path::Path};

    #[tokio::test]
    async fn config_calls_use_workspace_cwd_and_user_reads_use_home() {
        let scratch = Scratch::new("settings-cwd");
        let workspace = scratch.0.join("workspace with spaces");
        fs::create_dir(&workspace).unwrap();
        let launcher = scratch.0.join("agt");
        let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/app");
        let config_fixture = scratch.0.join("config.json");
        fs::write(
            &config_fixture,
            include_str!("../../../tests/fixtures/app/config-show.json")
                .replace("<ROOT>", scratch.0.to_str().unwrap()),
        )
        .unwrap();
        let calls = scratch.0.join("calls");
        fs::write(&launcher, format!("#!/bin/sh\npwd >> {}\nprintf '%s\\n' \"$@\" >> {}\ncase \"$2\" in show) /bin/cat {};; *) /bin/cat {}/config-set.json;; esac\n",
            crate::terminal::posix_quote(calls.to_str().unwrap()), crate::terminal::posix_quote(calls.to_str().unwrap()),
            crate::terminal::posix_quote(config_fixture.to_str().unwrap()), crate::terminal::posix_quote(fixtures.to_str().unwrap()))).unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let core = SettingsCore::new(
            AgtClient::new(launcher, Some(scratch.0.clone())).unwrap(),
            scratch.0.clone(),
        );
        core.show(None).await.unwrap();
        core.show(Some(workspace.clone())).await.unwrap();
        for level in [Level::Workspace, Level::Local] {
            core.write(
                "max_jobs".into(),
                Some("2".into()),
                level,
                Some(workspace.clone()),
                "revision".into(),
                None,
            )
            .await
            .unwrap();
        }
        core.write(
            "max_jobs".into(),
            None,
            Level::Local,
            Some(workspace.clone()),
            "revision".into(),
            None,
        )
        .await
        .unwrap();
        core.revert("2026-11:1".into(), Some(workspace.clone()), None)
            .await
            .unwrap();
        let text = fs::read_to_string(calls).unwrap();
        assert!(text.starts_with(scratch.0.canonicalize().unwrap().to_str().unwrap()));
        assert_eq!(
            text.lines()
                .filter(|l| *l == workspace.canonicalize().unwrap().to_str().unwrap())
                .count(),
            5
        );
        assert!(text.contains("--local\n--if-unchanged\nrevision"));
        assert!(core
            .write(
                "max_jobs".into(),
                Some("2".into()),
                Level::Workspace,
                None,
                "revision".into(),
                None
            )
            .await
            .is_err());
        assert!(core.checked_file("/etc/passwd", 1).is_err());
        let config = core.show(None).await.unwrap();
        assert_eq!(
            core.checked_file(&config.files[0].file, 2).unwrap(),
            PathBuf::from(&config.files[0].file)
        );
        assert!(core.checked_file(&config.files[0].file, 0).is_err());
    }

    #[test]
    fn autostart_writes_and_removes_plist_in_child_home() {
        // No process-global HOME mutation: run the actual plugin in a child with a temp HOME.
        let scratch = Scratch::new("autostart");
        fs::create_dir(scratch.0.join("Library")).unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "settings::tests::autostart_child", "--nocapture"])
            .env("HOME", &scratch.0)
            .env("AGT_APP_AUTOSTART_TEST_HOME", &scratch.0)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn autostart_child() {
        let Some(home) = std::env::var_os("AGT_APP_AUTOSTART_TEST_HOME") else {
            return;
        };
        assert_eq!(std::env::var_os("HOME"), Some(home.clone()));
        use tauri_plugin_autostart::ManagerExt;
        let app = tauri::test::mock_builder()
            .plugin(autostart_plugin())
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap();
        let plist = PathBuf::from(home)
            .join("Library/LaunchAgents")
            .join(format!("{AUTOSTART_LABEL}.plist"));
        assert!(!app.autolaunch().is_enabled().unwrap());
        app.autolaunch().enable().unwrap();
        assert!(app.autolaunch().is_enabled().unwrap());
        let data = fs::read_to_string(&plist).unwrap();
        assert!(data.contains(&format!("<string>{AUTOSTART_LABEL}</string>")));
        assert!(!data.contains("agents-router.share"));
        let triggers = include_str!("../../../src/agents_router/share/triggers.py");
        let trigger_label = triggers
            .lines()
            .find_map(|l| l.strip_prefix("LABEL = \""))
            .unwrap()
            .trim_end_matches('"');
        assert_ne!(AUTOSTART_LABEL, trigger_label);
        app.autolaunch().disable().unwrap();
        assert!(!plist.exists());
        assert!(!app.autolaunch().is_enabled().unwrap());
    }
}
