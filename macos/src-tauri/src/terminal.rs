//! The explicit interactive-command exception: private, self-unlinking Terminal handoffs.
use serde::Serialize;
use std::{
    fs::{self, DirBuilder, OpenOptions},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
use tokio::process::Command;

static NEXT_HANDOFF: AtomicU64 = AtomicU64::new(0);

pub fn posix_quote(word: &str) -> String {
    format!("'{}'", word.replace('\'', "'\\''"))
}

#[derive(Debug, Clone)]
pub enum TerminalAction {
    Setup,
    Login { account: String, runtime: String },
    AddAccount { account: String, runtime: String },
}
impl TerminalAction {
    fn arguments(&self) -> Vec<String> {
        match self {
            Self::Setup => vec!["setup".into()],
            Self::Login { account, runtime } => vec![
                "accounts".into(),
                "login".into(),
                account.clone(),
                "--runtime".into(),
                runtime.clone(),
            ],
            Self::AddAccount { account, runtime } => vec![
                "accounts".into(),
                "add".into(),
                account.clone(),
                "--runtime".into(),
                runtime.clone(),
            ],
        }
    }
}

#[derive(Debug)]
pub struct Handoff {
    pub path: PathBuf,
    pub copy_command: String,
}

#[derive(Debug, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum LoginStatus {
    Opened,
    TerminalFailed,
}

#[derive(Debug, Serialize)]
pub struct LoginResult {
    pub status: LoginStatus,
    pub copy_command: String,
}

struct FormattedCommand {
    command: String,
    start_line: String,
    copy_command: String,
}

/// Pure formatting shared by the clipboard and the Terminal handoff. No files,
/// environment reads or shell evaluation are needed to obtain the command.
fn format_command(
    launcher: &Path,
    start: Option<&Path>,
    action: &TerminalAction,
) -> io::Result<FormattedCommand> {
    let launcher = launcher
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "launcher is not UTF-8"))?;
    let start = start
        .map(|folder| {
            folder.to_str().ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "start folder is not UTF-8")
            })
        })
        .transpose()?;
    let arguments = action.arguments();
    if launcher.contains('\0')
        || start.is_some_and(|s| s.contains('\0'))
        || arguments.iter().any(|arg| arg.contains('\0'))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Terminal argument contains NUL",
        ));
    }
    let command = std::iter::once(launcher)
        .chain(arguments.iter().map(String::as_str))
        .map(posix_quote)
        .collect::<Vec<_>>()
        .join(" ");
    let (start_line, copy_command) = match start {
        Some(folder) => (
            format!("cd -- {} || exit 1\n", posix_quote(folder)),
            format!("cd {} && {command}", posix_quote(folder)),
        ),
        None => (String::new(), command.clone()),
    };
    Ok(FormattedCommand {
        command,
        start_line,
        copy_command,
    })
}

pub struct TerminalLauncher {
    launcher: PathBuf,
    directory: PathBuf,
    /// A test build's private HOME: handoffs start in the HOME agt sees, as the
    /// app's own agt calls do. The launcher's AGT_EXCLUDE_HOMES, not this
    /// folder, keeps the person's own settings out. The release has none.
    start: Option<PathBuf>,
}
impl TerminalLauncher {
    pub fn new(launcher: PathBuf, directory: PathBuf) -> io::Result<Self> {
        if !launcher.is_absolute() || !directory.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Terminal launcher and private directory must be absolute",
            ));
        }
        Ok(Self {
            launcher,
            directory,
            start: None,
        })
    }
    pub fn starting_in(mut self, folder: PathBuf) -> io::Result<Self> {
        if !folder.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Terminal start folder must be absolute",
            ));
        }
        self.start = Some(folder);
        Ok(self)
    }
    pub fn command(&self, action: &TerminalAction) -> io::Result<String> {
        Ok(format_command(&self.launcher, self.start.as_deref(), action)?.copy_command)
    }
    pub fn prepare(&self, action: &TerminalAction) -> io::Result<Handoff> {
        let FormattedCommand {
            command,
            start_line: cd,
            copy_command,
        } = format_command(&self.launcher, self.start.as_deref(), action)?;
        match DirBuilder::new()
            .mode(0o700)
            .recursive(true)
            .create(&self.directory)
        {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e),
        }
        let metadata = fs::symlink_metadata(&self.directory)?;
        if !metadata.is_dir() || metadata.permissions().mode() & 0o077 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "handoff directory must be private (0700) and not a symlink",
            ));
        }
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let id = NEXT_HANDOFF.fetch_add(1, Ordering::Relaxed);
        let path = self
            .directory
            .join(format!("agt-{}-{nonce}-{id}.command", std::process::id()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o700)
            .open(&path)?;
        // Enforce the exact mode even with a restrictive umask. No credential or shell state is embedded.
        file.set_permissions(fs::Permissions::from_mode(0o700))?;
        let result = (|| {
            writeln!(
                file,
                "#!/bin/sh\n/bin/rm -f -- \"$0\" || exit 1\n{cd}exec {command}"
            )?;
            file.sync_all()
        })();
        if let Err(error) = result {
            let _ = fs::remove_file(&path);
            return Err(error);
        }
        Ok(Handoff { path, copy_command })
    }
    /// Opening Terminal is a handoff, not proof of success. Refresh on app reactivation.
    /// If opening fails the caller can offer copy_command and should remove the unused file.
    pub async fn open(&self, handoff: &Handoff) -> io::Result<()> {
        self.open_with(handoff, Path::new("/usr/bin/open")).await
    }
    async fn open_with(&self, handoff: &Handoff, opener: &Path) -> io::Result<()> {
        let result = Command::new(opener)
            .args(["-b", "com.apple.Terminal"])
            .arg(&handoff.path)
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .status()
            .await;
        match result {
            Ok(status) if status.success() => Ok(()),
            other => {
                let _ = fs::remove_file(&handoff.path);
                Err(match other {
                    Err(e) => e,
                    Ok(status) => {
                        io::Error::other(format!("Terminal could not be opened: {status}"))
                    }
                })
            }
        }
    }
    pub async fn login(&self, action: &TerminalAction) -> io::Result<LoginResult> {
        self.login_with(action, Path::new("/usr/bin/open")).await
    }
    async fn login_with(&self, action: &TerminalAction, opener: &Path) -> io::Result<LoginResult> {
        let handoff = self.prepare(action)?;
        let status = match self.open_with(&handoff, opener).await {
            Ok(()) => LoginStatus::Opened,
            Err(_) => LoginStatus::TerminalFailed,
        };
        Ok(LoginResult {
            status,
            copy_command: handoff.copy_command,
        })
    }
    pub fn directory(&self) -> &Path {
        &self.directory
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub struct Scratch(pub PathBuf);
    impl Scratch {
        pub fn new(label: &str) -> Self {
            let id = NEXT_HANDOFF.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("agt-app-test-{label}-{}-{id}", std::process::id()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn quoting_mode_and_self_unlink_preserve_literal_arguments() {
        let temp = Scratch::new("terminal");
        let launcher = temp.0.join("agt with 'quotes'");
        let result = temp.0.join("arguments");
        fs::write(
            &launcher,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > {}\n",
                posix_quote(result.to_str().unwrap())
            ),
        )
        .unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let terminal = TerminalLauncher::new(launcher, temp.0.join("private handoffs")).unwrap();
        let account = "a name's $(touch injected); `false`\nsecond line";
        let action = TerminalAction::Login {
            account: account.into(),
            runtime: "claude".into(),
        };
        let copy_command = terminal.command(&action).unwrap();
        assert!(
            !terminal.directory().exists(),
            "copying must not create handoff files or directories"
        );
        let handoff = terminal.prepare(&action).unwrap();
        assert_eq!(copy_command, handoff.copy_command);
        assert!(std::process::Command::new("/bin/sh")
            .args(["-c", &copy_command])
            .env("HOME", &temp.0)
            .current_dir(&temp.0)
            .status()
            .unwrap()
            .success());
        assert_eq!(
            fs::read_to_string(&result).unwrap(),
            format!("accounts\nlogin\n{account}\n--runtime\nclaude\n")
        );
        assert_eq!(
            fs::metadata(&handoff.path).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(terminal.directory())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        // Execute the generated file directly, with a fake launcher; never opens Terminal in a test.
        assert!(std::process::Command::new(&handoff.path)
            .env("HOME", &temp.0)
            .current_dir(&temp.0)
            .status()
            .unwrap()
            .success());
        assert!(!handoff.path.exists());
        assert_eq!(
            fs::read_to_string(result).unwrap(),
            format!("accounts\nlogin\n{account}\n--runtime\nclaude\n")
        );
        assert!(!temp.0.join("injected").exists());
        assert_eq!(posix_quote(""), "''");
    }
    #[test]
    fn test_build_handoffs_start_in_the_private_home() {
        let temp = Scratch::new("terminal-start");
        let base = temp.0.canonicalize().unwrap();
        let launcher = base.join("agt");
        let result = base.join("cwd");
        fs::write(
            &launcher,
            format!(
                "#!/bin/sh\npwd -P > {}\n",
                posix_quote(result.to_str().unwrap())
            ),
        )
        .unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let private = base.join("private home");
        let person = base.join("person");
        fs::create_dir_all(&private).unwrap();
        fs::create_dir_all(person.join(".agt")).unwrap();
        let release = TerminalLauncher::new(launcher.clone(), base.join("handoffs")).unwrap();
        let test = TerminalLauncher::new(launcher.clone(), base.join("handoffs"))
            .unwrap()
            .starting_in(private.clone())
            .unwrap();
        let plain = release.prepare(&TerminalAction::Setup).unwrap();
        assert_eq!(
            plain.copy_command,
            format!("{} 'setup'", posix_quote(launcher.to_str().unwrap()))
        );
        assert!(!fs::read_to_string(&plain.path).unwrap().contains("cd "));
        let handoff = test.prepare(&TerminalAction::Setup).unwrap();
        assert_eq!(
            test.command(&TerminalAction::Setup).unwrap(),
            handoff.copy_command
        );
        assert_eq!(
            handoff.copy_command,
            format!(
                "cd {} && {} 'setup'",
                posix_quote(private.to_str().unwrap()),
                posix_quote(launcher.to_str().unwrap())
            )
        );
        // Terminal opens in the person's own HOME; the handoff starts in the private one.
        assert!(std::process::Command::new(&handoff.path)
            .env("HOME", &private)
            .current_dir(&person)
            .status()
            .unwrap()
            .success());
        assert_eq!(
            fs::read_to_string(&result).unwrap().trim_end(),
            private.to_str().unwrap()
        );
        // The copied command has the same private start, even from another cwd.
        assert!(std::process::Command::new("/bin/sh")
            .args(["-c", &handoff.copy_command])
            .env("HOME", &private)
            .current_dir(&person)
            .status()
            .unwrap()
            .success());
        assert_eq!(
            fs::read_to_string(&result).unwrap().trim_end(),
            private.to_str().unwrap()
        );
        assert!(TerminalLauncher::new(launcher, base.join("handoffs"))
            .unwrap()
            .starting_in(PathBuf::from("relative"))
            .is_err());
    }
    #[test]
    fn refuses_public_or_symlinked_handoff_directory() {
        let temp = Scratch::new("terminal-private");
        let public = temp.0.join("public");
        fs::create_dir(&public).unwrap();
        fs::set_permissions(&public, fs::Permissions::from_mode(0o755)).unwrap();
        let terminal = TerminalLauncher::new(temp.0.join("agt"), public.clone()).unwrap();
        assert!(terminal.prepare(&TerminalAction::Setup).is_err());
        let link = temp.0.join("link");
        std::os::unix::fs::symlink(public, &link).unwrap();
        assert!(TerminalLauncher::new(temp.0.join("agt"), link)
            .unwrap()
            .prepare(&TerminalAction::Setup)
            .is_err());
    }
    #[test]
    fn command_only_rejects_nul_without_creating_files() {
        let temp = Scratch::new("command-only");
        let terminal = TerminalLauncher::new(temp.0.join("agt"), temp.0.join("handoffs")).unwrap();
        assert!(terminal
            .command(&TerminalAction::Login {
                account: "bad\0account".into(),
                runtime: "claude".into()
            })
            .is_err());
        assert!(!terminal.directory().exists());
    }
    #[tokio::test]
    async fn failed_terminal_open_returns_copy_command_and_removes_unused_file() {
        let temp = Scratch::new("failed-open");
        let terminal = TerminalLauncher::new(temp.0.join("agt"), temp.0.join("handoffs")).unwrap();
        let action = TerminalAction::Login {
            account: "work".into(),
            runtime: "claude".into(),
        };
        let fake = temp.0.join("fake-open");
        fs::write(&fake, "#!/bin/sh\nexit 1\n").unwrap();
        fs::set_permissions(&fake, fs::Permissions::from_mode(0o700)).unwrap();
        // Both spawn failure and a nonzero exit remove the unused handoff.
        for opener in [temp.0.join("missing-open"), fake] {
            let result = terminal.login_with(&action, &opener).await.unwrap();
            assert_eq!(result.status, LoginStatus::TerminalFailed);
            assert_eq!(result.copy_command, terminal.command(&action).unwrap());
            assert_eq!(fs::read_dir(terminal.directory()).unwrap().count(), 0);
            assert_eq!(
                serde_json::to_value(result).unwrap()["status"],
                "terminal_failed"
            );
        }
    }
    #[tokio::test]
    async fn opened_terminal_returns_the_same_command_and_keeps_its_handoff() {
        let temp = Scratch::new("opened");
        let terminal = TerminalLauncher::new(temp.0.join("agt"), temp.0.join("handoffs")).unwrap();
        let fake = temp.0.join("fake-open");
        fs::write(&fake, "#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&fake, fs::Permissions::from_mode(0o700)).unwrap();
        let action = TerminalAction::Login {
            account: "work".into(),
            runtime: "codex".into(),
        };
        let result = terminal.login_with(&action, &fake).await.unwrap();
        assert_eq!(result.status, LoginStatus::Opened);
        assert_eq!(result.copy_command, terminal.command(&action).unwrap());
        assert_eq!(fs::read_dir(terminal.directory()).unwrap().count(), 1);
        assert_eq!(serde_json::to_value(result).unwrap()["status"], "opened");
    }
}
