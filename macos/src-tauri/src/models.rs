//! JSON contracts from agt. Dynamic setting values stay JSON; contract structure is typed.
use serde::{de::Error, Deserialize, Deserializer, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::Path};

pub const MIN_SCHEMA_VERSION: u32 = 1;
pub const MAX_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct SchemaVersion(pub u32);

impl<'de> Deserialize<'de> for SchemaVersion {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let version = u32::deserialize(deserializer)?;
        if !(MIN_SCHEMA_VERSION..=MAX_SCHEMA_VERSION).contains(&version) {
            return Err(D::Error::custom(format!(
                "unsupported agt schema_version {version}; supported range is 1…1"
            )));
        }
        Ok(Self(version))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    Off,
    LoggedOut,
    Unhealthy,
    Cooling,
    Recovering,
    Busy,
    Ok,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Usage {
    pub schema_version: SchemaVersion,
    pub agt_version: String,
    pub generated_at: f64,
    pub providers: Vec<ProviderUsage>,
    pub jobs: JobTotals,
    pub uninstall_blockers: Vec<UninstallBlocker>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderUsage {
    pub runtime: String,
    pub label: String,
    pub accounts: Vec<AccountUsage>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountUsage {
    pub name: String,
    pub enabled: bool,
    pub availability: Availability,
    pub reason: Option<String>,
    pub until: Option<f64>,
    pub running_jobs: u64,
    pub busy_slots: u64,
    pub capacity: Option<u64>,
    pub unhealthy_models: Vec<ModelHealth>,
    pub windows: Vec<UsageWindow>,
    pub problem: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelHealth {
    pub model: String,
    pub reason: Option<String>,
    pub until: Option<f64>,
    pub availability: Availability,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UsageWindow {
    pub name: String,
    pub label: String,
    pub used_percent: f64,
    pub resets_at: Option<f64>,
    pub observed_at: Option<f64>,
    pub stale: bool,
    pub reset: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobTotals {
    pub active: u64,
    pub queued: u64,
    pub starting: u64,
    pub running: u64,
    pub unassigned: u64,
    pub removed_accounts: u64,
    pub surviving_workers: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UninstallBlocker {
    pub kind: String,
    pub job_id: String,
    pub runtime: Option<String>,
    pub account: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Level {
    User,
    Workspace,
    Local,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SettingsFile {
    pub level: Level,
    pub file: String,
    pub exists: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigShow {
    pub schema_version: SchemaVersion,
    pub workspace: Option<String>,
    pub files: Vec<SettingsFile>,
    pub error: Option<Value>,
    pub notes: Vec<String>,
    pub settings: Vec<Setting>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Setting {
    pub key: String,
    pub value: Value,
    pub shown: String,
    pub source: String,
    pub file: Option<String>,
    pub line: Option<u64>,
    pub description: String,
    pub needs_user_words: bool,
    pub user_only: bool,
    pub changed_by: Option<Value>,
    pub applies: String,
    pub kind: String,
    pub choices: Vec<Value>,
    pub minimum: Option<f64>,
    pub cap: bool,
    pub editable: bool,
    pub accepted_levels: Vec<Level>,
    pub still_limited_by: Vec<Value>,
    pub levels: BTreeMap<String, LevelValue>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LevelValue {
    pub present: bool,
    pub value: Value,
    pub file: String,
    pub line: Option<u64>,
    pub revision: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Previous {
    pub level: Level,
    pub present: bool,
    pub value: Value,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Override {
    pub level: Level,
    pub file: String,
    pub value: Value,
    pub fix: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConfigWrite {
    pub ok: bool,
    pub key: String,
    pub file: String,
    pub level: Level,
    pub changed: bool,
    pub previous: Previous,
    pub revision: String,
    pub old: Option<Value>,
    pub new: Option<Value>,
    pub applies: Option<String>,
    pub effective: Option<Value>,
    pub overridden_by: Option<Override>,
    #[serde(default)]
    pub still_limited_by: Vec<Value>,
    pub undo: Option<String>,
    pub backup: Option<String>,
    pub excluded_in: Option<Value>,
    #[serde(default)]
    pub triggers: Vec<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountsList {
    pub runtimes: Vec<AccountRuntime>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountRuntime {
    pub runtime: String,
    pub binary: String,
    pub version: Option<String>,
    pub primary_setup: String,
    pub failover: String,
    pub limits: RuntimeLimits,
    pub accounts: Vec<Account>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuntimeLimits {
    pub usage_ceiling: f64,
    pub when_full: String,
    pub max_jobs: Option<u64>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Account {
    pub name: String,
    pub home: String,
    pub enabled: bool,
    pub takes_unpinned_jobs: Option<bool>,
    pub auth: Auth,
    pub busy_slots: u64,
    pub running_jobs: u64,
    pub limits: AccountLimits,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Auth {
    pub logged_in: Option<bool>,
    pub auth_method: Option<String>,
    pub subscription_type: Option<String>,
    pub config_directory: Option<String>,
    pub detail: Option<String>,
    pub error: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountLimits {
    pub max_jobs: Option<u64>,
    pub max_jobs_from: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountsCheck {
    pub schema_version: SchemaVersion,
    pub runtime: String,
    pub accounts: Vec<LoginCheck>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LoginCheck {
    pub account: String,
    pub logged_in: Option<bool>,
    pub checked_at: f64,
    pub error: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountToggle {
    pub ok: bool,
    pub account: String,
    pub enabled: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountAddCheck {
    pub ok: bool,
    pub account: String,
    pub runtime: String,
    pub home: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RolesShow {
    pub schema_version: SchemaVersion,
    pub roles: Vec<Role>,
    pub files: BTreeMap<String, Option<String>>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Role {
    pub name: String,
    pub access: String,
    pub enabled: bool,
    pub base: bool,
    pub use_when: String,
    pub level: String,
    pub providers: Vec<RoleProvider>,
    pub sources: BTreeMap<String, RoleSource>,
    pub when_to_call: String,
    pub rules_chars: u64,
    pub rules_state: Option<String>,
    pub agent_files: Vec<Value>,
    pub archived: Vec<Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoleSource {
    pub level: String,
    pub file: Option<String>,
    pub line: Option<u64>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoleProvider {
    pub provider: String,
    pub entry: String,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub usable: bool,
    pub reason: Option<String>,
    pub choices: ProviderChoices,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProviderChoices {
    pub models: Vec<ModelChoice>,
    pub efforts: Vec<Option<String>>,
    pub catalog: Catalog,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Catalog {
    pub status: String,
    pub message: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelChoice {
    pub model: Option<String>,
    pub offered: bool,
    pub allowed: bool,
    pub reason: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RolesConfig {
    pub changes: BTreeMap<String, RoleChange>,
    pub lines: Vec<String>,
    pub file: String,
    pub level: Level,
    pub dry_run: bool,
    pub needs_user_words: bool,
    pub overridden_by: Option<Value>,
    pub in_effect: Value,
    pub backup: Option<String>,
    pub undo: Option<String>,
    pub undo_from: Option<String>,
    pub if_unchanged: Option<String>,
    pub excluded_in: Option<Value>,
    pub agents: Vec<Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoleChange {
    pub old: BTreeMap<String, Value>,
    pub new: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareList {
    pub schema_version: SchemaVersion,
    pub enabled: bool,
    pub folders: Vec<ShareFolder>,
    pub items: Vec<ShareItem>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareFolder {
    pub label: String,
    pub cli: String,
    pub path: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareItem {
    pub id: String,
    pub kind: String,
    pub name: String,
    pub cells: BTreeMap<String, ShareCell>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareCell {
    pub state: String,
    pub note: String,
    pub editable: bool,
    pub reason: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareStatus {
    pub enabled: bool,
    #[serde(default)]
    pub folders: Vec<ShareFolder>,
    #[serde(default)]
    pub attention: Vec<Value>,
    #[serde(default)]
    pub conflicts: Vec<String>,
    #[serde(default)]
    pub pending_delete: Vec<String>,
    #[serde(default)]
    pub frozen: Vec<Value>,
    #[serde(flatten)]
    pub details: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    pub ok: bool,
    pub error: AgtError,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgtError {
    pub message: String,
    pub code: Option<String>,
    pub key: Option<String>,
    pub account: Option<String>,
    pub runtime: Option<String>,
    pub exit_code: Option<i32>,
    pub needs_user_words: Option<bool>,
    pub file: Option<String>,
    pub line: Option<u64>,
    pub role: Option<String>,
    #[serde(default)]
    pub reasons: Vec<String>,
    pub ask: Option<String>,
    pub retry: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NeedsChoice {
    pub error: String,
    pub ask: String,
    pub labels: Vec<String>,
    pub choices: Vec<ShareChoice>,
    pub retry: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareChoice {
    pub kind: String,
    pub name: String,
    pub cells: BTreeMap<String, ChoiceCell>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChoiceCell {
    pub state: String,
    pub note: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CommandKind {
    Usage,
    ConfigShow,
    ConfigSet,
    ConfigUnset,
    ConfigRevert,
    AccountsList,
    AccountsCheck,
    AccountsAddCheck,
    AccountsEnable,
    AccountsDisable,
    RolesShow,
    RolesConfig,
    ShareList,
    ShareStatus,
    SharePick,
}
impl CommandKind {
    pub fn prefix(self) -> &'static [&'static str] {
        match self {
            Self::Usage => &["usage"],
            Self::ConfigShow => &["config", "show"],
            Self::ConfigSet => &["config", "set"],
            Self::ConfigUnset => &["config", "unset"],
            Self::ConfigRevert => &["config", "revert"],
            Self::AccountsList => &["accounts", "list"],
            Self::AccountsCheck => &["accounts", "check"],
            Self::AccountsAddCheck => &["accounts", "add"],
            Self::AccountsEnable => &["accounts", "enable"],
            Self::AccountsDisable => &["accounts", "disable"],
            Self::RolesShow => &["roles", "show"],
            Self::RolesConfig => &["roles", "config"],
            Self::ShareList => &["share", "list"],
            Self::ShareStatus => &["share", "status"],
            Self::SharePick => &["share", "pick"],
        }
    }
    pub fn is_read(self) -> bool {
        matches!(
            self,
            Self::Usage
                | Self::ConfigShow
                | Self::AccountsList
                | Self::AccountsCheck
                | Self::AccountsAddCheck
                | Self::RolesShow
                | Self::ShareList
                | Self::ShareStatus
        )
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum Snapshot {
    Usage(Usage),
    Config(ConfigShow),
    ConfigWrite(ConfigWrite),
    Accounts(AccountsList),
    AccountsCheck(AccountsCheck),
    AccountAddCheck(AccountAddCheck),
    AccountToggle(AccountToggle),
    Roles(RolesShow),
    RolesConfig(RolesConfig),
    Sharing(ShareList),
    ShareStatus(ShareStatus),
    Error(ErrorEnvelope),
    Choice(NeedsChoice),
}

pub fn decode(kind: CommandKind, bytes: &[u8]) -> Result<Snapshot, String> {
    let mut value: Value =
        serde_json::from_slice(bytes).map_err(|e| format!("invalid agt JSON: {e}"))?;
    // Legacy commands have no version. If one is supplied it must still be understood.
    if let Some(version) = value.get("schema_version") {
        serde_json::from_value::<SchemaVersion>(version.clone()).map_err(|e| e.to_string())?;
    }
    macro_rules! parse {
        ($ty:ty, $variant:ident) => {
            serde_json::from_value::<$ty>(value)
                .map(Snapshot::$variant)
                .map_err(|e| format!("invalid {kind:?} JSON: {e}"))
        };
    }
    if value.get("error").and_then(Value::as_str) == Some("needs_choice") {
        return parse!(NeedsChoice, Choice);
    }
    // config set/unset retain the CLI's original top-level exit-3 shape.
    if value.get("error").and_then(Value::as_str) == Some("needs_user_words") {
        value = serde_json::json!({"ok":false, "error": {
            "code":"needs_user_words", "needs_user_words":true,
            "message":value["ask"], "ask":value["ask"], "key":value["key"], "retry":value["retry"]
        }});
    }
    if value.get("ok") == Some(&Value::Bool(false)) {
        return parse!(ErrorEnvelope, Error);
    }
    match kind {
        CommandKind::Usage => parse!(Usage, Usage),
        CommandKind::ConfigShow => parse!(ConfigShow, Config),
        CommandKind::ConfigSet | CommandKind::ConfigUnset | CommandKind::ConfigRevert => {
            parse!(ConfigWrite, ConfigWrite)
        }
        CommandKind::AccountsList => parse!(AccountsList, Accounts),
        CommandKind::AccountsCheck => parse!(AccountsCheck, AccountsCheck),
        CommandKind::AccountsAddCheck => parse!(AccountAddCheck, AccountAddCheck),
        CommandKind::AccountsEnable | CommandKind::AccountsDisable => {
            parse!(AccountToggle, AccountToggle)
        }
        CommandKind::RolesShow => parse!(RolesShow, Roles),
        CommandKind::RolesConfig => parse!(RolesConfig, RolesConfig),
        CommandKind::ShareList => parse!(ShareList, Sharing),
        CommandKind::ShareStatus => parse!(ShareStatus, ShareStatus),
        CommandKind::SharePick => {
            Err("share pick did not return a supported choice or refusal".into())
        }
    }
}

/// Explicit inventory: unknown files fail the checker, so a new producer needs a model.
pub const FIXTURES: &[(&str, CommandKind)] = &[
    ("usage.json", CommandKind::Usage),
    ("usage-popover.json", CommandKind::Usage),
    ("usage-nothing.json", CommandKind::Usage),
    ("usage-idle.json", CommandKind::Usage),
    ("config-show.json", CommandKind::ConfigShow),
    ("config-show-levels.json", CommandKind::ConfigShow),
    ("config-show-broken.json", CommandKind::ConfigShow),
    ("config-set.json", CommandKind::ConfigSet),
    ("config-set-error.json", CommandKind::ConfigSet),
    ("config-set-needs-user-words.json", CommandKind::ConfigSet),
    ("config-unset-absent.json", CommandKind::ConfigUnset),
    ("config-revert.json", CommandKind::ConfigRevert),
    ("accounts-list.json", CommandKind::AccountsList),
    ("accounts-check.json", CommandKind::AccountsCheck),
    ("accounts-add-check.json", CommandKind::AccountsAddCheck),
    ("accounts-enable.json", CommandKind::AccountsEnable),
    ("accounts-disable.json", CommandKind::AccountsDisable),
    ("accounts-enable-refusal.json", CommandKind::AccountsEnable),
    ("roles-show.json", CommandKind::RolesShow),
    ("roles-show-discovered.json", CommandKind::RolesShow),
    ("roles-config.json", CommandKind::RolesConfig),
    ("roles-config-error.json", CommandKind::RolesConfig),
    (
        "roles-config-needs-user-words.json",
        CommandKind::RolesConfig,
    ),
    ("share-list.json", CommandKind::ShareList),
    ("share-list-kinds.json", CommandKind::ShareList),
    ("share-status.json", CommandKind::ShareStatus),
    ("share-pick-needs-choice.json", CommandKind::SharePick),
];

pub fn check_fixtures(folder: &Path) -> Result<usize, String> {
    let mut count = 0;
    for entry in fs::read_dir(folder).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or("non-UTF8 fixture name")?;
        let (_, kind) = FIXTURES
            .iter()
            .find(|(file, _)| *file == name)
            .ok_or_else(|| format!("missing model inventory entry for {name}"))?;
        decode(*kind, &fs::read(&path).map_err(|e| e.to_string())?)
            .map_err(|e| format!("{name}: {e}"))?;
        count += 1;
    }
    for (name, _) in FIXTURES {
        if !folder.join(name).is_file() {
            return Err(format!("missing fixture: {name}"));
        }
    }
    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_golden_fixture_decodes() {
        let folder = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/app");
        assert_eq!(check_fixtures(&folder).unwrap(), FIXTURES.len());
    }
    #[test]
    fn schema_range_is_enforced_by_models_and_decoder() {
        for version in [0, 2, 99] {
            let mut value: Value =
                serde_json::from_str(include_str!("../../../tests/fixtures/app/usage.json"))
                    .unwrap();
            value["schema_version"] = version.into();
            assert!(serde_json::from_value::<Usage>(value.clone())
                .unwrap_err()
                .to_string()
                .contains("supported range is 1…1"));
            assert!(decode(CommandKind::Usage, &serde_json::to_vec(&value).unwrap()).is_err());
        }
        assert!(decode(CommandKind::Usage, b"{}").is_err());
    }
}
