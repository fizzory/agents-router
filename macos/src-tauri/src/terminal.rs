//! The explicit interactive-command exception: private, self-unlinking Terminal handoffs.
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

pub struct TerminalLauncher {
    launcher: PathBuf,
    directory: PathBuf,
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
        })
    }
    pub fn prepare(&self, action: &TerminalAction) -> io::Result<Handoff> {
        let launcher = self
            .launcher
            .to_str()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "launcher is not UTF-8"))?;
        let arguments = action.arguments();
        if launcher.contains('\0') || arguments.iter().any(|arg| arg.contains('\0')) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "Terminal argument contains NUL",
            ));
        }
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
        let copy_command = std::iter::once(launcher)
            .chain(arguments.iter().map(String::as_str))
            .map(posix_quote)
            .collect::<Vec<_>>()
            .join(" ");
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
                "#!/bin/sh\n/bin/rm -f -- \"$0\" || exit 1\nexec {copy_command}"
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
        let result = Command::new("/usr/bin/open")
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
        let handoff = terminal
            .prepare(&TerminalAction::Login {
                account: account.into(),
                runtime: "claude".into(),
            })
            .unwrap();
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
}
