//! Shared login checks. A webview closing never cancels an accepted check.
use crate::{
    client::{AgtClient, ClientError, CommandOutcome, ReadReply, Request},
    models::{
        AccountsCheck, AccountsList, CommandKind, LoginCheck, SchemaVersion, Snapshot, Usage,
    },
};
use serde::Serialize;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};
use tokio::sync::watch;

pub const THROTTLE_SECONDS: f64 = 600.0;
type Outcome = Result<CommandOutcome, ClientError>;
type Key = (String, String);
type Publisher = Arc<dyn Fn(Vec<CheckSnapshot>) + Send + Sync>;

#[derive(Clone, Copy)]
enum Intent {
    Automatic,
    Retry,
    Settings,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Idle,
    Checking,
    Complete,
    Failed,
}
#[derive(Debug, Clone, Serialize)]
pub struct CheckSnapshot {
    pub runtime: String,
    pub account: String,
    pub phase: Phase,
    pub result: Option<LoginCheck>,
}
struct Entry {
    snapshot: CheckSnapshot,
    last_at: Option<f64>,
    outcome: Option<Outcome>,
    active: Option<watch::Receiver<Option<Outcome>>>,
}
struct Inner {
    client: AgtClient,
    cwd: Option<std::path::PathBuf>,
    clock: Arc<dyn Fn() -> f64 + Send + Sync>,
    entries: Mutex<BTreeMap<Key, Entry>>,
    publisher: Mutex<Option<Publisher>>,
}
#[derive(Clone)]
pub struct AccountChecks(Arc<Inner>);
impl AccountChecks {
    pub fn new(client: AgtClient, home: std::path::PathBuf) -> Self {
        Self::with_clock(client, Arc::new(crate::app::now), Some(home))
    }
    fn with_clock(
        client: AgtClient,
        clock: Arc<dyn Fn() -> f64 + Send + Sync>,
        cwd: Option<std::path::PathBuf>,
    ) -> Self {
        Self(Arc::new(Inner {
            client,
            cwd,
            clock,
            entries: Mutex::new(BTreeMap::new()),
            publisher: Mutex::new(None),
        }))
    }
    pub fn set_publisher(&self, publish: Publisher) {
        *self.0.publisher.lock().expect("check publisher") = Some(publish);
    }
    pub fn snapshots(&self) -> Vec<CheckSnapshot> {
        self.0
            .entries
            .lock()
            .expect("account checks")
            .values()
            .map(|e| e.snapshot.clone())
            .collect()
    }
    fn publish(&self) {
        let publisher = self.0.publisher.lock().expect("check publisher");
        if let Some(publish) = publisher.as_ref() {
            publish(self.snapshots());
        }
    }
    /// Cached list reads seed state but never start work (including in Settings).
    pub fn seed(&self, list: &AccountsList) {
        let mut entries = self.0.entries.lock().expect("account checks");
        for provider in &list.runtimes {
            for account in &provider.accounts {
                let entry = entries
                    .entry((provider.runtime.clone(), account.name.clone()))
                    .or_insert_with(|| Entry {
                        snapshot: CheckSnapshot {
                            runtime: provider.runtime.clone(),
                            account: account.name.clone(),
                            phase: Phase::Idle,
                            result: None,
                        },
                        last_at: None,
                        outcome: None,
                        active: None,
                    });
                if entry.active.is_some() {
                    continue;
                }
                if let Some(result) = account.login_check.as_ref().filter(|r| {
                    r.checked_at.is_finite() && entry.last_at.is_none_or(|at| r.checked_at > at)
                }) {
                    entry.last_at = Some(result.checked_at);
                    entry.snapshot.phase = if result.error.is_some() {
                        Phase::Failed
                    } else {
                        Phase::Complete
                    };
                    entry.snapshot.result = Some(result.clone());
                    entry.outcome = Some(Ok(check_outcome(&provider.runtime, result.clone())));
                }
            }
        }
        drop(entries);
        self.publish();
    }
    /// Automatic checks are throttled; Retry bypasses only a failed result.
    pub async fn check(&self, runtime: String, account: String, retry: bool) -> Outcome {
        self.check_with(
            runtime,
            account,
            if retry {
                Intent::Retry
            } else {
                Intent::Automatic
            },
        )
        .await
        .0
    }
    /// An explicit Settings click always checks, but still joins active work.
    pub async fn check_explicit(&self, runtime: String, account: String) -> Outcome {
        self.check_with(runtime, account, Intent::Settings).await.0
    }
    /// Discover accounts once per native open. This read has a separate resource
    /// key from Settings' workspace-scoped listing, so neither supersedes the other.
    /// If the cache cannot be read, known usage accounts still get due checks.
    pub async fn on_open(&self, fallback: Option<Usage>) -> Result<usize, ClientError> {
        let mut request = Request::new(CommandKind::AccountsList, None);
        request.arguments.push("--cached-only".into());
        let accounts: Vec<Key> = match self.read(request).await {
            Ok(CommandOutcome::Data(data)) if matches!(data.as_ref(), Snapshot::Accounts(_)) => {
                let Snapshot::Accounts(list) = *data else {
                    unreachable!()
                };
                self.seed(&list);
                list.runtimes
                    .into_iter()
                    .flat_map(|p| {
                        p.accounts
                            .into_iter()
                            .filter(|a| a.enabled)
                            .map(move |a| (p.runtime.clone(), a.name))
                    })
                    .collect()
            }
            _ => {
                let usage = match fallback {
                    Some(usage) => usage,
                    None => match self
                        .read(Request::new(CommandKind::Usage, self.0.cwd.clone()))
                        .await?
                    {
                        CommandOutcome::Data(data) => match *data {
                            Snapshot::Usage(usage) => usage,
                            _ => {
                                return Err(ClientError::InvalidResponse(
                                    "Expected usage accounts".into(),
                                ))
                            }
                        },
                        _ => {
                            return Err(ClientError::InvalidResponse(
                                "Cannot read usage accounts".into(),
                            ))
                        }
                    },
                };
                usage
                    .providers
                    .into_iter()
                    .flat_map(|p| {
                        p.accounts
                            .into_iter()
                            .filter(|a| a.enabled)
                            .map(move |a| (p.runtime.clone(), a.name))
                    })
                    .collect()
            }
        };
        let mut work = tokio::task::JoinSet::new();
        for (runtime, account) in accounts {
            let checks = self.clone();
            work.spawn(async move {
                checks
                    .check_with(runtime, account, Intent::Automatic)
                    .await
                    .1
            });
        }
        let mut completed = 0;
        while let Some(result) = work.join_next().await {
            if result.unwrap_or(false) {
                completed += 1;
            }
        }
        Ok(completed)
    }
    // The flag tells the open path whether work actually ran. Returning a
    // retained result must not cause another usage read for each throttled row.
    async fn check_with(
        &self,
        runtime: String,
        account: String,
        intent: Intent,
    ) -> (Outcome, bool) {
        let key = (runtime.clone(), account.clone());
        let (mut receiver, start) = {
            let mut entries = self.0.entries.lock().expect("account checks");
            let entry = entries.entry(key.clone()).or_insert_with(|| Entry {
                snapshot: CheckSnapshot {
                    runtime: runtime.clone(),
                    account: account.clone(),
                    phase: Phase::Idle,
                    result: None,
                },
                last_at: None,
                outcome: None,
                active: None,
            });
            if let Some(active) = &entry.active {
                (active.clone(), None)
            } else {
                if !matches!(intent, Intent::Settings)
                    && !(matches!(intent, Intent::Retry)
                        && matches!(entry.snapshot.phase, Phase::Failed))
                    && entry
                        .last_at
                        .is_some_and(|at| (self.0.clock)() - at < THROTTLE_SECONDS)
                {
                    if let Some(outcome) = &entry.outcome {
                        return (outcome.clone(), false);
                    }
                }
                let (sender, receiver) = watch::channel(None);
                entry.active = Some(receiver.clone());
                entry.snapshot.phase = Phase::Checking;
                (receiver, Some(sender))
            }
        };
        if let Some(sender) = start {
            self.publish();
            let checks = self.clone();
            // The initiating IPC future can disappear when a window closes.
            tauri::async_runtime::spawn(async move {
                let outcome = checks.execute(&runtime, &account).await;
                let result = match &outcome {
                    Ok(CommandOutcome::Data(data)) => match data.as_ref() {
                        Snapshot::AccountsCheck(check) => check
                            .accounts
                            .iter()
                            .find(|r| r.account == account)
                            .cloned(),
                        _ => None,
                    },
                    _ => None,
                }
                .unwrap_or_else(|| LoginCheck {
                    account: account.clone(),
                    logged_in: None,
                    checked_at: (checks.0.clock)(),
                    error: Some(match &outcome {
                        Err(error) => error.to_string(),
                        _ => "Account check refused".into(),
                    }),
                });
                {
                    let mut entries = checks.0.entries.lock().expect("account checks");
                    let entry = entries.get_mut(&key).expect("running account check");
                    entry.last_at = Some((checks.0.clock)());
                    entry.snapshot.phase = if result.error.is_some() {
                        Phase::Failed
                    } else {
                        Phase::Complete
                    };
                    entry.snapshot.result = Some(result);
                    entry.outcome = Some(outcome.clone());
                    entry.active = None;
                    sender.send_replace(Some(outcome));
                }
                checks.publish();
            });
        }
        loop {
            if let Some(outcome) = receiver.borrow().clone() {
                return (outcome, true);
            }
            if let Err(error) = receiver.changed().await {
                return (Err(ClientError::Execution(error.to_string())), true);
            }
        }
    }
    async fn execute(&self, runtime: &str, account: &str) -> Outcome {
        let mut request = Request::new(CommandKind::AccountsCheck, self.0.cwd.clone());
        request.arguments = vec![account.into(), "--runtime".into(), runtime.into()];
        match self.read(request).await? {
            CommandOutcome::Data(data) => {
                if let Snapshot::AccountsCheck(check) = data.as_ref() {
                    if check.runtime == runtime
                        && check.accounts.iter().any(|r| r.account == account)
                    {
                        return Ok(CommandOutcome::Data(data));
                    }
                }
                Err(ClientError::InvalidResponse(
                    "Expected account check result".into(),
                ))
            }
            outcome => Ok(outcome),
        }
    }
    async fn read(&self, request: Request) -> Outcome {
        match self
            .0
            .client
            .read(request)
            .await?
            .await
            .map_err(|e| ClientError::Execution(e.to_string()))?
        {
            ReadReply::Fresh { result, .. } => result,
            ReadReply::Superseded { .. } => Err(ClientError::InvalidResponse(
                "Account check superseded".into(),
            )),
        }
    }
}
fn check_outcome(runtime: &str, result: LoginCheck) -> CommandOutcome {
    CommandOutcome::Data(Box::new(Snapshot::AccountsCheck(AccountsCheck {
        schema_version: SchemaVersion(1),
        runtime: runtime.into(),
        accounts: vec![result],
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    use std::{
        fs,
        future::Future,
        os::unix::fs::PermissionsExt,
        sync::atomic::{AtomicU64, Ordering},
        time::Duration,
    };

    struct Fake {
        scratch: Scratch,
        checks: AccountChecks,
        clock: Arc<AtomicU64>,
    }
    impl Fake {
        fn new() -> Self {
            let scratch = Scratch::new("account-checks");
            let launcher = scratch.0.join("fake-agt");
            // Only this fake is executed; HOME, provider homes and AGT_HOME are private.
            fs::write(&launcher, "#!/bin/sh\nset -eu\ncase \"$1 $2\" in\n'accounts list') /bin/cat \"$HOME/accounts.json\"; exit;;\n'usage --json') /bin/cat \"$HOME/usage.json\"; exit;;\nesac\nprintf '%s\\n' call >> \"$HOME/calls\"\nif [ -p \"$HOME/gate\" ]; then /bin/cat \"$HOME/gate\" > /dev/null; fi\n/bin/cat \"$HOME/result.json\"\n").unwrap();
            fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
            let env = ["HOME", "AGT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"]
                .into_iter()
                .map(|name| (name, Some(scratch.0.clone().into_os_string())))
                .collect();
            let client = AgtClient::with_env(launcher, env, Some(scratch.0.clone())).unwrap();
            let clock = Arc::new(AtomicU64::new(1000));
            let time = clock.clone();
            let checks = AccountChecks::with_clock(
                client,
                Arc::new(move || time.load(Ordering::SeqCst) as f64),
                None,
            );
            let fake = Self {
                scratch,
                checks,
                clock,
            };
            fake.answer(Some(true), None);
            fs::write(
                fake.scratch.0.join("accounts.json"),
                serde_json::to_vec(&fake.cached(None)).unwrap(),
            )
            .unwrap();
            fs::write(
                fake.scratch.0.join("usage.json"),
                serde_json::to_vec(&fake.usage()).unwrap(),
            )
            .unwrap();
            fake
        }
        fn answer(&self, logged_in: Option<bool>, error: Option<&str>) {
            fs::write(self.scratch.0.join("result.json"), serde_json::json!({
                "schema_version": 1, "runtime": "codex", "accounts": [{"account": "primary", "logged_in": logged_in,
                "checked_at": self.clock.load(Ordering::SeqCst), "error": error}]
            }).to_string()).unwrap();
        }
        fn calls(&self) -> usize {
            fs::read_to_string(self.scratch.0.join("calls"))
                .unwrap_or_default()
                .lines()
                .count()
        }
        fn usage(&self) -> Usage {
            let mut usage: Usage =
                serde_json::from_slice(include_bytes!("../../../tests/fixtures/app/usage.json"))
                    .unwrap();
            usage.providers.retain(|p| p.runtime == "codex");
            usage.providers[0].accounts.retain(|a| a.name == "primary");
            assert_eq!(usage.providers[0].accounts.len(), 1);
            usage.providers[0].accounts[0].enabled = true;
            usage
        }
        fn cached(&self, result: Option<LoginCheck>) -> AccountsList {
            let mut list: AccountsList = serde_json::from_slice(include_bytes!(
                "../../../tests/fixtures/app/accounts-list-cached.json"
            ))
            .unwrap();
            list.runtimes.retain(|r| r.runtime == "codex");
            list.runtimes[0].accounts.retain(|a| a.name == "primary");
            assert_eq!(list.runtimes[0].accounts.len(), 1);
            list.runtimes[0].accounts[0].login_check = result;
            list
        }
        async fn check(&self, retry: bool) -> Outcome {
            self.checks
                .check("codex".into(), "primary".into(), retry)
                .await
        }
        fn block(&self) {
            assert!(std::process::Command::new("/usr/bin/mkfifo")
                .arg(self.scratch.0.join("gate"))
                .status()
                .unwrap()
                .success());
        }
        async fn started(&self, calls: usize) {
            tokio::time::timeout(Duration::from_secs(15), async {
                while self.calls() < calls {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("fake check started");
        }
        fn release(&self) {
            fs::write(self.scratch.0.join("gate"), b"done").unwrap();
            fs::remove_file(self.scratch.0.join("gate")).unwrap();
        }
    }
    #[tokio::test]
    async fn concurrent_windows_join_active_result_instead_of_queueing_another_read() {
        let fake = Fake::new();
        fake.block();
        let popover = fake.checks.clone();
        let first =
            tokio::spawn(
                async move { popover.check("codex".into(), "primary".into(), false).await },
            );
        fake.started(1).await;
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Checking));
        // Poll the second command until it has joined before releasing the CLI.
        let second = fake.check(false);
        tokio::pin!(second);
        std::future::poll_fn(|cx| {
            assert!(second.as_mut().poll(cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
        fake.release();
        assert!(first.await.unwrap().is_ok());
        assert!(second.await.is_ok());
        assert_eq!(fake.calls(), 1);
    }
    #[tokio::test]
    async fn reopen_and_restart_seed_the_cached_timestamp_and_retained_result() {
        let fake = Fake::new();
        fake.check(false).await.unwrap();
        let result = fake.checks.snapshots()[0].result.clone();
        fake.checks.seed(&fake.cached(None)); // A stale listing cannot erase a result.
        fake.check(false).await.unwrap();
        let restarted =
            AccountChecks::with_clock(fake.checks.0.client.clone(), Arc::new(|| 1599.0), None);
        restarted.seed(&fake.cached(result));
        restarted
            .check("codex".into(), "primary".into(), false)
            .await
            .unwrap();
        assert_eq!(fake.calls(), 1);
        assert!(matches!(restarted.snapshots()[0].phase, Phase::Complete));
    }
    #[tokio::test]
    async fn throttle_runs_at_exactly_the_600_second_boundary_and_not_before() {
        let fake = Fake::new();
        fake.check(false).await.unwrap();
        fake.clock.store(1599, Ordering::SeqCst);
        fake.check(false).await.unwrap();
        fake.check(true).await.unwrap(); // Retry never bypasses a successful result.
        assert_eq!(fake.calls(), 1);
        fake.clock.store(1600, Ordering::SeqCst);
        fake.check(false).await.unwrap();
        assert_eq!(fake.calls(), 2);
    }
    #[tokio::test]
    async fn failed_result_is_retained_and_retry_bypasses_throttle_but_joins_active() {
        let fake = Fake::new();
        fake.answer(None, Some("fake login check failed"));
        fake.check(false).await.unwrap();
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Failed));
        fake.check(false).await.unwrap();
        assert_eq!(fake.calls(), 1);
        fake.answer(Some(true), None);
        fake.block();
        let checks = fake.checks.clone();
        let retry =
            tokio::spawn(async move { checks.check("codex".into(), "primary".into(), true).await });
        fake.started(2).await;
        let join = fake.check(true);
        tokio::pin!(join);
        std::future::poll_fn(|cx| {
            assert!(join.as_mut().poll(cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
        fake.release();
        retry.await.unwrap().unwrap();
        join.await.unwrap();
        assert_eq!(fake.calls(), 2);
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Complete));
    }
    #[tokio::test]
    async fn logged_out_and_never_checked_are_distinct_from_failure() {
        let fake = Fake::new();
        fake.checks.seed(&fake.cached(None));
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Idle));
        assert!(fake.checks.snapshots()[0].result.is_none());
        fake.answer(Some(false), None); // A completed check can prove the account is logged out.
        fake.check(false).await.unwrap();
        let snapshot = &fake.checks.snapshots()[0];
        assert!(matches!(snapshot.phase, Phase::Complete));
        assert_eq!(snapshot.result.as_ref().unwrap().logged_in, Some(false));
        assert!(snapshot.result.as_ref().unwrap().error.is_none());
    }
    #[tokio::test]
    async fn settings_check_within_throttle_of_success_runs_again() {
        let fake = Fake::new();
        fake.check(false).await.unwrap();
        fake.answer(Some(false), None);
        fake.checks
            .check_explicit("codex".into(), "primary".into())
            .await
            .unwrap();
        assert_eq!(fake.calls(), 2);
        assert_eq!(
            fake.checks.snapshots()[0]
                .result
                .as_ref()
                .unwrap()
                .logged_in,
            Some(false)
        );
    }
    #[tokio::test]
    async fn settings_check_within_throttle_of_failure_runs_again() {
        let fake = Fake::new();
        fake.answer(None, Some("login check failed"));
        fake.check(false).await.unwrap();
        fake.answer(Some(true), None);
        fake.checks
            .check_explicit("codex".into(), "primary".into())
            .await
            .unwrap();
        assert_eq!(fake.calls(), 2);
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Complete));
    }
    #[tokio::test]
    async fn settings_check_joins_active_popover_check_with_one_process() {
        let fake = Fake::new();
        fake.block();
        let popover = fake.checks.clone();
        let first =
            tokio::spawn(
                async move { popover.check("codex".into(), "primary".into(), false).await },
            );
        fake.started(1).await;
        let second = fake.checks.check_explicit("codex".into(), "primary".into());
        tokio::pin!(second);
        std::future::poll_fn(|cx| {
            assert!(second.as_mut().poll(cx).is_pending());
            std::task::Poll::Ready(())
        })
        .await;
        fake.release();
        first.await.unwrap().unwrap();
        second.await.unwrap();
        assert_eq!(fake.calls(), 1);
    }
    #[test]
    fn cached_logged_out_seeds_as_complete_instead_of_failed() {
        let fake = Fake::new();
        fake.checks.seed(&fake.cached(Some(LoginCheck {
            account: "primary".into(),
            logged_in: Some(false),
            checked_at: 1000.0,
            error: None,
        })));
        let row = &fake.checks.snapshots()[0];
        assert!(matches!(row.phase, Phase::Complete));
        assert_eq!(row.result.as_ref().unwrap().logged_in, Some(false));
        assert!(row.result.as_ref().unwrap().error.is_none());
    }
    #[tokio::test]
    async fn newer_cached_record_replaces_retained_result_and_timestamp() {
        let fake = Fake::new();
        fake.check(false).await.unwrap();
        fake.checks.seed(&fake.cached(Some(LoginCheck {
            account: "primary".into(),
            logged_in: Some(false),
            checked_at: 1100.0,
            error: None,
        })));
        assert_eq!(
            fake.checks.snapshots()[0]
                .result
                .as_ref()
                .unwrap()
                .logged_in,
            Some(false)
        );
        fake.clock.store(1600, Ordering::SeqCst);
        let result = fake.check(false).await.unwrap();
        assert!(
            matches!(result, CommandOutcome::Data(data) if matches!(*data, Snapshot::AccountsCheck(ref c) if c.accounts[0].logged_in == Some(false)))
        );
        assert_eq!(fake.calls(), 1); // The newer seed also moves the throttle.
    }
    #[tokio::test]
    async fn older_cached_record_cannot_replace_retained_result_or_timestamp() {
        let fake = Fake::new();
        fake.check(false).await.unwrap();
        fake.checks.seed(&fake.cached(Some(LoginCheck {
            account: "primary".into(),
            logged_in: None,
            checked_at: 900.0,
            error: Some("old failure".into()),
        })));
        let row = &fake.checks.snapshots()[0];
        assert!(matches!(row.phase, Phase::Complete));
        assert_eq!(row.result.as_ref().unwrap().logged_in, Some(true));
        fake.clock.store(1500, Ordering::SeqCst);
        fake.check(false).await.unwrap();
        assert_eq!(fake.calls(), 1);
        fake.clock.store(1600, Ordering::SeqCst);
        fake.check(false).await.unwrap();
        assert_eq!(fake.calls(), 2);
    }
    #[tokio::test]
    async fn repeated_open_skips_throttled_checks_and_usage_refresh() {
        let fake = Fake::new();
        assert_eq!(fake.checks.on_open(None).await.unwrap(), 1);
        assert_eq!(fake.checks.on_open(None).await.unwrap(), 0);
        assert_eq!(fake.calls(), 1);
        fake.clock.store(1600, Ordering::SeqCst);
        assert_eq!(fake.checks.on_open(None).await.unwrap(), 1);
        assert_eq!(fake.calls(), 2);
    }
    #[tokio::test]
    async fn failed_cached_list_still_checks_never_checked_usage_accounts() {
        for fallback in [true, false] {
            let fake = Fake::new();
            fs::write(fake.scratch.0.join("accounts.json"), "not json").unwrap();
            assert_eq!(
                fake.checks
                    .on_open(fallback.then(|| fake.usage()))
                    .await
                    .unwrap(),
                1
            );
            assert_eq!(fake.calls(), 1);
            assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Complete));
            assert_eq!(fake.checks.on_open(Some(fake.usage())).await.unwrap(), 0);
        }
    }
    #[tokio::test]
    async fn process_failure_is_published_and_retry_recovers_without_waiting() {
        let fake = Fake::new();
        fs::write(fake.scratch.0.join("result.json"), "not json").unwrap();
        assert!(fake.check(false).await.is_err());
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Failed));
        assert!(fake.checks.snapshots()[0]
            .result
            .as_ref()
            .unwrap()
            .error
            .is_some());
        assert!(fake.check(false).await.is_err());
        assert_eq!(fake.calls(), 1);
        fake.answer(Some(false), None);
        fake.check(true).await.unwrap();
        assert_eq!(fake.calls(), 2);
        assert!(matches!(fake.checks.snapshots()[0].phase, Phase::Complete));
    }
}
