//! Watches directories rather than settings files so atomic renames are visible.
use notify_debouncer_mini::{
    new_debouncer,
    notify::{RecommendedWatcher, RecursiveMode},
    DebounceEventResult, Debouncer,
};
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
        let worker =
            thread::spawn(move || watch_loop(paths, debouncer, receiver, ready_tx, on_change));
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

fn watch_loop(
    paths: WatchPaths,
    mut debouncer: Debouncer<RecommendedWatcher>,
    receiver: mpsc::Receiver<Message>,
    ready: mpsc::Sender<Result<(), String>>,
    on_change: impl Fn(),
) {
    let mut registered = HashMap::new();
    if let Err(error) = reconcile(&paths, &mut debouncer, &mut registered) {
        let _ = ready.send(Err(error));
        return;
    }
    let _ = ready.send(Ok(()));
    let mut dirty: Option<Instant> = None;
    let mut previous_settings: HashSet<_> = paths
        .settings
        .iter()
        .filter_map(|p| p.canonicalize().ok())
        .collect();
    loop {
        match receiver.recv_timeout(Duration::from_millis(100)) {
            Ok(Message::Stop) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Ok(Message::Wake) => {
                dirty = Some(Instant::now());
            }
            Ok(Message::Events(Ok(events))) => {
                if events
                    .iter()
                    .any(|e| paths.relevant(&e.path) || previous_settings.contains(&e.path))
                {
                    // notify-debouncer-mini already debounced these events by one second.
                    dirty.get_or_insert(Instant::now() - DEBOUNCE);
                }
            }
            Ok(Message::Events(Err(_))) => {
                dirty.get_or_insert(Instant::now());
            }
            Err(mpsc::RecvTimeoutError::Timeout) => (),
        }
        match reconcile(&paths, &mut debouncer, &mut registered) {
            Ok(true) | Err(_) => {
                dirty.get_or_insert(Instant::now());
            }
            Ok(false) => (),
        }
        previous_settings = paths
            .settings
            .iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect();
        if dirty.is_some_and(|start| start.elapsed() >= DEBOUNCE) {
            dirty = None;
            on_change();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    #[test]
    fn wake_burst_is_one_debounced_read() {
        let temp = Scratch::new("wake");
        let (tx, rx) = mpsc::channel();
        let watcher = SettingsWatcher::start(
            WatchPaths {
                state: temp.0.join("state"),
                jobs: temp.0.join("jobs"),
                settings: vec![],
            },
            move || {
                let _ = tx.send(());
            },
        )
        .unwrap();
        let wake = watcher.wake_trigger();
        for _ in 0..3 {
            wake.notify();
        }
        assert!(rx.recv_timeout(Duration::from_millis(750)).is_err());
        rx.recv_timeout(Duration::from_secs(3)).expect("wake read");
        assert!(rx.recv_timeout(Duration::from_millis(1300)).is_err());
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
        let (tx, rx) = mpsc::channel();
        let _watcher = SettingsWatcher::start(
            WatchPaths {
                state,
                jobs,
                settings: vec![file.clone()],
            },
            move || {
                let _ = tx.send(());
            },
        )
        .unwrap();
        let swap = settings.join(".save");
        fs::write(&swap, "max_jobs=2").unwrap();
        fs::rename(swap, &file).unwrap();
        rx.recv_timeout(Duration::from_secs(8))
            .expect("atomic save observed");
        fs::rename(&settings, temp.0.join("old-config")).unwrap();
        fs::create_dir(&settings).unwrap();
        fs::write(&file, "max_jobs=3").unwrap();
        rx.recv_timeout(Duration::from_secs(8))
            .expect("folder replacement observed");
        // Wait for re-registration and drain replacement notifications before another atomic save.
        thread::sleep(Duration::from_millis(1400));
        while rx.try_recv().is_ok() {}
        let swap = settings.join(".save");
        fs::write(&swap, "max_jobs=4").unwrap();
        fs::rename(swap, file).unwrap();
        rx.recv_timeout(Duration::from_secs(8))
            .expect("new folder is watched");
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
        let _watcher = SettingsWatcher::start(
            WatchPaths {
                state: temp.0.join("state"),
                jobs: temp.0.join("jobs"),
                settings: vec![link],
            },
            move || {
                let _ = tx.send(());
            },
        )
        .unwrap();
        let swap = target_dir.join(".save");
        fs::write(&swap, "max_jobs=2").unwrap();
        fs::rename(swap, target).unwrap();
        rx.recv_timeout(Duration::from_secs(8))
            .expect("resolved target save observed");
    }
}
