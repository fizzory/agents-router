//! Popover-only free Codex reads run independently of the usage read queue.
use crate::{
    client::{AgtClient, ClientError, CommandOutcome, Request},
    models::CommandKind,
};
use std::future::Future;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;
use tokio::time::timeout;

pub const TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Default, Clone)]
pub struct CodexProbe(Arc<AtomicBool>);

struct Active(Arc<AtomicBool>);
impl Drop for Active {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

impl CodexProbe {
    /// Reserve before the app spawns this future, including calls on AppKit's thread.
    pub fn start(
        &self,
        client: AgtClient,
        reason: &'static str,
    ) -> Option<impl Future<Output = Result<CommandOutcome, ClientError>> + Send + 'static> {
        if !matches!(reason, "open" | "manual")
            || self
                .0
                .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
                .is_err()
        {
            return None;
        }
        let active = Active(self.0.clone());
        Some(async move {
            let _active = active;
            timeout(
                TIMEOUT,
                client.mutate(Request::new(CommandKind::UsageProbeCodex, None)),
            )
            .await
            .map_err(|_| ClientError::TimedOut)?
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{client::ReadReply, models::Snapshot, terminal::tests::Scratch};
    use std::{fs, os::unix::fs::PermissionsExt, path::Path};

    fn fake(root: &Path) -> AgtClient {
        let gate = root.join("gate");
        assert!(std::process::Command::new("mkfifo")
            .arg(&gate)
            .status()
            .unwrap()
            .success());
        let launcher = root.join("agt");
        fs::write(
            &launcher,
            format!(
                "#!/bin/sh\nset -eu\ncd '{}'\ncase \"$*\" in\n\
             'usage --probe-codex --json')\n\
             echo probe >> calls\necho $$ > pid\n: > entered\nread -r release < gate\n\
             printf '%s\\n' '{{\"schema_version\":1,\"probed\":[\"primary\"],\"skipped\":[]}}';;\n\
             'usage --json')\n\
             echo usage >> calls\ncat '{}' ;;\n\
             *) exit 2 ;;\nesac\n",
                root.display(),
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../tests/fixtures/app/usage.json")
                    .display(),
            ),
        )
        .unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        AgtClient::with_env(launcher, vec![], Some(root.to_path_buf())).unwrap()
    }

    async fn entered(root: &Path) {
        timeout(Duration::from_secs(10), async {
            while !root.join("entered").exists() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("fake probe reached FIFO");
    }

    async fn release(root: &Path) {
        let gate = root.join("gate");
        tokio::task::spawn_blocking(move || fs::write(gate, "continue\n").unwrap())
            .await
            .unwrap();
    }

    #[test]
    fn appkit_can_reserve_without_a_tokio_context_and_drop_before_spawning() {
        let scratch = Scratch::new("codex-probe-before-spawn");
        let client = fake(&scratch.0);
        let probe = CodexProbe::default();
        let future = probe.start(client.clone(), "open").unwrap();
        assert!(probe.start(client.clone(), "open").is_none());
        drop(future);
        assert!(probe.start(client, "manual").is_some());
        assert!(!scratch.0.join("calls").exists());
    }

    #[test]
    fn versioned_result_decodes_empty_arrays_and_rejects_null_or_missing_arrays() {
        use crate::models::decode;
        assert_eq!(
            CommandKind::UsageProbeCodex.prefix(),
            &["usage", "--probe-codex"]
        );
        assert!(!CommandKind::UsageProbeCodex.is_read());
        for bytes in [
            include_bytes!("../../../tests/fixtures/app/usage-probe-codex.json").as_slice(),
            br#"{"schema_version":1,"probed":[],"skipped":[]}"#,
        ] {
            assert!(matches!(
                decode(CommandKind::UsageProbeCodex, bytes),
                Ok(Snapshot::UsageProbeCodex(_))
            ));
        }
        for bytes in [
            br#"{"schema_version":1,"probed":null,"skipped":[]}"#.as_slice(),
            br#"{"schema_version":1,"probed":[],"skipped":null}"#,
            br#"{"schema_version":1,"probed":[]}"#,
            br#"{"schema_version":2,"probed":[],"skipped":[]}"#,
        ] {
            assert!(decode(CommandKind::UsageProbeCodex, bytes).is_err());
        }
    }

    #[tokio::test]
    async fn open_and_manual_probe_once_without_blocking_usage_reads() {
        for reason in ["open", "manual"] {
            let scratch = Scratch::new("codex-probe");
            let client = fake(&scratch.0);
            let probe = CodexProbe::default();
            let task = tokio::spawn(probe.start(client.clone(), reason).expect("accepted probe"));
            entered(&scratch.0).await;
            assert!(probe.start(client.clone(), "open").is_none());
            assert!(probe.start(client.clone(), "manual").is_none());
            let rx = client
                .read(Request::new(CommandKind::Usage, None))
                .await
                .unwrap();
            let reply = timeout(Duration::from_secs(10), rx)
                .await
                .expect("read completes while probe held")
                .unwrap();
            assert!(
                matches!(reply, ReadReply::Fresh { result: Ok(CommandOutcome::Data(data)), .. }
                if matches!(*data, Snapshot::Usage(_)))
            );
            assert!(!task.is_finished(), "probe remains held by FIFO");
            assert_eq!(
                fs::read_to_string(scratch.0.join("calls")).unwrap(),
                "probe\nusage\n"
            );
            release(&scratch.0).await;
            assert!(
                matches!(task.await.unwrap().unwrap(), CommandOutcome::Data(data)
                if matches!(*data, Snapshot::UsageProbeCodex(_)))
            );
            assert!(!probe.0.load(Ordering::Acquire));
        }
    }

    #[tokio::test]
    async fn timer_wake_reactivation_and_watcher_never_probe() {
        let scratch = Scratch::new("codex-no-probe");
        let client = fake(&scratch.0);
        let probe = CodexProbe::default();
        for reason in [
            "timer",
            "wake",
            "reactivation",
            "debounced",
            "watcher",
            "startup",
            "account-check",
        ] {
            assert!(probe.start(client.clone(), reason).is_none(), "{reason}");
        }
        assert!(!scratch.0.join("calls").exists());
    }

    #[tokio::test]
    async fn timeout_releases_reservation_and_kills_the_child() {
        let scratch = Scratch::new("codex-probe-timeout");
        let client = fake(&scratch.0);
        let probe = CodexProbe::default();
        let task = tokio::spawn(probe.start(client.clone(), "open").unwrap());
        entered(&scratch.0).await;
        let pid: i32 = fs::read_to_string(scratch.0.join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        tokio::time::pause();
        tokio::time::advance(TIMEOUT + Duration::from_secs(1)).await;
        assert!(matches!(task.await.unwrap(), Err(ClientError::TimedOut)));
        assert!(!probe.0.load(Ordering::Acquire));
        // Start a replacement after timeout: no permanently held reservation.
        tokio::time::resume();
        timeout(Duration::from_secs(10), async {
            while unsafe { libc::kill(pid, 0) } == 0 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("timed-out child was killed and reaped");
        fs::remove_file(scratch.0.join("entered")).unwrap();
        let task = tokio::spawn(probe.start(client, "manual").unwrap());
        entered(&scratch.0).await;
        release(&scratch.0).await;
        assert!(task.await.unwrap().is_ok());
    }

    #[test]
    fn native_open_and_manual_refresh_are_the_only_app_probe_callers() {
        let app = include_str!("app.rs");
        let native = include_str!("native.rs");
        assert_eq!(app.matches("request_codex_probe(&app,").count(), 1);
        assert!(app.contains("request_codex_probe(&app, \"manual\");"));
        assert_eq!(native.matches("app::request_codex_probe(").count(), 1);
        let open = native
            .split("fn show_from(")
            .nth(1)
            .unwrap()
            .split("/// Called only")
            .next()
            .unwrap();
        assert!(open.contains("app::request_codex_probe(app, \"open\");"));
        let read = app
            .split("pub fn request_usage(")
            .nth(1)
            .unwrap()
            .split("/// Only the native")
            .next()
            .unwrap();
        assert!(!read.contains("request_codex_probe"));
    }
}
