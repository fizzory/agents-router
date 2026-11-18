//! Watches directories rather than settings files so atomic renames are visible.
use notify_debouncer_mini::{
    new_debouncer,
    notify::{RecommendedWatcher, RecursiveMode},
    DebounceEventResult, Debouncer,
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
#[derive(Debug, Clone)]
pub struct WatchPaths {
    pub state: PathBuf,
    pub jobs: PathBuf,
    pub settings: Vec<PathBuf>,
}

impl WatchPaths {
    fn relevant(&self, event: &Path) -> bool {
        if [&self.state, &self.jobs].iter().any(|root| {
            let resolved = root.canonicalize().unwrap_or_else(|_| (*root).clone());
            event.starts_with(root)
                || root.starts_with(event)
                || event.starts_with(&resolved)
                || resolved.starts_with(event)
        }) {
            return true;
        }
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
}
impl Reads {
    fn new(paths: WatchPaths) -> Self {
        Self {
            settings: resolved(&paths.settings),
            relevance: Relevance::new(paths),
            dirty: None,
        }
    }
    /// A debounced batch of event paths; true when any of them counts.
    fn events<'a>(&mut self, paths: impl IntoIterator<Item = &'a Path>, now: Instant) -> bool {
        let mut relevant = false;
        // Every event, not just up to the first relevant one, so each
        // status.json's last-seen fields stay current.
        for path in paths {
            relevant |= self.relevance.relevant(path) || self.settings.contains(path);
        }
        if relevant {
            // notify-debouncer-mini already debounced these events by one second.
            self.dirty.get_or_insert(now - DEBOUNCE);
        }
        relevant
    }
    /// A wake: the read waits DEBOUNCE after the latest one.
    fn wake(&mut self, now: Instant) {
        self.dirty = Some(now);
    }
    /// Events may have gone unseen: the watcher reported an error, or a watched
    /// folder came, went or was replaced, perhaps the jobs folder, whose jobs'
    /// statuses may be another folder's now.
    fn missed(&mut self, now: Instant) {
        self.relevance.forget();
        self.dirty.get_or_insert(now);
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
    pub fn start(paths: WatchPaths, on_change: impl Fn() + Send + 'static) -> Result<Self, String> {
        Self::start_observed(paths, on_change, |_| ())
    }
    /// `start`, with `on_turn` told what each eventful turn of the watch loop
    /// did. Tests wait on it; the app passes a no-op.
    fn start_observed(
        paths: WatchPaths,
        on_change: impl Fn() + Send + 'static,
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
            watch_loop(paths, debouncer, receiver, ready_tx, on_change, on_turn)
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
}

fn watch_loop(
    paths: WatchPaths,
    mut debouncer: Debouncer<RecommendedWatcher>,
    receiver: mpsc::Receiver<Message>,
    ready: mpsc::Sender<Result<(), String>>,
    on_change: impl Fn(),
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
        if message.is_ok() || rewatched || read {
            on_turn(&Turn {
                woke: matches!(message, Ok(Message::Wake)),
                read,
                pending: reads.dirty.is_some(),
                watched: &registered,
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
    }
    /// A running watcher, what each read it started saw, and its turns.
    struct Watched {
        reads: mpsc::Receiver<String>,
        turns: mpsc::Receiver<Ack>,
        watcher: SettingsWatcher,
    }
    /// `observe` runs in each read, as a usage read would look at the files.
    fn watch(paths: WatchPaths, observe: impl Fn() -> String + Send + 'static) -> Watched {
        let (read_tx, reads) = mpsc::channel();
        let (turn_tx, turns) = mpsc::channel();
        let watcher = SettingsWatcher::start_observed(
            paths,
            move || {
                let _ = read_tx.send(observe());
            },
            move |turn: &Turn| {
                let _ = turn_tx.send(Ack {
                    woke: turn.woke,
                    read: turn.read,
                    pending: turn.pending,
                    watched: turn.watched.clone(),
                });
            },
        )
        .unwrap();
        Watched {
            reads,
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
        watched.watching(&jobs, true);
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
        )
        .unwrap();
        save(&target, "max_jobs=2");
        read_seeing(&rx, "max_jobs=2", "resolved target save observed");
    }
}
