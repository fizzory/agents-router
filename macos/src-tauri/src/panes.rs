//! Fixed-purpose Settings operations. agt owns validation, locks, backups and guarded writes.
use crate::{
    client::{ClientError, CommandOutcome, ReadReply, Request},
    models::{AccountsList, CommandKind, Level, ShareList, Snapshot},
    settings::SettingsCore,
    terminal::TerminalAction,
};
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, path::PathBuf};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum RoleEdit {
    Enable {
        role: String,
        enabled: bool,
    },
    Reorder {
        role: String,
        providers: Vec<String>,
    },
    // These flags are entry-wide in agt; the UI labels them "All roles".
    Model {
        entry: String,
        value: Option<String>,
    },
    Effort {
        entry: String,
        value: Option<String>,
    },
    Restore {
        undo_from: String,
        if_unchanged: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RoleUndo {
    revision: String,
    level: String,
    cwd: PathBuf,
}

#[derive(Default)]
pub(crate) struct PaneState {
    accounts: Option<AccountsList>,
    sharing: Option<ShareList>,
    role_undo: HashMap<String, RoleUndo>,
}

fn invalid(message: &str) -> ClientError {
    ClientError::InvalidRequest(message.into())
}
fn operand(value: &str) -> Result<(), ClientError> {
    if value.is_empty() || value.starts_with('-') || value.contains('\0') {
        Err(invalid("Choose a nonempty agt name"))
    } else {
        Ok(())
    }
}
fn runtime(name: &str) -> Result<(), ClientError> {
    if matches!(name, "claude" | "codex" | "antigravity" | "kiro") {
        Ok(())
    } else {
        Err(invalid("Unknown provider"))
    }
}
fn level_flag(level: &Level) -> &'static str {
    match level {
        Level::User => "--user",
        Level::Workspace => "--workspace",
        Level::Local => "--local",
    }
}
pub(crate) fn valid_log_id(id: &str) -> bool {
    let Some((month, line)) = id.split_once(':') else {
        return false;
    };
    month.len() == 7
        && month.as_bytes()[4] == b'-'
        && month
            .bytes()
            .enumerate()
            .all(|(i, c)| i == 4 || c.is_ascii_digit())
        && month[5..]
            .parse::<u8>()
            .is_ok_and(|v| (1..=12).contains(&v))
        && !line.is_empty()
        && line.bytes().all(|c| c.is_ascii_digit())
        && line.parse::<u64>().is_ok_and(|v| v > 0)
}

impl SettingsCore {
    async fn pane_read(&self, request: Request) -> Result<CommandOutcome, ClientError> {
        match self
            .client
            .read(request)
            .await?
            .await
            .map_err(|e| ClientError::Execution(e.to_string()))?
        {
            ReadReply::Fresh { result, .. } => result,
            ReadReply::Superseded { .. } => Err(invalid("A newer read is running")),
        }
    }
    fn user_request(&self, command: CommandKind) -> Result<Request, ClientError> {
        Ok(Request::new(command, Some(self.cwd(None)?)))
    }
    pub async fn roles_show(
        &self,
        workspace: Option<PathBuf>,
    ) -> Result<CommandOutcome, ClientError> {
        let result = self
            .pane_read(Request::new(
                CommandKind::RolesShow,
                Some(self.cwd(workspace)?),
            ))
            .await?;
        if let CommandOutcome::Data(data) = &result {
            if let Snapshot::Roles(roles) = data.as_ref() {
                self.files
                    .lock()
                    .expect("settings files")
                    .extend(roles.files.values().flatten().map(PathBuf::from));
            }
        }
        Ok(result)
    }
    pub(crate) fn role_request(
        &self,
        edit: RoleEdit,
        level: Level,
        workspace: Option<PathBuf>,
        user_said: Option<String>,
    ) -> Result<Request, ClientError> {
        if !matches!(level, Level::User) && workspace.is_none() {
            return Err(invalid("Choose a workspace before editing this level"));
        }
        let cwd = self.cwd(workspace)?;
        let mut request = Request::new(CommandKind::RolesConfig, Some(cwd.clone()));
        let entry_flag = if matches!(&edit, RoleEdit::Model { .. }) {
            "--set-model"
        } else {
            "--set-effort"
        };
        request.arguments = match edit {
            RoleEdit::Enable { role, enabled } => {
                operand(&role)?;
                vec![if enabled { "--enable" } else { "--disable" }.into(), role]
            }
            RoleEdit::Reorder { role, providers } => {
                operand(&role)?;
                if providers.is_empty() {
                    return Err(invalid("Choose the role's providers"));
                }
                for provider in &providers {
                    operand(provider)?;
                    if provider.contains(',') {
                        return Err(invalid("One provider per chip"));
                    }
                }
                vec![role, "--order".into(), providers.join(",")]
            }
            RoleEdit::Model { entry, value } | RoleEdit::Effort { entry, value } => {
                operand(&entry)?;
                if entry.contains('=') {
                    return Err(invalid("Choose an agt provider entry"));
                }
                vec![
                    entry_flag.into(),
                    format!("{entry}={}", value.as_deref().unwrap_or("")),
                ]
            }
            RoleEdit::Restore {
                undo_from,
                if_unchanged,
            } => {
                let expected = RoleUndo {
                    revision: if_unchanged.clone(),
                    level: level_flag(&level).into(),
                    cwd,
                };
                if self
                    .panes
                    .lock()
                    .expect("pane state")
                    .role_undo
                    .get(&undo_from)
                    != Some(&expected)
                {
                    return Err(invalid(
                        "Choose structured undo fields from this session's role change",
                    ));
                }
                vec![
                    "--from".into(),
                    undo_from,
                    "--if-unchanged".into(),
                    if_unchanged,
                ]
            }
        };
        request.arguments.push(level_flag(&level).into());
        if let Some(words) = user_said {
            request.arguments.extend(["--user-said".into(), words]);
        }
        Ok(request)
    }
    pub async fn roles_config(
        &self,
        edit: RoleEdit,
        level: Level,
        workspace: Option<PathBuf>,
        user_said: Option<String>,
    ) -> Result<CommandOutcome, ClientError> {
        let request = self.role_request(edit, level, workspace, user_said)?;
        let cwd = request.workspace.clone().expect("role cwd");
        let result = self.client.mutate(request).await?;
        if let CommandOutcome::Data(data) = &result {
            if let Snapshot::RolesConfig(report) = data.as_ref() {
                if let (Some(path), Some(revision)) = (&report.undo_from, &report.if_unchanged) {
                    if PathBuf::from(path).is_absolute() && !revision.is_empty() {
                        self.panes.lock().expect("pane state").role_undo.insert(
                            path.clone(),
                            RoleUndo {
                                revision: revision.clone(),
                                level: level_flag(&report.level).into(),
                                cwd,
                            },
                        );
                    }
                }
            }
        }
        Ok(result)
    }
    pub async fn accounts_list(&self) -> Result<CommandOutcome, ClientError> {
        let result = self
            .pane_read(self.user_request(CommandKind::AccountsList)?)
            .await?;
        if let CommandOutcome::Data(data) = &result {
            if let Snapshot::Accounts(accounts) = data.as_ref() {
                self.panes.lock().expect("pane state").accounts = Some(accounts.clone());
            }
        }
        Ok(result)
    }
    pub fn account_action(
        &self,
        runtime_name: &str,
        account: &str,
        toggle: bool,
    ) -> Result<TerminalAction, ClientError> {
        runtime(runtime_name)?;
        operand(account)?;
        let panes = self.panes.lock().expect("pane state");
        let found = panes
            .accounts
            .as_ref()
            .and_then(|list| list.runtimes.iter().find(|r| r.runtime == runtime_name))
            .and_then(|r| r.accounts.iter().find(|a| a.name == account))
            .ok_or_else(|| invalid("Read Accounts and choose a listed account"))?;
        if toggle && !found.can_toggle {
            return Err(invalid(
                found
                    .toggle_reason
                    .as_deref()
                    .unwrap_or("This account cannot be toggled"),
            ));
        }
        Ok(TerminalAction::Login {
            runtime: runtime_name.into(),
            account: account.into(),
        })
    }
    pub(crate) fn account_request(
        &self,
        kind: CommandKind,
        runtime_name: String,
        account: String,
    ) -> Result<Request, ClientError> {
        runtime(&runtime_name)?;
        operand(&account)?;
        let mut request = self.user_request(kind)?;
        request.arguments = vec![account, "--runtime".into(), runtime_name];
        Ok(request)
    }
    pub async fn accounts_check(
        &self,
        runtime: String,
        account: String,
    ) -> Result<CommandOutcome, ClientError> {
        self.account_action(&runtime, &account, false)?;
        self.pane_read(self.account_request(CommandKind::AccountsCheck, runtime, account)?)
            .await
    }
    pub async fn accounts_add_check(
        &self,
        runtime: String,
        account: String,
    ) -> Result<CommandOutcome, ClientError> {
        self.pane_read(self.account_request(CommandKind::AccountsAddCheck, runtime, account)?)
            .await
    }
    /// Recheck immediately before a handoff: validation reserves nothing, and Terminal proves no success.
    pub async fn add_account_action(
        &self,
        runtime: String,
        account: String,
    ) -> Result<TerminalAction, ClientError> {
        let checked = self
            .accounts_add_check(runtime.clone(), account.clone())
            .await?;
        match checked {
            CommandOutcome::Data(data)
                if matches!(*data, Snapshot::AccountAddCheck(ref c)
                if c.ok && c.account == account && c.runtime == runtime) =>
            {
                Ok(TerminalAction::AddAccount { runtime, account })
            }
            CommandOutcome::Refused(error) => Err(invalid(&error.message)),
            _ => Err(invalid("Check the account name before adding it")),
        }
    }
    pub async fn accounts_toggle(
        &self,
        runtime: String,
        account: String,
        enabled: bool,
    ) -> Result<CommandOutcome, ClientError> {
        self.account_action(&runtime, &account, true)?;
        self.client
            .mutate(self.account_request(
                if enabled {
                    CommandKind::AccountsEnable
                } else {
                    CommandKind::AccountsDisable
                },
                runtime,
                account,
            )?)
            .await
    }
    pub async fn share_list(&self) -> Result<CommandOutcome, ClientError> {
        let result = self
            .pane_read(self.user_request(CommandKind::ShareList)?)
            .await?;
        if let CommandOutcome::Data(data) = &result {
            if let Snapshot::Sharing(sharing) = data.as_ref() {
                self.panes.lock().expect("pane state").sharing = Some(sharing.clone());
            }
        }
        Ok(result)
    }
    pub async fn share_status(&self) -> Result<CommandOutcome, ClientError> {
        self.pane_read(self.user_request(CommandKind::ShareStatus)?)
            .await
    }
    pub(crate) fn share_kind_request(
        &self,
        kind: String,
        enabled: bool,
    ) -> Result<Request, ClientError> {
        // The decided switches that apply immediately. Plugin installation keeps its own approval flow.
        if !matches!(kind.as_str(), "mcp" | "hooks") {
            return Err(invalid("Choose MCP servers or Hooks"));
        }
        let mut request = self.user_request(if enabled {
            CommandKind::ShareKindsAdd
        } else {
            CommandKind::ShareKindsRemove
        })?;
        request.arguments = vec![kind];
        Ok(request)
    }
    pub async fn share_kind(
        &self,
        kind: String,
        enabled: bool,
    ) -> Result<CommandOutcome, ClientError> {
        self.client
            .mutate(self.share_kind_request(kind, enabled)?)
            .await
    }
    pub(crate) fn share_pick_request(
        &self,
        item: String,
        label: String,
        included: bool,
    ) -> Result<Request, ClientError> {
        operand(&item)?;
        operand(&label)?;
        let panes = self.panes.lock().expect("pane state");
        let cell = panes
            .sharing
            .as_ref()
            .and_then(|s| s.items.iter().find(|i| i.id == item))
            .and_then(|i| i.cells.get(&label))
            .ok_or_else(|| invalid("Read Sharing and choose a listed CLI cell"))?;
        if !cell.editable {
            return Err(invalid(
                cell.reason
                    .as_deref()
                    .unwrap_or("Use Copy command for this cell"),
            ));
        }
        let mut request = self.user_request(CommandKind::SharePick)?;
        request.arguments = vec![
            item,
            if included { "--in" } else { "--not-in" }.into(),
            label,
        ];
        Ok(request)
    }
    pub async fn share_pick(
        &self,
        item: String,
        label: String,
        included: bool,
    ) -> Result<CommandOutcome, ClientError> {
        self.client
            .mutate(self.share_pick_request(item, label, included)?)
            .await
    }
    pub async fn activity_undo_sync(&self, entry: String) -> Result<CommandOutcome, ClientError> {
        if !valid_log_id(&entry) {
            return Err(invalid("Choose an Activity event"));
        }
        // Refresh immediately; never interpret a command string or a supplied path.
        let CommandOutcome::Data(data) = self.activity_log().await? else {
            return Err(invalid("Read Activity again"));
        };
        let Snapshot::Activity(log) = data.as_ref() else {
            return Err(invalid("Read Activity again"));
        };
        let at = log
            .entries
            .iter()
            .position(|e| e.id == entry)
            .ok_or_else(|| invalid("Read Activity and choose a current event"))?;
        let event = &log.entries[at];
        if event.event_type != "sync" {
            return Err(invalid("Choose a sharing event"));
        }
        if ["workspace", "family"]
            .iter()
            .any(|key| event.fields.get(*key).is_some_and(|v| !v.is_null()))
        {
            return Err(invalid(
                "Open this repository's sharing history in Terminal",
            ));
        }
        let changes = event
            .fields
            .get("changes")
            .and_then(|v| v.as_array())
            .ok_or_else(|| invalid("No sharing backup"))?;
        let first = changes
            .first()
            .ok_or_else(|| invalid("No sharing backup"))?;
        let item = first
            .get("item")
            .and_then(|v| v.as_str())
            .ok_or_else(|| invalid("No sharing item"))?;
        operand(item)?;
        if !item.starts_with("agents/") && !item.starts_with("skills/") {
            return Err(invalid("Only agents and skills have single-item sync undo"));
        }
        if changes.iter().any(|c| {
            c.get("item").and_then(|v| v.as_str()) != Some(item)
                || c.get("ok").and_then(|v| v.as_bool()) != Some(true)
        }) || !changes
            .iter()
            .any(|c| c.get("backup").and_then(|v| v.as_str()).is_some())
        {
            return Err(invalid(
                "Choose a successful single-item event with a backup",
            ));
        }
        if log.entries[..at].iter().any(|e| {
            e.fields
                .get("changes")
                .and_then(|v| v.as_array())
                .is_some_and(|cs| {
                    cs.iter()
                        .any(|c| c.get("item").and_then(|v| v.as_str()) == Some(item))
                })
        }) {
            return Err(invalid("Changed elsewhere. Showing current value."));
        }
        let mut request = self.user_request(CommandKind::ShareUndo)?;
        request.arguments = vec![item.to_string()];
        self.client.mutate(request).await
    }
    pub async fn activity_log(&self) -> Result<CommandOutcome, ClientError> {
        let mut request = self.user_request(CommandKind::ActivityLog)?;
        request.arguments = vec!["--all".into(), "--limit".into(), "200".into()];
        let result = self.pane_read(request).await?;
        if let CommandOutcome::Data(data) = &result {
            if let Snapshot::Activity(log) = data.as_ref() {
                self.files.lock().expect("settings files").extend(
                    log.entries
                        .iter()
                        .filter_map(|e| e.fields.get("file").and_then(|v| v.as_str()))
                        .map(PathBuf::from),
                );
            }
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        client::AgtClient,
        terminal::{tests::Scratch, TerminalLauncher},
    };
    use std::{fs, os::unix::fs::PermissionsExt, path::Path};

    fn core(scratch: &Scratch) -> SettingsCore {
        let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/app");
        let folder = scratch.0.join("fixtures");
        fs::create_dir(&folder).unwrap();
        for name in [
            "accounts-list-locked",
            "accounts-check",
            "accounts-add-check",
            "accounts-enable",
            "roles-show-panes",
            "roles-config",
            "share-list",
            "share-status",
            "share-kinds-add",
            "share-pick-in",
            "share-undo",
            "activity-log",
            "config-set",
            "config-revert",
        ] {
            fs::write(
                folder.join(format!("{name}.json")),
                fs::read_to_string(fixtures.join(format!("{name}.json")))
                    .unwrap()
                    .replace("<ROOT>", scratch.0.to_str().unwrap()),
            )
            .unwrap();
        }
        let launcher = scratch.0.join("fake agt");
        let quote = crate::terminal::posix_quote;
        fs::write(&launcher, format!("#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" > {calls}\npwd > {cwd}\ncase \"$1 $2\" in\n'roles show') name=roles-show-panes;;\n'roles config') name=roles-config;;\n'accounts list') name=accounts-list-locked;;\n'accounts check') name=accounts-check;;\n'accounts add') name=accounts-add-check;;\n'accounts enable'|'accounts disable') name=accounts-enable;;\n'share list') name=share-list;;\n'share status') name=share-status;;\n'share kinds') name=share-kinds-add;;\n'share pick') name=share-pick-in;;\n'share undo') name=share-undo;;\n'config revert') name=config-revert;;\n'config set') name=config-set;;\n*) name=activity-log;;\nesac\n/bin/cat {folder}/\"$name\".json\n",
            calls=quote(scratch.0.join("arguments").to_str().unwrap()),
            cwd=quote(scratch.0.join("cwd").to_str().unwrap()), folder=quote(folder.to_str().unwrap()))).unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        SettingsCore::new(
            AgtClient::new(launcher, Some(scratch.0.clone())).unwrap(),
            scratch.0.clone(),
        )
    }
    fn arguments(scratch: &Scratch, expected: &[&str]) {
        assert_eq!(
            fs::read_to_string(scratch.0.join("arguments"))
                .unwrap()
                .lines()
                .collect::<Vec<_>>(),
            expected
        );
        assert_eq!(
            fs::read_to_string(scratch.0.join("cwd")).unwrap().trim(),
            scratch.0.canonicalize().unwrap().to_str().unwrap()
        );
    }

    #[tokio::test]
    async fn roles_enable_reorder_model_effort_and_structured_undo_argument_arrays() {
        let scratch = Scratch::new("roles-argv");
        let core = core(&scratch);
        core.roles_show(None).await.unwrap();
        arguments(&scratch, &["roles", "show", "--json"]);
        for enabled in [true, false] {
            core.roles_config(
                RoleEdit::Enable {
                    role: "builder".into(),
                    enabled,
                },
                Level::User,
                None,
                None,
            )
            .await
            .unwrap();
            arguments(
                &scratch,
                &[
                    "roles",
                    "config",
                    if enabled { "--enable" } else { "--disable" },
                    "builder",
                    "--user",
                    "--json",
                ],
            );
        }
        core.roles_config(
            RoleEdit::Reorder {
                role: "builder".into(),
                providers: vec!["codex".into(), "claude:work".into()],
            },
            Level::User,
            None,
            None,
        )
        .await
        .unwrap();
        arguments(
            &scratch,
            &[
                "roles",
                "config",
                "builder",
                "--order",
                "codex,claude:work",
                "--user",
                "--json",
            ],
        );
        for (edit, flag, value) in [
            (
                RoleEdit::Model {
                    entry: "claude:work".into(),
                    value: Some("opus".into()),
                },
                "--set-model",
                "claude:work=opus",
            ),
            (
                RoleEdit::Effort {
                    entry: "claude:work".into(),
                    value: None,
                },
                "--set-effort",
                "claude:work=",
            ),
        ] {
            core.roles_config(edit, Level::User, None, Some("my words".into()))
                .await
                .unwrap();
            arguments(
                &scratch,
                &[
                    "roles",
                    "config",
                    flag,
                    value,
                    "--user",
                    "--user-said",
                    "my words",
                    "--json",
                ],
            );
        }
        let file = scratch
            .0
            .join("agt/state/backups/settings/<UNDO>.json")
            .to_str()
            .unwrap()
            .to_owned();
        let result = core
            .roles_config(
                RoleEdit::Restore {
                    undo_from: file.clone(),
                    if_unchanged: "7896db640a27".into(),
                },
                Level::User,
                None,
                None,
            )
            .await
            .unwrap();
        assert!(matches!(result, CommandOutcome::Data(_)));
        arguments(
            &scratch,
            &[
                "roles",
                "config",
                "--from",
                &file,
                "--if-unchanged",
                "7896db640a27",
                "--user",
                "--json",
            ],
        );
        for (file, revision) in [(file, "changed"), ("/etc/passwd".into(), "7896db640a27")] {
            assert!(core
                .roles_config(
                    RoleEdit::Restore {
                        undo_from: file,
                        if_unchanged: revision.into()
                    },
                    Level::User,
                    None,
                    None
                )
                .await
                .is_err());
        }
        assert!(core
            .role_request(
                RoleEdit::Enable {
                    role: "builder".into(),
                    enabled: true
                },
                Level::Local,
                None,
                None
            )
            .is_err());
    }

    #[tokio::test]
    async fn account_enable_disable_check_name_check_and_terminal_handoffs() {
        let scratch = Scratch::new("accounts-argv");
        let core = core(&scratch);
        assert!(core.account_action("claude", "work", false).is_err());
        core.accounts_list().await.unwrap();
        arguments(&scratch, &["accounts", "list", "--json"]);
        for enabled in [true, false] {
            core.accounts_toggle("claude".into(), "work".into(), enabled)
                .await
                .unwrap();
            arguments(
                &scratch,
                &[
                    "accounts",
                    if enabled { "enable" } else { "disable" },
                    "work",
                    "--runtime",
                    "claude",
                    "--json",
                ],
            );
        }
        for name in ["primary", "claude-lab"] {
            assert!(core
                .accounts_toggle("claude".into(), name.into(), false)
                .await
                .is_err());
        }
        core.accounts_check("claude".into(), "primary".into())
            .await
            .unwrap();
        arguments(
            &scratch,
            &[
                "accounts",
                "check",
                "primary",
                "--runtime",
                "claude",
                "--json",
            ],
        );
        core.accounts_add_check("claude".into(), "spare".into())
            .await
            .unwrap();
        arguments(
            &scratch,
            &[
                "accounts",
                "add",
                "spare",
                "--runtime",
                "claude",
                "--check",
                "--json",
            ],
        );
        let terminal =
            TerminalLauncher::new(scratch.0.join("fake agt"), scratch.0.join("handoffs")).unwrap();
        let login = terminal
            .prepare(&core.account_action("claude", "work", false).unwrap())
            .unwrap();
        assert!(fs::read_to_string(login.path)
            .unwrap()
            .contains("'accounts' 'login' 'work' '--runtime' 'claude'"));
        let add = terminal
            .prepare(
                &core
                    .add_account_action("claude".into(), "spare".into())
                    .await
                    .unwrap(),
            )
            .unwrap();
        assert!(fs::read_to_string(add.path)
            .unwrap()
            .contains("'accounts' 'add' 'spare' '--runtime' 'claude'"));
        assert!(core
            .accounts_add_check("unknown".into(), "spare".into())
            .await
            .is_err());
        assert!(core
            .accounts_add_check("claude".into(), "--no-login".into())
            .await
            .is_err());
    }

    #[tokio::test]
    async fn share_switch_kinds_picks_activity_and_guarded_revert_argument_arrays() {
        let scratch = Scratch::new("share-argv");
        let core = core(&scratch);
        core.write(
            "share.notify".into(),
            Some("false".into()),
            Level::User,
            None,
            "read-revision".into(),
            None,
        )
        .await
        .unwrap();
        arguments(
            &scratch,
            &[
                "config",
                "set",
                "share.notify",
                "false",
                "--user",
                "--if-unchanged",
                "read-revision",
                "--json",
            ],
        );
        for (kind, enabled) in [
            ("mcp", true),
            ("mcp", false),
            ("hooks", true),
            ("hooks", false),
        ] {
            core.share_kind(kind.into(), enabled).await.unwrap();
            arguments(
                &scratch,
                &[
                    "share",
                    "kinds",
                    if enabled { "add" } else { "remove" },
                    kind,
                    "--json",
                ],
            );
        }
        assert!(core.share_kind("plugins".into(), true).await.is_err());
        core.share_list().await.unwrap();
        arguments(&scratch, &["share", "list", "--json"]);
        core.share_status().await.unwrap();
        arguments(&scratch, &["share", "status", "--json"]);
        for included in [true, false] {
            core.share_pick("agents/example".into(), "codex".into(), included)
                .await
                .unwrap();
            arguments(
                &scratch,
                &[
                    "share",
                    "pick",
                    "agents/example",
                    if included { "--in" } else { "--not-in" },
                    "codex",
                    "--json",
                ],
            );
        }
        assert!(core
            .share_pick("agents/example".into(), "claude".into(), false)
            .await
            .is_err());
        assert!(core
            .share_pick("agents/missing".into(), "codex".into(), true)
            .await
            .is_err());
        let CommandOutcome::Data(log) = core.activity_log().await.unwrap() else {
            panic!("expected Activity")
        };
        arguments(&scratch, &["log", "--all", "--limit", "200", "--json"]);
        let Snapshot::Activity(log) = *log else {
            panic!("expected log model")
        };
        let entry = log
            .entries
            .iter()
            .find(|e| {
                e.event_type == "setting"
                    && e.fields.get("key").and_then(|v| v.as_str()) == Some("max_depth")
            })
            .unwrap();
        core.revert(entry.id.clone(), None, None).await.unwrap();
        arguments(&scratch, &["config", "revert", &entry.id, "--json"]);
        assert!(core.revert("--user".into(), None, None).await.is_err());
        assert!(core.revert("2026-13:1".into(), None, None).await.is_err());
    }
    #[tokio::test]
    async fn activity_sync_undo_uses_one_fresh_backed_item_and_refuses_stale_or_forged_events() {
        let scratch = Scratch::new("sync-undo");
        let core = core(&scratch);
        assert!(core.activity_undo_sync("--user".into()).await.is_err());
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/app/activity-log-backed.json");
        let path = scratch.0.join("fixtures/activity-log.json");
        let text = fs::read_to_string(fixture)
            .unwrap()
            .replace("<ROOT>", scratch.0.to_str().unwrap());
        fs::write(&path, &text).unwrap();
        let mut value: serde_json::Value = serde_json::from_str(&text).unwrap();
        let id = value["entries"][0]["id"].as_str().unwrap().to_string();
        core.activity_undo_sync(id.clone()).await.unwrap();
        arguments(&scratch, &["share", "undo", "agents/example", "--json"]);
        assert!(core
            .activity_undo_sync("2027-01:999999".into())
            .await
            .is_err());
        let mut later = value["entries"][0].clone();
        later["id"] = serde_json::json!("2027-01:999999");
        value["entries"].as_array_mut().unwrap().insert(0, later);
        fs::write(&path, value.to_string()).unwrap();
        assert!(core.activity_undo_sync(id.clone()).await.is_err());
        value["entries"].as_array_mut().unwrap().remove(0);
        value["entries"][0]["workspace"] = serde_json::json!("/a/repo");
        fs::write(&path, value.to_string()).unwrap();
        assert!(core.activity_undo_sync(id).await.is_err());
    }
}
