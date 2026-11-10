//! The sole data/settings subprocess boundary. No shell and no frontend-selected executable.
use crate::models::{self, AgtError, CommandKind, NeedsChoice, Snapshot};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{process::Command, sync::oneshot, time::timeout};

#[derive(Debug, Clone)]
pub enum CommandOutcome {
    Data(Box<Snapshot>),
    Refused(AgtError),
    NeedsChoice(NeedsChoice),
    NeedsUserWords(AgtError),
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientError {
    InvalidRequest(String),
    Execution(String),
    TimedOut,
    InvalidResponse(String),
    Exit { code: Option<i32>, stderr: String },
}
impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for ClientError {}

#[derive(Debug, Clone)]
pub struct Request {
    pub command: CommandKind,
    pub workspace: Option<PathBuf>,
    /// Arguments chosen by a Rust command handler, never a program or shell string.
    pub arguments: Vec<String>,
    pub refresh: bool,
}
impl Request {
    pub fn new(command: CommandKind, workspace: Option<PathBuf>) -> Self {
        Self {
            command,
            workspace,
            arguments: Vec::new(),
            refresh: false,
        }
    }
    fn normalize(mut self) -> Result<Self, ClientError> {
        if let Some(path) = &self.workspace {
            if !path.is_absolute() {
                return Err(ClientError::InvalidRequest(
                    "workspace must be absolute".into(),
                ));
            }
            self.workspace = Some(
                path.canonicalize()
                    .map_err(|e| ClientError::InvalidRequest(e.to_string()))?,
            );
        }
        if self.refresh && self.command != CommandKind::Usage {
            return Err(ClientError::InvalidRequest(
                "only usage supports refresh".into(),
            ));
        }
        if self
            .arguments
            .iter()
            .any(|s| s == "--refresh" || s == "--json")
        {
            return Err(ClientError::InvalidRequest(
                "refresh and JSON flags are owned by AgtClient".into(),
            ));
        }
        Ok(self)
    }
    fn argv(&self) -> Vec<String> {
        let mut argv: Vec<String> = self.command.prefix().iter().map(|s| (*s).into()).collect();
        argv.extend(self.arguments.clone());
        if self.command == CommandKind::AccountsAddCheck {
            argv.push("--check".into());
        }
        argv.push("--json".into());
        if self.refresh {
            argv.push("--refresh".into());
        }
        argv
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Resource {
    command: CommandKind,
    workspace: Option<PathBuf>,
    arguments: Vec<String>,
}
impl From<&Request> for Resource {
    fn from(r: &Request) -> Self {
        Self {
            command: r.command,
            workspace: r.workspace.clone(),
            arguments: r.arguments.clone(),
        }
    }
}
#[derive(Debug)]
pub enum ReadReply {
    Fresh {
        generation: u64,
        result: Result<CommandOutcome, ClientError>,
    },
    Superseded {
        generation: u64,
    },
}
struct Batch {
    generation: u64,
    request: Request,
    waiters: Vec<oneshot::Sender<ReadReply>>,
}
struct Flight {
    active: bool,
    newest: u64,
    pending: Option<Batch>,
}
struct Inner {
    launcher: PathBuf,
    agt_home: Option<PathBuf>,
    deadline: Duration,
    flights: Mutex<HashMap<Resource, Flight>>,
}
#[derive(Clone)]
pub struct AgtClient {
    inner: Arc<Inner>,
}
impl AgtClient {
    pub fn new(launcher: PathBuf, agt_home: Option<PathBuf>) -> Result<Self, ClientError> {
        if !launcher.is_absolute() || agt_home.as_ref().is_some_and(|p| !p.is_absolute()) {
            return Err(ClientError::InvalidRequest(
                "launcher and explicit AGT_HOME must be absolute".into(),
            ));
        }
        Ok(Self {
            inner: Arc::new(Inner {
                launcher,
                agt_home,
                deadline: Duration::from_secs(30),
                flights: Mutex::new(HashMap::new()),
            }),
        })
    }

    /// One active read, one pending batch per resource/workspace. Receivers may be dropped safely.
    /// Callers publish only Fresh replies; superseded answers never become UI events.
    pub async fn read(
        &self,
        request: Request,
    ) -> Result<oneshot::Receiver<ReadReply>, ClientError> {
        let request = request.normalize()?;
        if !request.command.is_read() {
            return Err(ClientError::InvalidRequest("mutation is not a read".into()));
        }
        let key = Resource::from(&request);
        let (tx, rx) = oneshot::channel();
        let batch = {
            let mut flights = self.inner.flights.lock().expect("read flights lock");
            let flight = flights.entry(key.clone()).or_insert(Flight {
                active: false,
                newest: 0,
                pending: None,
            });
            flight.newest += 1;
            if flight.active {
                match &mut flight.pending {
                    Some(pending) => {
                        pending.generation = flight.newest;
                        pending.request.refresh |= request.refresh;
                        pending.waiters.push(tx);
                    }
                    None => {
                        flight.pending = Some(Batch {
                            generation: flight.newest,
                            request,
                            waiters: vec![tx],
                        })
                    }
                }
                return Ok(rx);
            }
            flight.active = true;
            Batch {
                generation: flight.newest,
                request,
                waiters: vec![tx],
            }
        };
        let client = self.clone();
        tokio::spawn(async move {
            client.drain(key, batch).await;
        });
        Ok(rx)
    }

    async fn drain(&self, key: Resource, mut batch: Batch) {
        loop {
            let result = self.execute(&batch.request).await;
            // Keep the generation comparison and publication atomic with new requests.
            let next = {
                let mut flights = self.inner.flights.lock().expect("read flights lock");
                let flight = flights.get_mut(&key).expect("active flight");
                for waiter in batch.waiters {
                    let reply = if batch.generation == flight.newest {
                        ReadReply::Fresh {
                            generation: batch.generation,
                            result: result.clone(),
                        }
                    } else {
                        ReadReply::Superseded {
                            generation: batch.generation,
                        }
                    };
                    let _ = waiter.send(reply);
                }
                let next = flight.pending.take();
                if next.is_none() {
                    flight.active = false;
                }
                next
            };
            match next {
                Some(next) => batch = next,
                None => break,
            }
        }
    }

    /// Writes are never coalesced, retried or replayed by the core.
    pub async fn mutate(&self, request: Request) -> Result<CommandOutcome, ClientError> {
        let request = request.normalize()?;
        if request.command.is_read() {
            return Err(ClientError::InvalidRequest(
                "use single-flight for reads".into(),
            ));
        }
        self.execute(&request).await
    }

    async fn execute(&self, request: &Request) -> Result<CommandOutcome, ClientError> {
        let mut command = Command::new(&self.inner.launcher);
        command
            .args(request.argv())
            .kill_on_drop(true)
            .stdin(std::process::Stdio::null())
            .env_remove("AGT_HOME")
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME");
        if let Some(path) = &request.workspace {
            command.current_dir(path);
        }
        if let Some(path) = &self.inner.agt_home {
            command.env("AGT_HOME", path);
        }
        let output = timeout(self.inner.deadline, command.output())
            .await
            .map_err(|_| ClientError::TimedOut)?
            .map_err(|e| ClientError::Execution(e.to_string()))?;
        interpret_exit(
            request.command,
            output.status.code(),
            &output.stdout,
            &output.stderr,
        )
    }
}

pub fn interpret_exit(
    kind: CommandKind,
    code: Option<i32>,
    stdout: &[u8],
    stderr: &[u8],
) -> Result<CommandOutcome, ClientError> {
    let failure = || ClientError::Exit {
        code,
        stderr: String::from_utf8_lossy(stderr).trim().into(),
    };
    // Signal termination and unexpected exit codes always stay execution errors, even with JSON.
    if !matches!(code, Some(0 | 2 | 3)) && !(kind == CommandKind::ShareStatus && code == Some(1)) {
        return Err(failure());
    }
    let decoded = models::decode(kind, stdout).map_err(|e| {
        if code == Some(0) {
            ClientError::InvalidResponse(e)
        } else {
            failure()
        }
    })?;
    match (code, decoded) {
        (Some(3), Snapshot::Choice(choice)) if kind == CommandKind::SharePick => {
            Ok(CommandOutcome::NeedsChoice(choice))
        }
        (Some(3), Snapshot::Error(error))
            if error.error.code.as_deref() == Some("needs_user_words")
                || error.error.needs_user_words == Some(true) =>
        {
            Ok(CommandOutcome::NeedsUserWords(error.error))
        }
        (Some(2), Snapshot::Error(error)) => Ok(CommandOutcome::Refused(error.error)),
        // A broken TOML file still returns the schema and rows with its diagnostic.
        (Some(2), Snapshot::Config(config))
            if kind == CommandKind::ConfigShow && config.error.is_some() =>
        {
            Ok(CommandOutcome::Data(Box::new(Snapshot::Config(config))))
        }
        (Some(0), Snapshot::Error(_)) | (Some(0), Snapshot::Choice(_)) => Err(
            ClientError::InvalidResponse("success exit carried a refusal".into()),
        ),
        (Some(0), data) => Ok(CommandOutcome::Data(Box::new(data))),
        (Some(1), Snapshot::ShareStatus(data))
            if kind == CommandKind::ShareStatus && data.enabled =>
        {
            Ok(CommandOutcome::Data(Box::new(Snapshot::ShareStatus(data))))
        }
        _ => Err(failure()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn exits_are_command_specific() {
        let status = br#"{"enabled":true,"attention":[{"message":"choose"}],"conflicts":["a"]}"#;
        assert!(matches!(
            interpret_exit(CommandKind::ShareStatus, Some(1), status, b""),
            Ok(CommandOutcome::Data(_))
        ));
        assert!(interpret_exit(CommandKind::Usage, Some(1), status, b"bad").is_err());
        assert!(interpret_exit(CommandKind::ShareStatus, Some(1), b"{}", b"bad").is_err());
        assert!(interpret_exit(CommandKind::ShareStatus, None, status, b"killed").is_err());
        let choice = include_bytes!("../../../tests/fixtures/app/share-pick-needs-choice.json");
        let words =
            include_bytes!("../../../tests/fixtures/app/roles-config-needs-user-words.json");
        assert!(matches!(
            interpret_exit(CommandKind::SharePick, Some(3), choice, b""),
            Ok(CommandOutcome::NeedsChoice(_))
        ));
        assert!(matches!(
            interpret_exit(CommandKind::RolesConfig, Some(3), words, b""),
            Ok(CommandOutcome::NeedsUserWords(_))
        ));
        assert!(matches!(
            interpret_exit(
                CommandKind::ConfigSet,
                Some(2),
                include_bytes!("../../../tests/fixtures/app/config-set-error.json"),
                b""
            ),
            Ok(CommandOutcome::Refused(_))
        ));
        assert!(matches!(
            interpret_exit(CommandKind::ConfigSet, Some(3), include_bytes!("../../../tests/fixtures/app/config-set-needs-user-words.json"), b""),
            Ok(CommandOutcome::NeedsUserWords(error)) if error.message.contains("max_depth")
        ));
        let mut broken: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../../tests/fixtures/app/config-show.json"
        ))
        .unwrap();
        broken["error"] = "/settings.toml:2: invalid TOML".into();
        assert!(
            matches!(interpret_exit(CommandKind::ConfigShow, Some(2), &serde_json::to_vec(&broken).unwrap(), b""),
            Ok(CommandOutcome::Data(data)) if matches!(*data, Snapshot::Config(_)))
        );
        assert!(interpret_exit(CommandKind::RolesConfig, Some(9), words, b"failed").is_err());
    }

    #[tokio::test]
    async fn late_answer_dropped_pending_refresh_kept_and_no_overlap() {
        let dir = Scratch::new("client");
        let launcher = dir.0.join("fake agt");
        fs::write(&launcher, "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$*\" >> calls\nif [ -f active ]; then touch overlap; fi\ntouch active\nif [ ! -f first ]; then touch first; while [ ! -f release ]; do /bin/sleep 0.01; done; fi\n/bin/rm active\nprintf '{\"runtimes\":[]}'\n").unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let client = AgtClient::new(launcher, Some(dir.0.clone())).unwrap();
        // Use Usage to exercise --refresh; a fake usage document is still the production model.
        let fixture = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/app/usage.json");
        let script = fs::read_to_string(&client.inner.launcher).unwrap().replace(
            "printf '{\"runtimes\":[]}'",
            &format!(
                "/bin/cat {}",
                crate::terminal::posix_quote(fixture.to_str().unwrap())
            ),
        );
        fs::write(&client.inner.launcher, script).unwrap();
        let plain = Request::new(CommandKind::Usage, Some(dir.0.clone()));
        let first = client.read(plain.clone()).await.unwrap();
        timeout(Duration::from_secs(5), async {
            while !dir.0.join("first").exists() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let mut refresh = plain.clone();
        refresh.refresh = true;
        let second = client.read(refresh).await.unwrap();
        let third = client.read(plain).await.unwrap();
        fs::write(dir.0.join("release"), "").unwrap();
        assert!(matches!(
            timeout(Duration::from_secs(5), first)
                .await
                .unwrap()
                .unwrap(),
            ReadReply::Superseded { generation: 1 }
        ));
        for rx in [second, third] {
            assert!(matches!(
                timeout(Duration::from_secs(5), rx).await.unwrap().unwrap(),
                ReadReply::Fresh {
                    generation: 3,
                    result: Ok(_)
                }
            ));
        }
        let calls = fs::read_to_string(dir.0.join("calls")).unwrap();
        assert_eq!(calls.lines().count(), 2);
        assert!(!calls.lines().next().unwrap().contains("--refresh"));
        assert!(calls.lines().nth(1).unwrap().contains("--refresh"));
        assert!(!dir.0.join("overlap").exists());
        let fourth = client
            .read(Request::new(CommandKind::Usage, Some(dir.0.clone())))
            .await
            .unwrap();
        assert!(matches!(
            fourth.await.unwrap(),
            ReadReply::Fresh {
                generation: 4,
                result: Ok(_)
            }
        ));
    }

    #[tokio::test]
    async fn missing_executable_stays_an_error() {
        let dir = Scratch::new("missing-client");
        let client = AgtClient::new(dir.0.join("no-such-agt"), None).unwrap();
        let reply = client
            .read(Request::new(CommandKind::Usage, None))
            .await
            .unwrap()
            .await
            .unwrap();
        assert!(matches!(
            reply,
            ReadReply::Fresh {
                result: Err(ClientError::Execution(_)),
                ..
            }
        ));
        assert!(AgtClient::new("agt".into(), None).is_err());
    }

    #[tokio::test]
    async fn workspace_is_part_of_single_flight_key() {
        let dir = Scratch::new("workspaces");
        let client = AgtClient::new(dir.0.join("missing"), None).unwrap();
        let a = dir.0.join("a");
        let b = dir.0.join("b");
        fs::create_dir(&a).unwrap();
        fs::create_dir(&b).unwrap();
        let first = client
            .read(Request::new(CommandKind::ConfigShow, Some(a)))
            .await
            .unwrap();
        let second = client
            .read(Request::new(CommandKind::ConfigShow, Some(b)))
            .await
            .unwrap();
        for rx in [first, second] {
            assert!(matches!(
                rx.await.unwrap(),
                ReadReply::Fresh { generation: 1, .. }
            ));
        }
    }

    #[tokio::test]
    async fn arguments_are_literal_and_only_explicit_agt_home_is_passed() {
        let dir = Scratch::new("client-argv");
        let launcher = dir.0.join("fake agt 'quoted'");
        fs::write(&launcher, "#!/bin/sh\nset -eu\nif [ -n \"${AGT_HOME+x}\" ]; then exit 50; fi\nprintf '%s\\n' \"$@\" > arguments\nprintf '{\"runtimes\":[]}'\n").unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let client = AgtClient::new(launcher, None).unwrap();
        let mut request = Request::new(CommandKind::AccountsList, Some(dir.0.clone()));
        request
            .arguments
            .push("space ' quote $(touch injected); `false`".into());
        let response = client.read(request).await.unwrap().await.unwrap();
        assert!(matches!(response, ReadReply::Fresh { result: Ok(_), .. }));
        assert_eq!(
            fs::read_to_string(dir.0.join("arguments")).unwrap(),
            "accounts\nlist\nspace ' quote $(touch injected); `false`\n--json\n"
        );
        assert!(!dir.0.join("injected").exists());
    }

    #[tokio::test]
    async fn timeout_kills_the_child_and_a_later_read_can_run() {
        let dir = Scratch::new("client-timeout");
        let launcher = dir.0.join("fake");
        fs::write(
            &launcher,
            "#!/bin/sh\necho $$ > child-pid\nexec /bin/sleep 30\n",
        )
        .unwrap();
        fs::set_permissions(&launcher, fs::Permissions::from_mode(0o700)).unwrap();
        let mut client = AgtClient::new(launcher.clone(), None).unwrap();
        // Allow macOS executable validation on a freshly written script before timing out.
        Arc::get_mut(&mut client.inner).unwrap().deadline = Duration::from_secs(2);
        let request = Request::new(CommandKind::AccountsList, Some(dir.0.clone()));
        let reply = client.read(request.clone()).await.unwrap().await.unwrap();
        assert!(matches!(
            reply,
            ReadReply::Fresh {
                result: Err(ClientError::TimedOut),
                ..
            }
        ));
        let pid = fs::read_to_string(dir.0.join("child-pid")).unwrap();
        timeout(Duration::from_secs(3), async {
            loop {
                let alive = Command::new("/bin/kill")
                    .args(["-0", pid.trim()])
                    .output()
                    .await
                    .unwrap()
                    .status
                    .success();
                if !alive {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("timed-out fake child is killed and reaped");
        fs::write(launcher, "#!/bin/sh\nprintf '{\"runtimes\":[]}'\n").unwrap();
        let reply = client.read(request).await.unwrap().await.unwrap();
        assert!(matches!(
            reply,
            ReadReply::Fresh {
                generation: 2,
                result: Ok(_),
                ..
            }
        ));
    }
}
