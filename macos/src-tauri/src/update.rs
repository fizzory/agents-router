//! Updater policy, independent of windows, clocks and the network.
use serde::Serialize;

pub const DAY: u64 = 24 * 60 * 60;
pub const RELEASES: &str = "https://github.com/fizzory/agents-router/releases/latest";

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
    fn download_and_install_failures_have_separate_retry_dialogs() {
        let download = Dialog::failure(Stage::Download, false, "connection interrupted".into());
        let install = Dialog::failure(Stage::Install, false, "disk full".into());
        assert_ne!(download.title, install.title);
        for d in [download, install] {
            assert_eq!(d.default, "install");
            assert_eq!(d.buttons.last().unwrap().0, "Retry");
        }
    }
}
