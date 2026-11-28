//! Watches directories rather than settings files so atomic renames are visible.
use notify_debouncer_mini::{
    new_debouncer,
    notify::{RecommendedWatcher, RecursiveMode},
    DebounceEventResult, DebouncedEvent, Debouncer,
};
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

pub const DEBOUNCE: Duration = Duration::from_secs(1);
/// The files in a job folder that `agt usage` reads (usage_snapshot.py `_jobs`
/// and the lease rows). A running job also appends to `events.jsonl` and its
/// logs and leaves `write_json` temp files; none of those change usage.
const JOB_FILES: [&str; 4] = [
    "status.json",
    "runner.pid",
    "spec.json",
    "inbox-closed.json",
];
/// The status.json fields `agt usage` reads. The runner rewrites status.json
/// about every 2 s with only a new `updated_at` and `progress`; that heartbeat
/// is not a change. `updated_at` also ages a job whose runner died, which the
/// scheduled refresh catches.
const STATUS_FIELDS: [&str; 7] = [
    "state",
    "runner_pid",
    "child_pid",
    "child_started",
    "runtime",
    "account",
    "assigned_account",
];
/// Every `settings-refresh` kind the watcher sends, in the order it lists them.
const SETTINGS_KINDS: [&str; 3] = ["config", "share", "activity"];
#[derive(Debug, Clone)]
pub struct WatchPaths {
    pub state: PathBuf,
    pub jobs: PathBuf,
    pub settings: Vec<PathBuf>,
}

impl WatchPaths {
    fn state_entry<'a>(&self, event: &'a Path) -> Option<&'a Path> {
        event
            .strip_prefix(&self.state)
            .ok()
            .or_else(|| event.strip_prefix(self.state.canonicalize().ok()?).ok())
    }

    /// The state folder or a folder above it. An event there can stand for any
    /// file under it: FSEvents reports the events it dropped as one on a folder.
    fn covers_state(&self, event: &Path) -> bool {
        self.state.starts_with(event)
            || self
                .state
                .canonicalize()
                .is_ok_and(|root| root.starts_with(event))
    }

    fn relevant(&self, event: &Path) -> bool {
        if self.covers_state(event) {
            return true;
        }
        if let Some(entry) = self.state_entry(event) {
            return usage_state(entry, event);
        }
        self.settings_event(event)
    }

    fn settings_event(&self, event: &Path) -> bool {
        self.settings.iter().any(|file| {
            let target = file.canonicalize().unwrap_or_else(|_| file.clone());
            event == file || event == target || file.starts_with(event) || target.starts_with(event)
        })
    }
    fn watches(&self) -> HashMap<PathBuf, bool> {
        let mut result = HashMap::new();
        for root in [&self.state, &self.jobs] {
            add_watch(&mut result, root, true);
            if let Some(parent) = root.parent() {
                add_watch(&mut result, parent, false);
            }
            if let Ok(resolved) = root.canonicalize() {
                add_watch(&mut result, &resolved, true);
            }
        }
        for file in &self.settings {
            let mut candidates = vec![file.clone()];
            if let Ok(resolved) = file.canonicalize() {
                candidates.push(resolved);
            }
            // Also watch the target's parent if the symlink points at an as-yet absent file.
            if let Ok(target) = fs::read_link(file) {
                candidates.push(if target.is_absolute() {
                    target
                } else {
                    file.parent().unwrap_or(Path::new("/")).join(target)
                });
            }
            for path in candidates {
                if let Some(parent) = path.parent() {
                    add_watch(&mut result, parent, false);
                    if let Some(grandparent) = parent.parent() {
                        add_watch(&mut result, grandparent, false);
                    }
                }
            }
        }
        result
    }
}
/// notify-debouncer-mini reports paths, including folder events folded by FSEvents.
fn usage_state(entry: &Path, event: &Path) -> bool {
    if entry.as_os_str().is_empty()
        || entry == Path::new("claude-live")
        || entry == Path::new("leases")
    {
        return true;
    }
    let name = entry
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    if entry.starts_with("leases") {
        if let Some(slot) = name
            .strip_suffix(".lock")
            .or_else(|| name.strip_suffix(".owner.json"))
        {
            if !slot.is_empty() && slot.bytes().all(|b| b.is_ascii_digit()) {
                return true;
            }
        }
        // Removed lease folders have no metadata. Runtime and account folder names
        // usually have no extension; keep an ambiguous removal in that case.
        return event.is_dir() || (!event.exists() && entry.extension().is_none());
    }
    match entry.parent() {
        Some(parent) if parent == Path::new("claude-live") => name.ends_with(".json"),
        Some(parent) if parent.as_os_str().is_empty() => {
            matches!(
                name,
                "claude-profiles.json" | "account-checks.json" | "statusline.json"
            ) || ["-usage.json", "-health.json"].iter().any(|suffix| {
                name.strip_suffix(suffix)
                    .is_some_and(|runtime| !runtime.is_empty())
            })
        }
        _ => false,
    }
}

/// The Settings refresh kind of an entry under the state folder. An event on
/// `share` itself is none: the watcher's heartbeat makes that folder, each file
/// in it has its own event, and a visible Settings rereads Sharing on its
/// timer. Activity has no timer, so an event FSEvents folded onto `log` counts.
fn settings_state(entry: &Path) -> Option<&'static str> {
    if entry == Path::new("log") {
        return Some("activity");
    }
    let name = entry.file_name()?.to_str()?;
    match entry.parent()? {
        parent if parent == Path::new("share") => {
            // Atomic writes announce a temp file and then the destination.
            let temp = name
                .strip_prefix('.')
                .and_then(|s| s.strip_suffix(".tmp"))
                .and_then(|s| s.rsplit_once('.'))
                .is_some_and(|(destination, nonce)| {
                    !destination.is_empty()
                        && nonce.len() == 12
                        && nonce.bytes().all(|b| b.is_ascii_hexdigit())
                });
            let bookkeeping = matches!(
                name,
                "heartbeat.json"
                    | "signature.json"
                    | "sessions.json"
                    | "settings.good.toml"
                    | "launchd.log"
                    | "launcher.log"
                    | "notified.json"
                    | "shown.json"
                    | "hinted.json"
                    | "projects"
            );
            (!temp && !bookkeeping).then_some("share")
        }
        parent if parent == Path::new("log") && name.ends_with(".jsonl") => Some("activity"),
        _ => None,
    }
}
/// Which events change what `agt usage` reports. Unsure means yes: a path seen
/// for the first time and a status.json it can't read both count.
///
/// The cache holds what the watcher saw while it handled an event, not what the
/// usage read it started showed: that read runs later, on its own. So a
/// status.json that changes from Y to X and back to Y before its events are
/// handled can leave X on screen: a usage read running then samples X, and the
/// events compare Y with the cached Y and start nothing. The next successful
/// read of any kind corrects it, such as the scheduled refresh, opening the
/// popover or the app becoming active again (macos/README.md, "Tray and usage
/// reads"). If that staleness ever matters, reconcile the cache with the
/// per-job fields Python's read itself saw, and read again on a mismatch.
struct Relevance {
    paths: WatchPaths,
    /// Each job folder (under the configured jobs path), which folder it was and
    /// the STATUS_FIELDS of its status.json as the watcher last saw them. Holds
    /// only jobs on disk: a job folder event drops the jobs gone or replaced and
    /// stores that job's status as it is when the event is handled, and an event
    /// on the jobs folder or above, a changed watch or lost events drop them
    /// all. An empty cache holds nothing back, so clearing costs at most one
    /// more read, at the next heartbeats.
    statuses: HashMap<PathBuf, (Identity, Value)>,
}
impl Relevance {
    fn new(paths: WatchPaths) -> Self {
        Self {
            paths,
            statuses: HashMap::new(),
        }
    }
    fn relevant(&mut self, event: &Path) -> bool {
        let resolved = self.paths.jobs.canonicalize().ok();
        let roots = [Some(&self.paths.jobs), resolved.as_ref()];
        if roots.iter().flatten().any(|root| root.starts_with(event)) {
            // The jobs folder or a folder above it: any job may have changed.
            self.forget();
            return true;
        }
        let Some((job, rest)) = roots
            .iter()
            .flatten()
            .find_map(|root| event.strip_prefix(root).ok())
            .and_then(|rest| {
                let mut parts = rest.iter();
                Some((self.paths.jobs.join(parts.next()?), parts.next()))
            })
        else {
            return self.paths.relevant(event);
        };
        // A path deeper inside a job counts as its top entry there.
        match rest {
            None => {
                // A job came, went or was replaced, or FSEvents folded its
                // files' events into one on the folder. Store its status.json
                // as it is now, so a later status.json event is compared with
                // what the watcher saw here. The read this starts runs later
                // and may sample other fields (see the race on Relevance).
                self.prune();
                self.status(job);
                true
            }
            Some(name) if name == "status.json" => self.status(job),
            Some(name) => JOB_FILES.iter().any(|file| name == *file),
        }
    }
    /// Records the job's status; true when it differs from the last seen, or
    /// when the status or its folder can't be read.
    fn status(&mut self, job: PathBuf) -> bool {
        match identity(&job, false).zip(status_fields(&job.join("status.json"))) {
            Some(seen) => self.statuses.insert(job, seen.clone()) != Some(seen),
            None => {
                self.statuses.remove(&job);
                true
            }
        }
    }
    /// Keeps only the jobs whose folder is still the one seen.
    fn prune(&mut self) {
        self.statuses
            .retain(|job, (seen, _)| identity(job, false) == Some(*seen));
    }
    fn forget(&mut self) {
        self.statuses.clear();
    }
}
fn status_fields(path: &Path) -> Option<Value> {
    let status: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let status = status.as_object()?;
    Some(
        STATUS_FIELDS
            .iter()
            .map(|key| status.get(*key).cloned().unwrap_or(Value::Null))
            .collect(),
    )
}
/// When the watch loop reads usage again, apart from the watcher and the clock:
/// which events count, what the status cache keeps and when a read is due. The
/// loop hands it each message and the time; tests hand it paths and times.
struct Reads {
    relevance: Relevance,
    /// The settings files' targets as of the last turn, so an event on the old
    /// target of a settings symlink retargeted since still counts.
    settings: HashSet<PathBuf>,
    /// Since when a read is due; it starts once DEBOUNCE has passed.
    dirty: Option<Instant>,
    settings_kinds: Vec<&'static str>,
}
impl Reads {
    fn new(paths: WatchPaths) -> Self {
        Self {
            settings: resolved(&paths.settings),
            relevance: Relevance::new(paths),
            dirty: None,
            settings_kinds: Vec::new(),
        }
    }
    /// A debounced batch of event paths; true when any of them counts.
    fn events<'a>(&mut self, paths: impl IntoIterator<Item = &'a Path>, now: Instant) -> bool {
        let mut relevant = false;
        // Every event, not just up to the first relevant one, so each
        // status.json's last-seen fields stay current.
        for path in paths {
            let config = self.settings.contains(path) || self.relevance.paths.settings_event(path);
            relevant |= self.relevance.relevant(path) || config;
            if config {
                self.kind("config");
            }
            if self.relevance.paths.covers_state(path) {
                self.kind("share");
                self.kind("activity");
            } else if let Some(kind) = self
                .relevance
                .paths
                .state_entry(path)
                .and_then(settings_state)
            {
                self.kind(kind);
            }
        }
        if relevant {
            // notify-debouncer-mini already debounced these events by one second.
            self.dirty.get_or_insert(now - DEBOUNCE);
        }
        relevant
    }
    fn kind(&mut self, kind: &'static str) {
        if !self.settings_kinds.contains(&kind) {
            self.settings_kinds.push(kind);
        }
    }
    /// A wake: the read waits DEBOUNCE after the latest one.
    fn wake(&mut self, now: Instant) {
        self.dirty = Some(now);
    }
    /// Events may have gone unseen: the watcher reported an error, or a watched
    /// folder came, went or was replaced, perhaps the jobs folder, whose jobs'
    /// statuses may be another folder's now. Any settings, Sharing or Activity
    /// file may have changed too, so Settings gets every refresh kind.
    fn missed(&mut self, now: Instant) {
        self.relevance.forget();
        self.dirty.get_or_insert(now);
        for kind in SETTINGS_KINDS {
            self.kind(kind);
        }
    }
    /// Ends a turn: notes the settings targets, then takes the read if one is
    /// due.
    fn end_turn(&mut self, now: Instant) -> bool {
        self.settings = resolved(&self.relevance.paths.settings);
        let due = self
            .dirty
            .is_some_and(|start| now.saturating_duration_since(start) >= DEBOUNCE);
        if due {
            self.dirty = None;
        }
        due
    }
}
fn resolved(files: &[PathBuf]) -> HashSet<PathBuf> {
    files.iter().filter_map(|p| p.canonicalize().ok()).collect()
}
fn add_watch(map: &mut HashMap<PathBuf, bool>, path: &Path, recursive: bool) {
    let mut existing = path;
    while !existing.is_dir() {
        match existing.parent() {
            Some(parent) => existing = parent,
            None => return,
        }
    }
    map.entry(existing.to_path_buf())
        .and_modify(|r| *r |= recursive)
        .or_insert(recursive);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Identity {
    device: u64,
    inode: u64,
    recursive: bool,
}
fn identity(path: &Path, recursive: bool) -> Option<Identity> {
    fs::metadata(path).ok().map(|m| Identity {
        device: m.dev(),
        inode: m.ino(),
        recursive,
    })
}
enum Message {
    Events(DebounceEventResult),
    Wake,
    Stop,
    /// Tests: the loop waits here until the sender sends or is dropped, so the
    /// messages queued behind this one are all there when it goes on.
    #[cfg(test)]
    Hold(mpsc::Receiver<()>),
}
/// Wake notifications enter the same queue/deadline as filesystem notifications.
#[derive(Clone)]
pub struct WakeTrigger(mpsc::Sender<Message>);
impl WakeTrigger {
    pub fn notify(&self) {
        let _ = self.0.send(Message::Wake);
    }
}
pub struct SettingsWatcher {
    sender: mpsc::Sender<Message>,
    worker: Option<thread::JoinHandle<()>>,
}
impl SettingsWatcher {
    pub fn wake_trigger(&self) -> WakeTrigger {
        WakeTrigger(self.sender.clone())
    }
    pub fn start(
        paths: WatchPaths,
        on_change: impl Fn() + Send + 'static,
        on_settings: impl Fn(&[&str]) + Send + 'static,
    ) -> Result<Self, String> {
        Self::start_observed(paths, on_change, on_settings, |_| ())
    }
    /// `start`, with `on_turn` told what each eventful turn of the watch loop
    /// did. Tests wait on it; the app passes a no-op.
    fn start_observed(
        paths: WatchPaths,
        on_change: impl Fn() + Send + 'static,
        on_settings: impl Fn(&[&str]) + Send + 'static,
        on_turn: impl FnMut(&Turn) + Send + 'static,
    ) -> Result<Self, String> {
        if [&paths.state, &paths.jobs]
            .into_iter()
            .chain(paths.settings.iter())
            .any(|p| !p.is_absolute())
        {
            return Err("watch paths must be absolute".into());
        }
        let (sender, receiver) = mpsc::channel();
        let events = sender.clone();
        let debouncer = new_debouncer(DEBOUNCE, move |result| {
            let _ = events.send(Message::Events(result));
        })
        .map_err(|e| e.to_string())?;
        let (ready_tx, ready_rx) = mpsc::channel();
        let worker = thread::spawn(move || {
            watch_loop(
                paths,
                debouncer,
                receiver,
                ready_tx,
                on_change,
                on_settings,
                on_turn,
            )
        });
        match ready_rx.recv().map_err(|e| e.to_string())? {
            Ok(()) => Ok(Self {
                sender,
                worker: Some(worker),
            }),
            Err(error) => {
                let _ = sender.send(Message::Stop);
                let _ = worker.join();
                Err(error)
            }
        }
    }
}
impl Drop for SettingsWatcher {
    fn drop(&mut self) {
        let _ = self.sender.send(Message::Stop);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn reconcile(
    paths: &WatchPaths,
    debouncer: &mut Debouncer<RecommendedWatcher>,
    registered: &mut HashMap<PathBuf, Identity>,
) -> Result<bool, String> {
    let desired = paths.watches();
    let mut changed = false;
    let obsolete: Vec<_> = registered
        .iter()
        .filter(|(path, old)| {
            desired.get(*path).and_then(|r| identity(path, *r)).as_ref() != Some(*old)
        })
        .map(|(path, _)| path.clone())
        .collect();
    for path in obsolete {
        let _ = debouncer.watcher().unwatch(&path);
        registered.remove(&path);
        changed = true;
    }
    for (path, recursive) in desired {
        if registered.contains_key(&path) {
            continue;
        }
        if let Some(id) = identity(&path, recursive) {
            debouncer
                .watcher()
                .watch(
                    &path,
                    if recursive {
                        RecursiveMode::Recursive
                    } else {
                        RecursiveMode::NonRecursive
                    },
                )
                .map_err(|e| format!("watch {}: {e}", path.display()))?;
            registered.insert(path, id);
            changed = true;
        }
    }
    Ok(changed)
}

/// What one turn of the watch loop did, reported after its read (if any) has
/// started. Only turns that handled a message, changed a watch or started a
/// read are reported.
#[cfg_attr(not(test), allow(dead_code))]
struct Turn<'a> {
    woke: bool,
    /// A read started this turn.
    read: bool,
    /// A read is still due, after the debounce.
    pending: bool,
    /// The watches in place after this turn.
    watched: &'a HashMap<PathBuf, Identity>,
    /// The debounced events this turn handled.
    events: &'a [DebouncedEvent],
}

fn watch_loop(
    paths: WatchPaths,
    mut debouncer: Debouncer<RecommendedWatcher>,
    receiver: mpsc::Receiver<Message>,
    ready: mpsc::Sender<Result<(), String>>,
    on_change: impl Fn(),
    on_settings: impl Fn(&[&str]),
    mut on_turn: impl FnMut(&Turn),
) {
    let mut registered = HashMap::new();
    if let Err(error) = reconcile(&paths, &mut debouncer, &mut registered) {
        let _ = ready.send(Err(error));
        return;
    }
    let _ = ready.send(Ok(()));
    let mut reads = Reads::new(paths.clone());
    loop {
        let message = receiver.recv_timeout(Duration::from_millis(100));
        match &message {
            Ok(Message::Stop) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Ok(Message::Wake) => reads.wake(Instant::now()),
            Ok(Message::Events(Ok(events))) => {
                reads.events(events.iter().map(|e| e.path.as_path()), Instant::now());
            }
            Ok(Message::Events(Err(_))) => reads.missed(Instant::now()),
            #[cfg(test)]
            Ok(Message::Hold(release)) => {
                let _ = release.recv();
            }
            Err(mpsc::RecvTimeoutError::Timeout) => (),
        }
        // A watch added, dropped or failed: events in between may be lost.
        let rewatched = !matches!(
            reconcile(&paths, &mut debouncer, &mut registered),
            Ok(false)
        );
        if rewatched {
            reads.missed(Instant::now());
        }
        let read = reads.end_turn(Instant::now());
        if read {
            on_change();
        }
        if !reads.settings_kinds.is_empty() {
            on_settings(&reads.settings_kinds);
            reads.settings_kinds.clear();
        }
        if message.is_ok() || rewatched || read {
            on_turn(&Turn {
                woke: matches!(message, Ok(Message::Wake)),
                read,
                pending: reads.dirty.is_some(),
                watched: &registered,
                events: match &message {
                    Ok(Message::Events(Ok(events))) => events,
                    _ => &[],
                },
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    use serde_json::json;
    /// Like agt's fsutil.write_json: a temp file beside it, renamed over it.
    fn write_status(job: &Path, status: &Value) {
        let temp = job.join(".status.json.0123456789ab.tmp");
        fs::write(&temp, serde_json::to_vec(status).unwrap()).unwrap();
        fs::rename(temp, job.join("status.json")).unwrap();
    }
    fn running(updated_at: f64, progress: &str) -> Value {
        json!({"job_id": "j1", "state": "running", "runtime": "claude", "account": "work",
               "runner_pid": 41, "child_pid": 42, "child_started": 7, "updated_at": updated_at,
               "progress": progress, "message": "", "init": {"model": "opus"}})
    }
    fn append(path: &Path, line: &str) {
        use std::io::Write;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .unwrap();
        writeln!(file, "{line}").unwrap();
    }
    #[test]
    fn only_what_agt_usage_reads_in_a_job_folder_counts() {
        let temp = Scratch::new("relevance");
        let (state, jobs) = (temp.0.join("state"), temp.0.join("jobs"));
        let job = jobs.join("j1");
        fs::create_dir_all(&job).unwrap();
        fs::create_dir(&state).unwrap();
        let settings = temp.0.join("settings.toml");
        let mut relevance = Relevance::new(WatchPaths {
            state: state.clone(),
            jobs: jobs.clone(),
            settings: vec![settings.clone()],
        });
        let status = job.join("status.json");
        write_status(&job, &running(1.0, "1 tool call"));
        assert!(
            relevance.relevant(&status),
            "a status seen for the first time"
        );
        write_status(&job, &running(3.0, "4 tool calls"));
        assert!(!relevance.relevant(&status), "a heartbeat");
        let mut changed = running(5.0, "4 tool calls");
        changed["message"] = json!("still going");
        changed["init"] = json!({"model": "sonnet"});
        write_status(&job, &changed);
        assert!(!relevance.relevant(&status), "message and init aren't read");
        for (key, value) in [
            ("state", json!("done")),
            ("account", json!("home")),
            ("assigned_account", json!("home")),
            ("runtime", json!("codex")),
            ("runner_pid", json!(51)),
            ("child_pid", json!(52)),
            ("child_started", json!(8)),
        ] {
            changed[key] = value;
            write_status(&job, &changed);
            assert!(relevance.relevant(&status), "{key} changed");
            assert!(!relevance.relevant(&status), "{key} unchanged since");
        }
        // The canonical form FSEvents reports (/private/var for /var) is the same job.
        let canonical = jobs.canonicalize().unwrap().join("j1/status.json");
        write_status(&job, &running(9.0, "9 tool calls"));
        assert!(relevance.relevant(&canonical));
        write_status(&job, &running(11.0, "10 tool calls"));
        assert!(!relevance.relevant(&canonical));
        fs::write(&status, "{").unwrap();
        assert!(relevance.relevant(&status), "unreadable counts");
        fs::remove_file(&status).unwrap();
        assert!(relevance.relevant(&status), "removed counts");
        for noise in [
            "events.jsonl",
            "stderr-1.log",
            "runner.log",
            "result.md",
            ".status.json.0123456789ab.tmp",
            "notify.pending",
            "cancel",
            "homes/claude/.claude.json",
        ] {
            assert!(!relevance.relevant(&job.join(noise)), "{noise}");
        }
        for read in ["runner.pid", "spec.json", "inbox-closed.json"] {
            assert!(relevance.relevant(&job.join(read)), "{read}");
        }
        assert!(relevance.relevant(&job), "a job folder");
        assert!(relevance.relevant(&jobs), "the jobs folder");
        assert!(relevance.relevant(&temp.0), "a parent of the jobs folder");
        assert!(relevance.relevant(&state.join("leases/claude/work/0.lock")));
        assert!(relevance.relevant(&settings));
        assert!(!relevance.relevant(&temp.0.join("other.txt")));
    }
    fn jobs_relevance(name: &str) -> (Scratch, PathBuf, Relevance) {
        let temp = Scratch::new(name);
        let jobs = temp.0.join("jobs");
        fs::create_dir(&jobs).unwrap();
        let relevance = Relevance::new(WatchPaths {
            state: temp.0.join("state"),
            jobs: jobs.clone(),
            settings: vec![],
        });
        (temp, jobs, relevance)
    }
    fn new_job(jobs: &Path, name: &str, status: &Value) -> PathBuf {
        let job = jobs.join(name);
        fs::create_dir(&job).unwrap();
        write_status(&job, status);
        job.join("status.json")
    }
    fn cached(relevance: &Relevance) -> Vec<PathBuf> {
        let mut jobs: Vec<_> = relevance.statuses.keys().cloned().collect();
        jobs.sort();
        jobs
    }
    #[test]
    fn a_removed_job_leaves_the_cache() {
        let (_temp, jobs, mut relevance) = jobs_relevance("evict-removed");
        let one = new_job(&jobs, "j1", &running(1.0, "1"));
        let two = new_job(&jobs, "j2", &running(1.0, "1"));
        assert!(relevance.relevant(&one) && relevance.relevant(&two));
        assert_eq!(cached(&relevance), [jobs.join("j1"), jobs.join("j2")]);
        fs::remove_dir_all(jobs.join("j1")).unwrap();
        assert!(relevance.relevant(&jobs.join("j1")), "a job that went");
        assert_eq!(cached(&relevance), [jobs.join("j2")]);
        // Only its status.json's removal seen: the same.
        fs::remove_dir_all(jobs.join("j2")).unwrap();
        assert!(relevance.relevant(&two));
        assert!(relevance.statuses.is_empty());
        // Any job folder event drops every job gone, even one whose own events were lost.
        let three = new_job(&jobs, "j3", &running(1.0, "1"));
        assert!(relevance.relevant(&three));
        fs::remove_dir_all(jobs.join("j3")).unwrap();
        new_job(&jobs, "j4", &running(1.0, "1"));
        assert!(relevance.relevant(&jobs.join("j4")), "a job that came");
        assert_eq!(cached(&relevance), [jobs.join("j4")]);
    }
    #[test]
    fn a_job_folder_event_stores_its_status_as_seen() {
        let (_temp, jobs, mut relevance) = jobs_relevance("folder-refresh");
        let job = jobs.join("j1");
        let status = new_job(&jobs, "j1", &running(1.0, "1"));
        assert!(relevance.relevant(&status));
        // FSEvents folds the change into one event on the folder; the watcher
        // sees the job done.
        let mut done = running(3.0, "2");
        done["state"] = json!("done");
        write_status(&job, &done);
        assert!(relevance.relevant(&job), "a job folder");
        write_status(&job, &running(5.0, "3"));
        assert!(relevance.relevant(&status), "running again, as last seen");
        // A heartbeat after a folder event is still no change.
        assert!(relevance.relevant(&job), "a job folder");
        write_status(&job, &running(7.0, "4"));
        assert!(!relevance.relevant(&status), "a heartbeat");
        // A folder event with no readable status.json drops the job.
        fs::write(&status, "{").unwrap();
        assert!(relevance.relevant(&job), "a job folder");
        assert!(relevance.statuses.is_empty());
        write_status(&job, &running(9.0, "5"));
        assert!(relevance.relevant(&status), "readable again");
        fs::remove_file(&status).unwrap();
        assert!(relevance.relevant(&job), "a job folder");
        assert!(relevance.statuses.is_empty());
    }
    #[test]
    fn a_renamed_job_is_a_new_one() {
        let (_temp, jobs, mut relevance) = jobs_relevance("evict-renamed");
        let status = new_job(&jobs, "j1", &running(1.0, "1"));
        assert!(relevance.relevant(&status));
        fs::rename(jobs.join("j1"), jobs.join("j2")).unwrap();
        assert!(relevance.relevant(&jobs.join("j1")));
        assert!(relevance.statuses.is_empty());
        let moved = jobs.join("j2/status.json");
        assert!(relevance.relevant(&moved), "first seen under its new name");
        // Its folder's event too: stored as seen, so the same fields don't count.
        assert!(relevance.relevant(&jobs.join("j2")));
        assert_eq!(cached(&relevance), [jobs.join("j2")]);
        write_status(&jobs.join("j2"), &running(3.0, "2"));
        assert!(!relevance.relevant(&moved), "a heartbeat");
    }
    #[test]
    fn a_job_folder_back_under_its_old_name_is_new() {
        let (_temp, jobs, mut relevance) = jobs_relevance("evict-reused");
        let mut done = running(1.0, "1");
        done["state"] = json!("done");
        let status = new_job(&jobs, "j1", &done);
        assert!(relevance.relevant(&status));
        // Removed and made again with no event seen for the folder: the same
        // fields in another folder still count.
        fs::remove_dir_all(jobs.join("j1")).unwrap();
        new_job(&jobs, "j1", &done);
        assert!(relevance.relevant(&status));
        write_status(&jobs.join("j1"), &done);
        assert!(!relevance.relevant(&status));
    }
    #[test]
    fn a_replaced_jobs_folder_clears_the_cache() {
        let (temp, jobs, mut relevance) = jobs_relevance("evict-root");
        let mut done = running(1.0, "1");
        done["state"] = json!("done");
        let status = new_job(&jobs, "j1", &done);
        assert!(relevance.relevant(&status));
        // A jobs folder made elsewhere and moved in: only the jobs folder's
        // own events arrive, not its jobs'.
        let staged = temp.0.join("staged");
        fs::create_dir(&staged).unwrap();
        new_job(&staged, "j1", &running(1.0, "1"));
        fs::rename(&jobs, temp.0.join("old-jobs")).unwrap();
        fs::rename(&staged, &jobs).unwrap();
        assert!(relevance.relevant(&jobs), "the jobs folder");
        assert!(relevance.statuses.is_empty());
        write_status(&jobs.join("j1"), &done);
        assert!(relevance.relevant(&status), "done, as the old j1 was");
        assert!(!relevance.relevant(&status));
        // A folder above the jobs folder, in either form, clears it too.
        let canonical = temp.0.canonicalize().unwrap();
        for above in [&temp.0, &canonical, &jobs.canonicalize().unwrap()] {
            relevance.relevant(&status); // seen again after the last clear
            assert_eq!(cached(&relevance), [jobs.join("j1")]);
            assert!(relevance.relevant(above), "{}", above.display());
            assert!(relevance.statuses.is_empty(), "{}", above.display());
        }
        assert!(!relevance.relevant(&temp.0.join("old-jobs")));
        assert!(!relevance.relevant(&temp.0.join("old-jobs/j1/status.json")));
    }
    fn reads_for(name: &str) -> (Scratch, PathBuf, Reads) {
        let temp = Scratch::new(name);
        let jobs = temp.0.join("jobs");
        fs::create_dir(&jobs).unwrap();
        let reads = Reads::new(WatchPaths {
            state: temp.0.join("state"),
            jobs: jobs.clone(),
            settings: vec![],
        });
        (temp, jobs, reads)
    }
    #[test]
    fn usage_state_paths_match_what_usage_reads() {
        let (temp, _, mut reads) = reads_for("state-inputs");
        let state = temp.0.join("state");
        fs::create_dir_all(state.join("leases/provider/team.name")).unwrap();
        fs::write(state.join("leases/unrelated"), "").unwrap();
        for name in [
            "",
            "new-runtime-usage.json",
            "another-health.json",
            "claude-profiles.json",
            "account-checks.json",
            "statusline.json",
            "claude-live",
            "claude-live/home.json",
            "leases",
            "leases/provider",
            "leases/provider/team.name",
            "leases/123.lock",
            "leases/provider/work/0.owner.json",
            "leases/provider/work/04.lock",
        ] {
            assert!(reads.relevance.relevant(&state.join(name)), "{name}");
        }
        assert!(reads.relevance.relevant(&temp.0));
        for name in [
            "share/manifest.json",
            "log/changes.jsonl",
            "groups/g",
            "sessions/s.json",
            "sync/user.json",
            "backups/copy",
            "statusline-backups/s.json",
            "codex-probes/a.json",
            "locks/2.lock",
            "3.lock",
            "leases/unrelated",
            "leases/x.lock",
            "leases/1.json",
            "leases/3.owner.json.bak",
            "leases/٢.lock",
            "unknown.json",
            "-usage.json",
            "claude-live/home.txt",
            "claude-live/other/deeper.json",
            "other/codex-usage.json",
            ".codex-usage.json.0123456789ab.tmp",
            "leases/.3.lock.0123456789ab.tmp",
        ] {
            assert!(!reads.relevance.relevant(&state.join(name)), "{name}");
        }
    }

    #[test]
    fn share_heartbeat_and_its_atomic_write_start_no_reads() {
        let (temp, _, mut reads) = reads_for("share-heartbeat");
        let share = temp.0.join("state/share");
        let events = [
            share.join(".heartbeat.json.0123456789ab.tmp"),
            share.join("heartbeat.json"),
        ];
        assert_eq!(batch(&mut reads, &events, Instant::now()), (false, false));
        assert!(reads.settings_kinds.is_empty());
    }

    #[test]
    fn settings_refresh_kinds_follow_only_user_config_share_and_activity() {
        let (temp, _, mut reads) = reads_for("settings-kinds");
        let config = temp.0.join("config/settings.toml");
        let state = temp.0.join("state");
        reads.relevance.paths.settings.push(config.clone());
        let now = Instant::now();
        let (share, activity, none): (&[&str], &[&str], &[&str]) = (&["share"], &["activity"], &[]);
        for (name, expected) in [
            ("share", none),
            ("share/user.json", share),
            ("share/.unrelated.tmp", share),
            ("log", activity),
            ("log/settings.jsonl", activity),
            ("log/nested/a.jsonl", none),
            ("log/a.log", none),
            ("share/projects", none),
            ("share/projects/user.json", none),
            ("share/heartbeat.json", none),
            ("share/signature.json", none),
            ("share/sessions.json", none),
            ("share/settings.good.toml", none),
            ("share/launchd.log", none),
            ("share/launcher.log", none),
            ("share/notified.json", none),
            ("share/shown.json", none),
            ("share/hinted.json", none),
            ("share/.manifest.json.0123456789ab.tmp", none),
            ("claude-live/a.json", none),
            // The state folder: its event can stand for any file under it.
            ("", &["share", "activity"]),
        ] {
            let path = state.join(name);
            reads.events([path.as_path()], now);
            assert_eq!(
                std::mem::take(&mut reads.settings_kinds),
                expected,
                "{name}"
            );
        }
        // A folder above both the state folder and the settings file.
        reads.events([temp.0.as_path()], now);
        assert_eq!(
            std::mem::take(&mut reads.settings_kinds),
            SETTINGS_KINDS,
            "above"
        );
        let mixed = [
            config,
            state.join("share/user.json"),
            state.join("log/a.jsonl"),
            state.join("share/other.json"),
        ];
        reads.events(mixed.iter().map(PathBuf::as_path), now);
        assert_eq!(reads.settings_kinds, ["config", "share", "activity"]);
    }
    /// One watch loop turn that handles a debounced batch of `paths` at `now`:
    /// whether the batch counted, and whether a read started.
    fn batch(reads: &mut Reads, paths: &[PathBuf], now: Instant) -> (bool, bool) {
        let relevant = reads.events(paths.iter().map(PathBuf::as_path), now);
        (relevant, reads.end_turn(now))
    }
    #[test]
    fn a_running_jobs_heartbeat_and_output_read_nothing() {
        let (_temp, jobs, mut reads) = reads_for("heartbeat");
        let job = jobs.join("20261117-000000-abcdef");
        fs::create_dir(&job).unwrap();
        fs::write(job.join("spec.json"), "{}").unwrap();
        write_status(&job, &running(1.0, "starting"));
        let temp_file = job.join(".status.json.0123456789ab.tmp");
        let status = job.join("status.json");
        let (events, log) = (job.join("events.jsonl"), job.join("stderr-1.log"));
        let start = Instant::now();
        let started = [
            job.clone(),
            job.join("spec.json"),
            temp_file.clone(),
            status.clone(),
        ];
        assert_eq!(
            batch(&mut reads, &started, start),
            (true, true),
            "a new job"
        );
        let beats = [temp_file, status.clone(), events.clone(), log.clone()];
        for beat in 0..8 {
            write_status(
                &job,
                &running(2.0 + f64::from(beat), &format!("{beat} tool calls")),
            );
            append(&events, r#"{"type":"assistant"}"#);
            append(&log, "warning");
            let now = start + DEBOUNCE * (beat + 1);
            assert_eq!(
                batch(&mut reads, &beats, now),
                (false, false),
                "heartbeat {beat}"
            );
        }
        assert!(!reads.end_turn(start + DEBOUNCE * 20), "no read left due");
        let mut done = running(20.0, "8 tool calls");
        done["state"] = json!("done");
        write_status(&job, &done);
        let now = start + DEBOUNCE * 21;
        assert_eq!(
            batch(&mut reads, &[status], now),
            (true, true),
            "a finished job"
        );
    }
    #[test]
    fn a_job_folder_event_then_a_heartbeat_reads_once() {
        let (_temp, jobs, mut reads) = reads_for("folder-heartbeat");
        let job = jobs.join("j1");
        let status = new_job(&jobs, "j1", &running(1.0, "1"));
        let start = Instant::now();
        // FSEvents folded the job's start into one event on its folder.
        assert_eq!(batch(&mut reads, &[job.clone()], start), (true, true));
        write_status(&job, &running(3.0, "2"));
        let now = start + DEBOUNCE;
        assert_eq!(
            batch(&mut reads, &[status.clone()], now),
            (false, false),
            "a heartbeat"
        );
        let now = start + DEBOUNCE * 2;
        assert_eq!(
            batch(&mut reads, &[status], now),
            (false, false),
            "unchanged"
        );
    }
    #[test]
    fn every_event_in_a_batch_updates_the_cache() {
        let (_temp, jobs, mut reads) = reads_for("whole-batch");
        let one = new_job(&jobs, "j1", &running(1.0, "1"));
        let two = new_job(&jobs, "j2", &running(1.0, "1"));
        let start = Instant::now();
        let both = [one, two.clone()];
        assert_eq!(batch(&mut reads, &both, start), (true, true));
        let mut done = running(3.0, "2");
        done["state"] = json!("done");
        write_status(&jobs.join("j2"), &done);
        // Something that counts comes first in the batch; j2's new fields are
        // still stored.
        let mixed = [jobs.join("j1/spec.json"), two.clone()];
        assert_eq!(batch(&mut reads, &mixed, start + DEBOUNCE), (true, true));
        done["updated_at"] = json!(5.0);
        write_status(&jobs.join("j2"), &done);
        let now = start + DEBOUNCE * 2;
        assert_eq!(
            batch(&mut reads, &[two], now),
            (false, false),
            "j2's heartbeat"
        );
    }
    #[test]
    fn missed_events_clear_the_cache_and_read_after_the_debounce() {
        // The loop calls `missed` for a watcher error (lost events) and for a
        // watch added, dropped or replaced.
        let (_temp, jobs, mut reads) = reads_for("missed");
        let status = new_job(&jobs, "j1", &running(1.0, "1"));
        let start = Instant::now();
        assert_eq!(batch(&mut reads, &[status.clone()], start), (true, true));
        assert_eq!(batch(&mut reads, &[status.clone()], start), (false, false));
        reads.missed(start);
        assert!(reads.relevance.statuses.is_empty());
        // Settings, Sharing and Activity files may have changed unseen too.
        assert_eq!(reads.settings_kinds, SETTINGS_KINDS);
        assert!(!reads.end_turn(start));
        // The same fields count again, and the read stays due from the miss.
        let half = start + DEBOUNCE / 2;
        assert_eq!(batch(&mut reads, &[status], half), (true, false));
        assert!(!reads.end_turn(start + DEBOUNCE - Duration::from_nanos(1)));
        assert!(reads.end_turn(start + DEBOUNCE));
        assert!(!reads.end_turn(start + DEBOUNCE * 3), "one read");
    }
    #[test]
    fn a_wake_burst_is_one_read_a_debounce_after_its_last_wake() {
        let (_temp, _jobs, mut reads) = reads_for("wake-burst");
        let start = Instant::now();
        let step = Duration::from_millis(1);
        let wakes = [start, start + step, start + step * 2];
        for at in wakes {
            reads.wake(at);
            assert!(!reads.end_turn(at));
        }
        let last = wakes[2];
        assert!(!reads.end_turn(last + DEBOUNCE - Duration::from_nanos(1)));
        assert!(reads.end_turn(last + DEBOUNCE));
        assert!(!reads.end_turn(last + DEBOUNCE * 3), "one read");
    }
    #[test]
    fn a_settings_links_old_target_counts_until_the_turn_ends() {
        let temp = Scratch::new("retarget");
        let (old, new) = (temp.0.join("old.toml"), temp.0.join("new.toml"));
        fs::write(&old, "max_jobs=1").unwrap();
        fs::write(&new, "max_jobs=2").unwrap();
        let link = temp.0.join("settings.toml");
        std::os::unix::fs::symlink(&old, &link).unwrap();
        let mut reads = Reads::new(WatchPaths {
            state: temp.0.join("state"),
            jobs: temp.0.join("jobs"),
            settings: vec![link.clone()],
        });
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&new, &link).unwrap();
        let old = [old.canonicalize().unwrap()];
        let start = Instant::now();
        assert_eq!(
            batch(&mut reads, &old, start),
            (true, true),
            "the target as of the last turn"
        );
        let now = start + DEBOUNCE;
        assert_eq!(
            batch(&mut reads, &old, now),
            (false, false),
            "no longer a target"
        );
    }
    /// The longest a test waits on the watcher. Only a failing test gets near it.
    const WATCHDOG: Duration = Duration::from_secs(30);
    /// A `Turn`, owned so it can reach the test thread.
    struct Ack {
        woke: bool,
        read: bool,
        pending: bool,
        watched: HashMap<PathBuf, Identity>,
        events: Vec<PathBuf>,
    }
    /// A running watcher, what each read it started saw, the kinds of each
    /// Settings refresh it sent, and its turns.
    struct Watched {
        reads: mpsc::Receiver<String>,
        kinds: mpsc::Receiver<Vec<String>>,
        turns: mpsc::Receiver<Ack>,
        watcher: SettingsWatcher,
    }
    /// `observe` runs in each read, as a usage read would look at the files.
    fn watch(paths: WatchPaths, observe: impl Fn() -> String + Send + 'static) -> Watched {
        let (read_tx, reads) = mpsc::channel();
        let (kind_tx, kinds) = mpsc::channel();
        let (turn_tx, turns) = mpsc::channel();
        let watcher = SettingsWatcher::start_observed(
            paths,
            move || {
                let _ = read_tx.send(observe());
            },
            move |kinds: &[&str]| {
                let _ = kind_tx.send(kinds.iter().map(|kind| (*kind).to_owned()).collect());
            },
            move |turn: &Turn| {
                let _ = turn_tx.send(Ack {
                    woke: turn.woke,
                    read: turn.read,
                    pending: turn.pending,
                    watched: turn.watched.clone(),
                    events: turn.events.iter().map(|event| event.path.clone()).collect(),
                });
            },
        )
        .unwrap();
        Watched {
            reads,
            kinds,
            turns,
            watcher,
        }
    }
    /// Waits for a read that saw `expected`. A read started by earlier work
    /// that ran before the edit saw the old value, so it doesn't count.
    fn read_seeing(reads: &mpsc::Receiver<String>, expected: &str, what: &str) {
        let deadline = Instant::now() + WATCHDOG;
        let mut seen = Vec::new();
        while seen.last().map(String::as_str) != Some(expected) {
            let left = deadline.saturating_duration_since(Instant::now());
            match reads.recv_timeout(left) {
                Ok(value) => seen.push(value),
                Err(error) => panic!("{what}: {error}; reads saw {seen:?}"),
            }
        }
    }
    impl Watched {
        fn read(&self, expected: &str, what: &str) {
            read_seeing(&self.reads, expected, what);
        }
        fn turn(&self) -> Ack {
            self.turns
                .recv_timeout(WATCHDOG)
                .expect("a watch loop turn")
        }
        /// Waits until the watcher watches `folder` as it is now and no read
        /// is still due, so the next edit's read isn't one already scheduled.
        fn watching(&self, folder: &Path, recursive: bool) {
            let now = identity(folder, recursive);
            assert!(now.is_some(), "{}", folder.display());
            loop {
                let turn = self.turn();
                if turn.watched.get(folder) == now.as_ref() && !turn.pending {
                    break;
                }
            }
        }
        /// Waits for a Settings refresh of exactly these kinds, in any order.
        fn refreshed(&self, expected: &[&str], what: &str) {
            let mut expected = expected.to_vec();
            expected.sort_unstable();
            let deadline = Instant::now() + WATCHDOG;
            let mut seen = Vec::new();
            loop {
                let left = deadline.saturating_duration_since(Instant::now());
                match self.kinds.recv_timeout(left) {
                    Ok(mut kinds) => {
                        kinds.sort_unstable();
                        if kinds == expected {
                            return;
                        }
                        seen.push(kinds);
                    }
                    Err(error) => panic!("{what}: {error}; refreshes were {seen:?}"),
                }
            }
        }
        /// Waits for the turn that handled an event on `path`, which exists.
        /// FSEvents reports changes in order and the debouncer holds each path
        /// for the same time, so every earlier change has been handled by
        /// then. A turn makes its callbacks before it is reported.
        fn handled(&self, path: &Path) {
            let resolved = path.canonicalize().unwrap();
            while !self
                .turn()
                .events
                .iter()
                .any(|event| event == path || *event == resolved)
            {}
        }
        /// Takes the callbacks made so far: what each read saw, and the kinds
        /// of each Settings refresh.
        fn callbacks(&self) -> (Vec<String>, Vec<Vec<String>>) {
            (
                self.reads.try_iter().collect(),
                self.kinds.try_iter().collect(),
            )
        }
        /// Holds the watch loop at this point in its queue until the returned
        /// sender sends or is dropped. Declare it after `self`, so a failing
        /// test drops it first and the watcher can stop.
        fn hold(&self) -> mpsc::Sender<()> {
            let (release, hold) = mpsc::channel();
            self.watcher.sender.send(Message::Hold(hold)).unwrap();
            release
        }
    }
    /// What a read sees of job j1: `state/account`, or `gone`.
    fn job_seen(jobs: &Path) -> String {
        let Ok(bytes) = fs::read(jobs.join("j1/status.json")) else {
            return "gone".into();
        };
        let status: Value = serde_json::from_slice(&bytes).unwrap_or_default();
        let field = |key: &str| status[key].as_str().unwrap_or("?").to_owned();
        format!("{}/{}", field("state"), field("account"))
    }
    /// A watcher on a jobs folder that already holds one running job.
    fn watch_a_job(name: &str) -> (Scratch, PathBuf, Watched) {
        let temp = Scratch::new(name);
        let (state, jobs) = (temp.0.join("state"), temp.0.join("jobs"));
        fs::create_dir(&state).unwrap();
        fs::create_dir(&jobs).unwrap();
        new_job(&jobs, "j1", &running(1.0, "1"));
        let seen = jobs.clone();
        let watched = watch(
            WatchPaths {
                state,
                jobs: jobs.clone(),
                settings: vec![],
            },
            move || job_seen(&seen),
        );
        (temp, jobs, watched)
    }
    #[test]
    fn a_finished_job_is_read() {
        let (_temp, jobs, watched) = watch_a_job("watch-done");
        let mut done = running(3.0, "2");
        done["state"] = json!("done");
        write_status(&jobs.join("j1"), &done);
        watched.read("done/work", "a finished job is read");
    }
    #[test]
    fn a_removed_job_is_read() {
        let (_temp, jobs, watched) = watch_a_job("watch-removed");
        fs::remove_dir_all(jobs.join("j1")).unwrap();
        watched.read("gone", "a removed job is read");
    }
    #[test]
    fn a_jobs_folder_moved_in_is_read_and_watched() {
        let (temp, jobs, watched) = watch_a_job("watch-root");
        // Another jobs folder moved in place of this one, its j1 on another account.
        let staged = temp.0.join("staged");
        fs::create_dir(&staged).unwrap();
        let mut moved = running(1.0, "1");
        moved["account"] = json!("home");
        new_job(&staged, "j1", &moved);
        fs::rename(&jobs, temp.0.join("old-jobs")).unwrap();
        fs::rename(&staged, &jobs).unwrap();
        watched.read("running/home", "the new jobs folder is read");
        watched.refreshed(&SETTINGS_KINDS, "a replaced folder refreshes Settings");
        watched.watching(&jobs, true);
    }
    #[test]
    fn a_retargeted_agt_home_is_read_and_refreshes_settings() {
        let temp = Scratch::new("watch-home");
        for home in ["one", "two"] {
            let home = temp.0.join(home);
            fs::create_dir_all(home.join("state")).unwrap();
            fs::create_dir(home.join("jobs")).unwrap();
            let name = home.file_name().unwrap().to_str().unwrap();
            fs::write(home.join("state/codex-usage.json"), name).unwrap();
        }
        // AGT_HOME is a link, as a dotfiles setup makes it.
        let link = temp.0.join("home");
        std::os::unix::fs::symlink(temp.0.join("one"), &link).unwrap();
        let seen = link.join("state/codex-usage.json");
        let watched = watch(
            WatchPaths {
                state: link.join("state"),
                jobs: link.join("jobs"),
                settings: vec![],
            },
            move || fs::read_to_string(&seen).unwrap_or_default(),
        );
        fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(temp.0.join("two"), &link).unwrap();
        watched.read("two", "the new home is read");
        watched.refreshed(&SETTINGS_KINDS, "the new home refreshes Settings");
        watched.watching(&link.join("state"), true);
    }
    /// agt's fsutil.write_private, as `write_json` calls it: the folder made
    /// owner-only when it's missing, a `.<name>.<12 hex>.tmp` beside the file,
    /// renamed over it.
    fn write_private(path: &Path, nonce: &str, text: &str) {
        use std::os::unix::fs::DirBuilderExt;
        let folder = path.parent().unwrap();
        if !folder.exists() {
            fs::DirBuilder::new().mode(0o700).create(folder).unwrap();
        }
        let name = path.file_name().unwrap().to_str().unwrap();
        let temp = folder.join(format!(".{name}.{nonce}.tmp"));
        fs::write(&temp, text).unwrap();
        fs::rename(temp, path).unwrap();
    }
    #[test]
    fn a_share_heartbeat_starts_no_usage_read_and_no_sharing_refresh() {
        let temp = Scratch::new("watch-heartbeat");
        let (state, jobs) = (temp.0.join("state"), temp.0.join("jobs"));
        fs::create_dir(&state).unwrap();
        fs::create_dir(&jobs).unwrap();
        let usage = state.join("codex-usage.json");
        let seen = usage.clone();
        let watched = watch(
            WatchPaths {
                state: state.clone(),
                jobs,
                settings: vec![],
            },
            move || fs::read_to_string(&seen).unwrap_or_default(),
        );
        // Making the scratch folders can reach the watcher late and start a
        // read. A file written now is handled after them.
        let log = state.join("log");
        fs::create_dir(&log).unwrap();
        let ready = log.join("2026-10.jsonl");
        append(&ready, "{}");
        watched.handled(&ready);
        watched.callbacks();
        let heartbeat = state.join("share/heartbeat.json");
        let month = log.join("2026-11.jsonl");
        // The watcher's first tick makes state/share; the second finds it there.
        for (tick, nonce) in ["0123456789ab", "ba9876543210"].into_iter().enumerate() {
            write_private(&heartbeat, nonce, r#"{"result": "nothing moved"}"#);
            // Written after the heartbeat, so handled no earlier than it.
            append(&month, r#"{"type":"setting"}"#);
            watched.handled(&month);
            let (reads, kinds) = watched.callbacks();
            assert_eq!(kinds, [["activity"]], "tick {tick}");
            assert!(reads.is_empty(), "tick {tick}: reads saw {reads:?}");
        }
        write_private(&usage, "00112233aabb", "after");
        watched.handled(&usage);
        assert_eq!(watched.callbacks(), (vec!["after".to_owned()], vec![]));
    }
    /// How many reads a burst makes depends on the loop's timing: a stall
    /// longer than the debounce splits it. The exact coalescing, on injected
    /// time, is `a_wake_burst_is_one_read_a_debounce_after_its_last_wake`.
    #[test]
    fn queued_wakes_reach_the_loop_and_are_read() {
        let temp = Scratch::new("wake");
        let watched = watch(
            WatchPaths {
                state: temp.0.join("state"),
                jobs: temp.0.join("jobs"),
                settings: vec![],
            },
            String::new,
        );
        let wake = watched.watcher.wake_trigger();
        // All three wakes queued before the loop handles the first.
        let release = watched.hold();
        for _ in 0..3 {
            wake.notify();
        }
        drop(release);
        let (mut wakes, mut reads) = (0, 0);
        while wakes < 3 || reads == 0 {
            let turn = watched.turn();
            wakes += usize::from(turn.woke);
            reads += usize::from(turn.read);
        }
    }
    #[test]
    fn lost_events_are_read() {
        let temp = Scratch::new("lost");
        let watched = watch(
            WatchPaths {
                state: temp.0.join("state"),
                jobs: temp.0.join("jobs"),
                settings: vec![],
            },
            String::new,
        );
        let lost = notify_debouncer_mini::notify::Error::generic("events lost");
        watched
            .watcher
            .sender
            .send(Message::Events(Err(lost)))
            .unwrap();
        watched.read("", "lost events are read");
        watched.refreshed(&SETTINGS_KINDS, "lost events refresh Settings");
    }
    /// An editor's atomic save: a temp file renamed over the settings file.
    fn save(file: &Path, text: &str) {
        let swap = file.with_file_name(".save");
        fs::write(&swap, text).unwrap();
        fs::rename(swap, file).unwrap();
    }
    #[test]
    fn atomic_save_and_replaced_folder_are_seen() {
        let temp = Scratch::new("watch");
        let settings = temp.0.join("config");
        fs::create_dir(&settings).unwrap();
        let file = settings.join("settings.toml");
        fs::write(&file, "max_jobs=1").unwrap();
        let state = temp.0.join("state");
        let jobs = temp.0.join("jobs");
        fs::create_dir(&state).unwrap();
        fs::create_dir(&jobs).unwrap();
        let seen = file.clone();
        let watched = watch(
            WatchPaths {
                state,
                jobs,
                settings: vec![file.clone()],
            },
            move || fs::read_to_string(&seen).unwrap_or_default(),
        );
        save(&file, "max_jobs=2");
        watched.read("max_jobs=2", "atomic save observed");
        fs::rename(&settings, temp.0.join("old-config")).unwrap();
        fs::create_dir(&settings).unwrap();
        fs::write(&file, "max_jobs=3").unwrap();
        watched.read("max_jobs=3", "folder replacement observed");
        watched.watching(&settings, false);
        save(&file, "max_jobs=4");
        watched.read("max_jobs=4", "new folder is watched");
    }
    #[test]
    fn symlink_target_atomic_save_is_seen() {
        let temp = Scratch::new("watch-link");
        let target_dir = temp.0.join("target");
        fs::create_dir(&target_dir).unwrap();
        let target = target_dir.join("real.toml");
        fs::write(&target, "max_jobs=1").unwrap();
        let link = temp.0.join("settings.toml");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let (tx, rx) = mpsc::channel();
        let seen = target.clone();
        let _watcher = SettingsWatcher::start(
            WatchPaths {
                state: temp.0.join("state"),
                jobs: temp.0.join("jobs"),
                settings: vec![link],
            },
            move || {
                let _ = tx.send(fs::read_to_string(&seen).unwrap_or_default());
            },
            |_| (),
        )
        .unwrap();
        save(&target, "max_jobs=2");
        read_seeing(&rx, "max_jobs=2", "resolved target save observed");
    }
}
