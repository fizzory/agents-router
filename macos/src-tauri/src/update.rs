//! Updater policy, independent of windows, clocks and the network.
use serde::Serialize;

pub const DAY: u64 = 24 * 60 * 60;
pub const RELEASES: &str = "https://github.com/fizzory/agents-router/releases/latest";
/// Only scripts/build_app.py's release overlay sets this identifier; the base
/// config builds "Agents Router Test.app" with `<RELEASE_IDENTIFIER>.test`.
pub const RELEASE_IDENTIFIER: &str = "io.github.watcharagorn.agents-router";

/// Test builds never check or install from the production feed. The debug
/// updater rehearsal installs through its own localhost path, not this gate.
pub fn checks_feed(identifier: &str) -> bool {
    identifier == RELEASE_IDENTIFIER
}

#[derive(Default, Debug)]
pub struct Schedule {
    next: u64,
    running: bool,
    manual: bool,
}
impl Schedule {
    pub fn begin(&mut self, now: u64, manual: bool) -> bool {
        if self.running {
            self.manual |= manual;
            return false;
        }
        if !manual && now < self.next {
            return false;
        }
        self.running = true;
        self.manual = manual;
        true
    }
    pub fn finish(&mut self, now: u64) -> bool {
        self.running = false;
        self.next = now.saturating_add(DAY);
        std::mem::take(&mut self.manual)
    }
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Dialog {
    pub title: String,
    pub detail: String,
    pub buttons: Vec<(String, String)>,
    pub default: String,
    pub busy: bool,
}
impl Dialog {
    pub fn progress(title: &str) -> Self {
        Self {
            title: title.into(),
            detail: String::new(),
            buttons: vec![],
            default: String::new(),
            busy: true,
        }
    }
    fn choice(title: String, detail: String, label: &str, action: &str) -> Self {
        Self {
            title,
            detail,
            buttons: vec![
                ("Later".into(), "close".into()),
                (label.into(), action.into()),
            ],
            default: action.into(),
            busy: false,
        }
    }
    pub fn test_build() -> Self {
        Self {
            title: "Test builds don't update.".into(),
            detail: String::new(),
            buttons: vec![
                ("Open Releases Page".into(), "releases".into()),
                ("OK".into(), "close".into()),
            ],
            default: "close".into(),
            busy: false,
        }
    }
    pub fn failure(stage: Stage, permanent: bool, detail: String) -> Self {
        let title = match stage {
            Stage::Check => "Couldn't check for updates.",
            Stage::Download if permanent => "Couldn't verify the update.",
            Stage::Download => "Couldn't download the update.",
            Stage::Install => "Couldn't install the update.",
        };
        let (label, action) = if permanent {
            ("Open Releases Page", "releases")
        } else {
            (
                "Retry",
                if stage == Stage::Check {
                    "check"
                } else {
                    "install"
                },
            )
        };
        Self::choice(title.into(), detail, label, action)
    }
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Stage {
    Check,
    Download,
    Install,
}
impl Stage {
    /// The phase a failed install shows. A check failure has nothing to install.
    pub fn failed(self) -> Phase {
        match self {
            Stage::Check => Phase::Idle,
            Stage::Download => Phase::DownloadFailed,
            Stage::Install => Phase::InstallFailed,
        }
    }
}
/// The update's state for both windows, beside Settings' dialog.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Phase {
    #[default]
    Idle,
    Available,
    Downloading,
    Installing,
    DownloadFailed,
    InstallFailed,
}

pub enum CheckResult {
    Available(String),
    Current,
    Failed { permanent: bool, detail: String },
}
#[derive(Debug)]
pub struct Route {
    pub pending: Option<String>,
    pub popover: Option<String>,
    pub tray: Option<String>,
    pub dialog: Option<Dialog>,
}
pub fn route(result: CheckResult, manual: bool, install_problem: bool) -> Route {
    let pending = match &result {
        CheckResult::Available(v) => Some(v.clone()),
        _ => None,
    };
    let label = pending.as_ref().map(|v| format!("Update to {v}"));
    let dialog = if !manual {
        None
    } else {
        Some(match result {
            CheckResult::Available(v) => Dialog::choice(
                format!("Update to {v}?"),
                "Agents Router will restart after installing the update.".into(),
                &format!("Update to {v}"),
                "install",
            ),
            CheckResult::Current => Dialog {
                title: "You're up to date.".into(),
                detail: String::new(),
                buttons: vec![("OK".into(), "close".into())],
                default: "close".into(),
                busy: false,
            },
            CheckResult::Failed { permanent, detail } => {
                Dialog::failure(Stage::Check, permanent, detail)
            }
        })
    };
    Route {
        pending,
        popover: if install_problem { None } else { label.clone() },
        tray: label,
        dialog,
    }
}

pub fn permanent(error: &tauri_plugin_updater::Error) -> bool {
    use tauri_plugin_updater::Error::*;
    matches!(
        error,
        Minisign(_)
            | Base64(_)
            | SignatureUtf8(_)
            | SignedVersionMismatch { .. }
            | MissingSignedVersion
            | EmptyEndpoints
            | InsecureTransportProtocol
            | UnsupportedArch
            | UnsupportedOs
            | UrlParse(_)
            | TargetNotFound(_)
            | TargetsNotFound(_)
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn releases_is_the_latest_release_page_open_releases_opens() {
        assert!(RELEASES.ends_with("/releases/latest"), "{RELEASES}");
        // The updater feed lives under the same page, so the two cannot drift apart.
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(
            conf["plugins"]["updater"]["endpoints"],
            serde_json::json!([format!("{RELEASES}/download/latest.json")])
        );
        // Open Releases Page runs `open` on RELEASES and on nothing else.
        let source = include_str!("update_app.rs");
        let start = source.find("async fn open_releases_page(").unwrap();
        let page = &source[start..start + source[start..].find("\n}\n").unwrap()];
        assert!(page.contains("Command::new(\"/usr/bin/open\")"), "{page}");
        assert!(page.contains(".arg(update::RELEASES)"), "{page}");
        assert_eq!(page.matches(".arg(").count(), 1, "{page}");
        let open = source.find("pub async fn open_releases()").unwrap();
        let body = &source[open..open + source[open..].find("\n}\n").unwrap()];
        assert!(body.contains("open_releases_page().await"), "{body}");
    }
    #[test]
    fn overlapping_and_missed_checks_coalesce_and_manual_joins() {
        let mut s = Schedule::default();
        assert!(s.begin(0, false));
        assert!(!s.begin(1, false));
        assert!(!s.begin(2, true));
        assert!(!s.begin(DAY * 4, false));
        assert!(s.finish(DAY * 4));
        assert!(!s.begin(DAY * 4 + 1, false));
        assert!(s.begin(DAY * 9, false));
        assert!(!s.begin(DAY * 9, false));
        assert!(!s.finish(DAY * 9));
        assert!(s.begin(DAY * 9 + 1, true));
    }
    #[test]
    fn automatic_result_waits_in_both_slots_with_install_problem_priority() {
        for problem in [false, true] {
            let r = route(CheckResult::Available("0.17.1".into()), false, problem);
            assert_eq!(r.pending.as_deref(), Some("0.17.1"));
            assert_eq!(
                r.popover.as_deref(),
                if problem {
                    None
                } else {
                    Some("Update to 0.17.1")
                }
            );
            assert_eq!(r.tray.as_deref(), Some("Update to 0.17.1"));
            assert!(r.dialog.is_none());
        }
    }
    #[test]
    fn manual_results_always_alert_and_permanent_failures_open_releases() {
        for result in [
            CheckResult::Available("0.17.1".into()),
            CheckResult::Current,
            CheckResult::Failed {
                permanent: false,
                detail: "offline".into(),
            },
        ] {
            assert!(route(result, true, false).dialog.is_some());
        }
        let error = tauri_plugin_updater::Error::SignatureUtf8("bad".into());
        assert!(permanent(&error));
        for stage in [Stage::Check, Stage::Download, Stage::Install] {
            let d = Dialog::failure(stage, permanent(&error), "key or signature".into());
            assert_eq!(d.default, "releases");
            assert_eq!(d.buttons.last().unwrap().0, "Open Releases Page");
        }
        assert!(route(
            CheckResult::Failed {
                permanent: true,
                detail: "key".into()
            },
            false,
            false
        )
        .dialog
        .is_none());
    }
    #[test]
    fn only_the_release_identifier_checks_the_production_feed() {
        assert!(checks_feed("io.github.watcharagorn.agents-router"));
        for identifier in [
            "io.github.watcharagorn.agents-router.test",
            "io.github.watcharagorn.agents-router.test.update-42",
            "io.github.watcharagorn.agents-router.share",
            "",
        ] {
            assert!(!checks_feed(identifier), "{identifier}");
        }
        let d = Dialog::test_build();
        assert_eq!(d.title, "Test builds don't update.");
        assert!(d.detail.is_empty());
        assert_eq!(
            d.buttons,
            vec![
                ("Open Releases Page".to_string(), "releases".to_string()),
                ("OK".to_string(), "close".to_string()),
            ]
        );
        assert_eq!(d.default, "close");
        assert!(!d.busy);
    }
    #[test]
    fn download_and_install_failures_have_separate_retry_dialogs() {
        let download = Dialog::failure(Stage::Download, false, "connection interrupted".into());
        let install = Dialog::failure(Stage::Install, false, "disk full".into());
        assert_ne!(download.title, install.title);
        for d in [download, install] {
            assert_eq!(d.default, "install");
            assert_eq!(d.buttons.last().unwrap().0, "Retry");
        }
    }

    /// The popover's four failure titles are Settings' `Dialog::failure` titles
    /// with the version in place of "the update". rows.test.js reads the same file.
    #[test]
    fn popover_failure_titles_and_buttons_come_from_dialog_failure() {
        let cases: serde_json::Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/popover/update-failures.json"
        ))
        .unwrap();
        let cases = cases.as_array().unwrap();
        assert_eq!(cases.len(), 4);
        let mut seen = vec![];
        for case in cases {
            let stage = match case["phase"].as_str().unwrap() {
                "download-failed" => Stage::Download,
                "install-failed" => Stage::Install,
                other => panic!("unknown phase {other}"),
            };
            let retryable = case["retryable"].as_bool().unwrap();
            let version = case["version"].as_str().unwrap();
            let dialog = Dialog::failure(stage, !retryable, "detail".into());
            assert_eq!(
                dialog.title.replace("the update", version),
                case["title"].as_str().unwrap(),
                "{case}"
            );
            assert_eq!(dialog.detail, "detail");
            let (label, action) = dialog.buttons.last().unwrap();
            assert_eq!(label, case["button"].as_str().unwrap(), "{case}");
            assert_eq!(action, if retryable { "install" } else { "releases" });
            assert_eq!(dialog.default, *action);
            assert_eq!(serde_json::to_value(stage.failed()).unwrap(), case["phase"]);
            seen.push((stage, retryable));
        }
        for stage in [Stage::Download, Stage::Install] {
            for retryable in [true, false] {
                assert!(seen.contains(&(stage, retryable)), "{stage:?} {retryable}");
            }
        }
        assert_eq!(Stage::Check.failed(), Phase::Idle);
        assert_eq!(
            serde_json::to_value([Phase::DownloadFailed, Phase::InstallFailed, Phase::Available])
                .unwrap(),
            serde_json::json!(["download-failed", "install-failed", "available"])
        );
    }
}
