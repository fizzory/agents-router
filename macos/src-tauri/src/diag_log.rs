//! The diagnostic log: one file that keeps the update steps, app errors, start,
//! exit and panics across launches. From Finder, stderr is /dev/null.
//!
//! The release writes `~/Library/Logs/<release identifier>/agents-router.log`; a
//! test build writes `<app support>/logs/<identifier>/agents-router.log`, so it
//! follows `--test-app-support` and never reaches the release's file. Each record
//! is one line, sanitized, written and flushed at once: restart and exit run no
//! destructors, so nothing may wait in a buffer. Logging never changes what the
//! app does: an I/O failure goes to stderr only.
use crate::app_paths::{self, AppPaths};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock, TryLockError},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const FILE_NAME: &str = "agents-router.log";
pub const ROTATED_NAME: &str = "agents-router.log.1";
/// Rotate before a write would take the file past this size: at most about
/// twice this on disk, with the one `.1` file.
pub const MAX_BYTES: u64 = 1024 * 1024;
/// The longest record, newline included.
pub const MAX_RECORD: usize = 4096;
pub const TRUNCATED: &str = " …[truncated]";
/// The log crate targets the bridge forwards: the updater plugin's (a prefix)
/// and Tauri's restart, by exact target. Every other target is dropped.
pub const UPDATER_TARGET: &str = "tauri_plugin_updater";
pub const PROCESS_TARGET: &str = "tauri::process";
pub const APP_TARGET: &str = "tauri::app";
/// The updater plugin's records worth keeping. Everything else, notably
/// "update response:", "parsed release response", "using proxy" and
/// "Executing updater", carries the raw feed or settings and is dropped.
pub const BRIDGED: [&str; 10] = [
    "checking for updates",
    "update endpoint returned 204",
    "update endpoint did not respond with a successful status code",
    "failed to check for updates",
    "failed to deserialize update response",
    "Searching for updater target",
    "cannot swap the app bundles",
    "app installation needs admin privileges",
    "failed to install the update",
    "failed to move the new app into place",
];
/// Its AppleScript error is the privileged shell's stderr: only this is kept.
pub const MOVE_FAILED: &str = "failed to move the new app into place";
/// tauri::process, at Error: a failed relaunch (the spawn's io error).
pub const RESTART_FAILED: &str = "failed to restart app: ";
/// tauri::process, at Error: `open -n` failed. Its exit status is kept, the
/// stderr `open` wrote after "): " is dropped; the io error form stays whole.
pub const LAUNCH_SERVICES_FAILED: &str = "failed to restart app with LaunchServices";
/// tauri::app, at Debug, exactly: which restart path Tauri took.
pub const RESTART_PATHS: [&str; 2] = [
    "restart triggered on the main thread",
    "restart triggered from a separate thread",
];
/// tauri::app, at Error: the restart's exit request failed.
pub const REQUEST_EXIT_FAILED: &str = "failed to request exit: ";

/// Where this build writes its log.
pub fn log_path(identifier: &str, home: &Path, support: &Path) -> PathBuf {
    if app_paths::is_test_build(identifier) {
        support.join("logs").join(identifier).join(FILE_NAME)
    } else {
        app_paths::release_log_dir(home).join(FILE_NAME)
    }
}

/// UTC RFC 3339 with milliseconds: `2026-11-17T08:15:30.123Z`.
pub fn timestamp(secs: i64, millis: u32) -> String {
    let time = secs as libc::time_t;
    // SAFETY: an all-zero tm is valid; gmtime_r only reads `time` and writes `tm`.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::gmtime_r(&time, &mut tm) }.is_null() {
        return format!("@{secs}.{millis:03}");
    }
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        tm.tm_year as i64 + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        millis.min(999)
    )
}

fn now_stamp() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    timestamp(now.as_secs() as i64, now.subsec_millis())
}

/// Every URL keeps its scheme, host and path. Its user info, query and fragment
/// are dropped, wherever it sits in the text (error messages included).
pub fn sanitize(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find("://") {
        // The scheme stays with the text before "://".
        out.push_str(&rest[..at]);
        out.push_str("://");
        let body = &rest[at + 3..];
        // A URL runs to whitespace, a quote or a bracket that can't be in one.
        // Cutting later is safe: the query and fragment go either way.
        let end = body
            .find(|c: char| c.is_whitespace() || c.is_control() || "\"'<>`".contains(c))
            .unwrap_or(body.len());
        let (url, after) = body.split_at(end);
        let authority = url.find(['/', '?', '#']).unwrap_or(url.len());
        let (userinfo_host, tail) = url.split_at(authority);
        out.push_str(
            userinfo_host
                .rsplit_once('@')
                .map_or(userinfo_host, |(_, h)| h),
        );
        match tail.find(['?', '#']) {
            Some(cut) => {
                out.push_str(&tail[..cut]);
                // Keep the punctuation that closed the surrounding sentence.
                let dropped = &tail[cut..];
                let keep = dropped.len()
                    - dropped
                        .trim_end_matches(|c: char| ")]}.,;:".contains(c))
                        .len();
                out.push_str(&dropped[dropped.len() - keep..]);
            }
            None => out.push_str(tail),
        }
        rest = after;
    }
    out.push_str(rest);
    out
}

/// Quoted text in a deserialize error can be a piece of the feed: drop it.
pub fn without_quoted(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut quoted = false;
    for c in text.chars() {
        if c == '"' {
            if !quoted {
                out.push_str("\"…");
            }
            quoted = !quoted;
            if !quoted {
                out.push('"');
            }
        } else if !quoted {
            out.push(c);
        }
    }
    out
}

fn cut(text: &str, max: usize) -> &str {
    if text.len() <= max {
        return text;
    }
    let mut end = max;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    &text[..end]
}

/// One line: timestamp, pid, the sanitized message with control characters
/// escaped, bounded to MAX_RECORD bytes with a marker.
pub fn record(stamp: &str, pid: u32, message: &str) -> String {
    let prefix = format!("{stamp} [{pid}] ");
    // Bound the work first; a cut URL still loses its query.
    let message = sanitize(cut(message, MAX_RECORD * 4));
    let mut line = String::with_capacity(prefix.len() + message.len() + 1);
    line.push_str(&prefix);
    for c in message.chars() {
        if c.is_control() {
            line.extend(c.escape_default());
        } else {
            line.push(c);
        }
    }
    if line.len() + 1 > MAX_RECORD {
        let keep = cut(&line, MAX_RECORD - 1 - TRUNCATED.len()).len();
        line.truncate(keep);
        line.push_str(TRUNCATED);
    }
    line.push('\n');
    line
}

/// The open log file and its rotation.
pub struct Sink {
    path: PathBuf,
    file: Option<File>,
    max: u64,
    failed: bool,
}

impl Sink {
    /// Makes the folder (0700) and opens the file (0600). `within` is a test
    /// build's physical app support folder: nothing below it may be a symlink.
    pub fn open(path: PathBuf, within: Option<&Path>) -> Result<Self, String> {
        Self::with_limit(path, within, MAX_BYTES)
    }
    pub fn with_limit(path: PathBuf, within: Option<&Path>, max: u64) -> Result<Self, String> {
        let dir = path.parent().ok_or("log path has no folder")?.to_path_buf();
        let check = |when: &str| -> Result<(), String> {
            if let Some(root) = within {
                let relative = path
                    .strip_prefix(root)
                    .map_err(|_| format!("{} is outside {}", path.display(), root.display()))?;
                app_paths::inside(root, relative).map_err(|e| format!("{when}: {e}"))?;
            }
            Ok(())
        };
        check("before creating the log folder")?;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)
            .map_err(|e| format!("{}: {e}", dir.display()))?;
        check("after creating the log folder")?;
        match fs::symlink_metadata(&dir) {
            Ok(meta) if meta.is_dir() => {}
            _ => return Err(format!("{} must be a folder, not a link", dir.display())),
        }
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("{}: {e}", dir.display()))?;
        let file = Self::open_file(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        Ok(Self {
            path,
            file: Some(file),
            max,
            failed: false,
        })
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    fn open_file(path: &Path) -> io::Result<File> {
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)?;
        // mode() applies only when the file is created.
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        Ok(file)
    }
    /// The open file, reopened when the path no longer names it: another
    /// instance rotated it, or someone removed it.
    fn current(&mut self) -> io::Result<&mut File> {
        let same = match (&self.file, fs::symlink_metadata(&self.path)) {
            (Some(file), Ok(disk)) => {
                let open = file.metadata()?;
                open.dev() == disk.dev() && open.ino() == disk.ino()
            }
            _ => false,
        };
        if !same {
            self.file = None;
            self.file = Some(Self::open_file(&self.path)?);
        }
        Ok(self.file.as_mut().expect("log file open"))
    }
    /// One whole record: a single write on an O_APPEND file, then flush.
    pub fn append(&mut self, record: &str) -> io::Result<()> {
        let result = self.append_inner(record.as_bytes());
        if result.is_err() {
            self.file = None;
        }
        result
    }
    fn append_inner(&mut self, record: &[u8]) -> io::Result<()> {
        let size = self.current()?.metadata()?.len();
        if size > 0 && size + record.len() as u64 > self.max {
            self.file = None;
            fs::rename(&self.path, self.path.with_file_name(ROTATED_NAME))?;
        }
        let file = self.current()?;
        file.write_all(record)?;
        file.flush()
    }
    fn write(&mut self, record: &str) {
        match self.append(record) {
            Ok(()) => self.failed = false,
            Err(error) => {
                // Once per failure streak, not once per record.
                if !std::mem::replace(&mut self.failed, true) {
                    eprintln!(
                        "agt-app diagnostic log {} unavailable: {error}",
                        self.path.display()
                    );
                }
            }
        }
    }
}

static SINK: OnceLock<Mutex<Sink>> = OnceLock::new();

/// Opens this launch's log, installs the log bridge and the panic hook.
/// Call once, after AppPaths (and a test build's prepare()).
pub fn init(paths: &AppPaths, identifier: &str) -> Option<PathBuf> {
    let path = log_path(identifier, &paths.home, &paths.support.path);
    let within = paths.test.then_some(paths.support.path.as_path());
    let opened = match Sink::open(path, within) {
        Ok(sink) => {
            let path = sink.path().to_path_buf();
            SINK.set(Mutex::new(sink)).ok().map(|()| path)
        }
        Err(error) => {
            eprintln!("agt-app diagnostic log unavailable: {error}");
            None
        }
    };
    install_bridge();
    install_panic_hook();
    opened
}

/// Appends one record when the log is open; a no-op before init().
pub fn write(message: &str) {
    let Some(sink) = SINK.get() else {
        return;
    };
    let line = record(&now_stamp(), std::process::id(), message);
    sink.lock().unwrap_or_else(|e| e.into_inner()).write(&line);
}

/// For the panic hook: never blocks on the lock, which the panicking thread
/// may hold. The previous hook still prints the panic to stderr.
fn try_write(message: &str) {
    let Some(sink) = SINK.get() else {
        return;
    };
    let line = record(&now_stamp(), std::process::id(), message);
    for _ in 0..20 {
        match sink.try_lock() {
            Ok(mut sink) => return sink.write(&line),
            Err(TryLockError::Poisoned(e)) => return e.into_inner().write(&line),
            Err(TryLockError::WouldBlock) => std::thread::sleep(Duration::from_millis(1)),
        }
    }
    eprintln!("agt-app diagnostic log busy; the panic below isn't in the file");
}

fn install_panic_hook() {
    static ONCE: OnceLock<()> = OnceLock::new();
    if ONCE.set(()).is_err() {
        return;
    }
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let payload = info
            .payload()
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| info.payload().downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "(no message)".into());
        let location = info
            .location()
            .map(|l| format!(" at {}:{}", l.file(), l.line()))
            .unwrap_or_default();
        let thread = std::thread::current();
        try_write(&format!(
            "panic thread={}{location}: {payload}",
            thread.name().unwrap_or("unnamed")
        ));
        previous(info);
    }));
}

/// The line a forwarded record becomes, labelled by its target, or None when
/// the record isn't one worth keeping.
pub fn bridged(target: &str, level: log::Level, message: &str) -> Option<String> {
    if target.starts_with(UPDATER_TARGET) {
        return updater(message).map(|m| format!("updater {level}: {m}"));
    }
    let label = match target {
        PROCESS_TARGET => "tauri.process",
        APP_TARGET => "tauri.app",
        _ => return None,
    };
    restart(target, level, message).map(|m| format!("{label} {level}: {m}"))
}

/// The updater plugin's own record, when it is one worth keeping.
fn updater(message: &str) -> Option<String> {
    if !BRIDGED.iter().any(|p| message.starts_with(p)) {
        return None;
    }
    Some(
        if message.starts_with("failed to deserialize update response") {
            without_quoted(message)
        } else if message.starts_with(MOVE_FAILED) {
            format!("{MOVE_FAILED} (AppleScript error not kept)")
        } else {
            message.to_string()
        },
    )
}

/// Tauri's restart records: these targets, levels and messages only.
fn restart(target: &str, level: log::Level, message: &str) -> Option<String> {
    use log::Level::{Debug, Error};
    match (target, level) {
        (PROCESS_TARGET, Error) if message.starts_with(RESTART_FAILED) => Some(message.into()),
        (PROCESS_TARGET, Error) if message.starts_with(LAUNCH_SERVICES_FAILED) => {
            Some(launch_services(&message[LAUNCH_SERVICES_FAILED.len()..]))
        }
        (APP_TARGET, Debug) if RESTART_PATHS.contains(&message) => Some(message.into()),
        (APP_TARGET, Error) if message.starts_with(REQUEST_EXIT_FAILED) => Some(message.into()),
        _ => None,
    }
}

/// `open -n` failed: keep its exit status, never the stderr it wrote.
fn launch_services(rest: &str) -> String {
    if rest.starts_with(": ") {
        // The io error of running /usr/bin/open: no child output.
        return format!("{LAUNCH_SERVICES_FAILED}{rest}");
    }
    // " ({status}): {stderr}". An exit status never contains "): ".
    match rest.strip_prefix(" (").and_then(|r| r.split_once("): ")) {
        Some((status, _)) => format!("{LAUNCH_SERVICES_FAILED} ({status}); stderr not kept"),
        None => format!("{LAUNCH_SERVICES_FAILED}; detail not kept"),
    }
}

struct Bridge;
impl log::Log for Bridge {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        let target = metadata.target();
        target.starts_with(UPDATER_TARGET) || target == PROCESS_TARGET || target == APP_TARGET
    }
    fn log(&self, record: &log::Record) {
        // The macros call log() without asking enabled(): filter before formatting.
        if !self.enabled(record.metadata()) {
            return;
        }
        if let Some(line) = bridged(record.target(), record.level(), &record.args().to_string()) {
            write(&line);
        }
    }
    fn flush(&self) {}
}

fn install_bridge() {
    static BRIDGE: Bridge = Bridge;
    // Another logger already installed keeps its place; the bridged lines then go there.
    if log::set_logger(&BRIDGE).is_ok() {
        log::set_max_level(log::LevelFilter::Debug);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::update::RELEASE_IDENTIFIER;

    struct Scratch(PathBuf);
    impl Scratch {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "agt-diag-{label}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn release_and_test_builds_log_in_their_own_folders() {
        let home = Path::new("/Users/someone");
        let support = Path::new("/tmp/scratch/support");
        assert_eq!(
            log_path(RELEASE_IDENTIFIER, home, support),
            home.join("Library/Logs/io.github.watcharagorn.agents-router/agents-router.log")
        );
        for id in [
            crate::app_paths::TEST_IDENTIFIER,
            "io.github.watcharagorn.agents-router.test.update-42",
        ] {
            let path = log_path(id, home, support);
            assert_eq!(
                path,
                support.join("logs").join(id).join("agents-router.log")
            );
            assert!(!path.starts_with(app_paths::release_log_dir(home)));
        }
        assert!(app_paths::production(home).contains(&app_paths::release_log_dir(home)));
    }

    #[test]
    fn timestamps_are_utc_rfc3339_with_milliseconds() {
        assert_eq!(timestamp(0, 0), "1970-01-01T00:00:00.000Z");
        assert_eq!(timestamp(1_795_427_730, 7), "2026-11-23T09:55:30.007Z");
        assert_eq!(timestamp(951_782_400, 999), "2000-02-29T00:00:00.999Z");
        let line = record(&timestamp(0, 5), 77, "hello");
        assert_eq!(line, "1970-01-01T00:00:00.005Z [77] hello\n");
    }

    #[test]
    fn urls_lose_user_info_query_and_fragment_anywhere_in_the_text() {
        for (input, expected) in [
            (
                "https://user:secret@example.com/a/b?token=abc#frag",
                "https://example.com/a/b",
            ),
            (
                "error sending request for url (https://x-access-token:ghs_1@github.com/o/r/latest.json?sig=s3cr3t): timed out",
                "error sending request for url (https://github.com/o/r/latest.json): timed out",
            ),
            (
                "checking for updates https://h.example/latest.json?current=1&k=v",
                "checking for updates https://h.example/latest.json",
            ),
            ("see http://h/p#section.", "see http://h/p."),
            ("two: https://a/x?q=1 and \"https://u@b/y#f\"", "two: https://a/x and \"https://b/y\""),
            ("query ) inside https://h/p?a=(x)&token=S", "query ) inside https://h/p"),
            ("p@ss in path https://h/p@x stays", "p@ss in path https://h/p@x stays"),
            ("no url here?a=b", "no url here?a=b"),
            ("https://example.com", "https://example.com"),
        ] {
            assert_eq!(sanitize(input), expected, "{input}");
            let line = record("T", 1, input);
            for secret in ["secret", "token=", "ghs_1", "s3cr3t", "frag", "k=v"] {
                assert!(!line.contains(secret), "{line}");
            }
        }
    }

    #[test]
    fn records_are_one_bounded_line() {
        let line = record("T", 1, "first\nsecond\r\u{1b}[31mred");
        assert_eq!(line, "T [1] first\\nsecond\\r\\u{1b}[31mred\n");
        let long = "é".repeat(MAX_RECORD * 10);
        let line = record("2026-11-17T00:00:00.000Z", 123, &long);
        assert!(line.len() <= MAX_RECORD, "{}", line.len());
        assert!(line.ends_with(&format!("{TRUNCATED}\n")));
        assert_eq!(line.matches('\n').count(), 1);
        // A cut through a URL's query still drops the query.
        let url = format!("https://h/p?token={}", "x".repeat(MAX_RECORD * 5));
        assert!(!record("T", 1, &url).contains("token"));
    }

    #[test]
    fn rotation_keeps_one_previous_file_and_caps_the_size() {
        let scratch = Scratch::new("rotate");
        let path = scratch.0.join("logs/agents-router.log");
        let mut sink = Sink::with_limit(path.clone(), None, 1000).unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mode = |p: &Path| fs::metadata(p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(path.parent().unwrap()), 0o700);
        assert_eq!(mode(&path), 0o600);
        for i in 0..200 {
            sink.append(&record("T", 1, &format!("line {i:04} {}", "x".repeat(40))))
                .unwrap();
        }
        let names: Vec<_> = fs::read_dir(path.parent().unwrap())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(names.len(), 2, "{names:?}");
        let rotated = path.with_file_name(ROTATED_NAME);
        assert!(fs::metadata(&path).unwrap().len() <= 1000);
        assert!(fs::metadata(&rotated).unwrap().len() <= 1000);
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.ends_with(&format!("line 0199 {}\n", "x".repeat(40))));
        assert!(fs::read_to_string(&rotated)
            .unwrap()
            .lines()
            .all(|l| l.starts_with("T [1] line ")));
        // Removed while open (rm, or another instance rotating): the next record makes it again.
        fs::remove_file(&path).unwrap();
        sink.append("again\n").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "again\n");
        assert_eq!(mode(&path), 0o600);
    }

    #[test]
    fn a_symlinked_log_file_or_folder_is_refused() {
        let scratch = Scratch::new("links");
        let elsewhere = scratch.0.join("elsewhere");
        fs::create_dir_all(&elsewhere).unwrap();
        // The log file itself: O_NOFOLLOW.
        let dir = scratch.0.join("release");
        fs::create_dir_all(&dir).unwrap();
        std::os::unix::fs::symlink(elsewhere.join("file"), dir.join(FILE_NAME)).unwrap();
        assert!(Sink::open(dir.join(FILE_NAME), None).is_err());
        assert!(!elsewhere.join("file").exists());
        // A test build's logs folder below app support.
        let support = scratch.0.join("support");
        fs::create_dir_all(&support).unwrap();
        std::os::unix::fs::symlink(&elsewhere, support.join("logs")).unwrap();
        let path = log_path(crate::app_paths::TEST_IDENTIFIER, &scratch.0, &support);
        let error = Sink::open(path, Some(&support)).err().unwrap();
        assert!(error.contains("is a symlink"), "{error}");
        assert_eq!(fs::read_dir(&elsewhere).unwrap().count(), 0);
    }

    #[test]
    fn the_bridge_keeps_only_allowed_updater_records() {
        use log::Level::{Debug, Error};
        const UPDATER: &str = "tauri_plugin_updater::updater";
        for message in BRIDGED.iter().filter(|m| **m != MOVE_FAILED) {
            let line = format!("{message}: detail");
            assert_eq!(
                bridged(UPDATER, Debug, &line),
                Some(format!("updater DEBUG: {line}"))
            );
        }
        assert_eq!(
            bridged(UPDATER, Debug, "checking for updates https://h/l.json?x=1")
                .map(|m| sanitize(&m)),
            Some("updater DEBUG: checking for updates https://h/l.json".into())
        );
        for (target, message) in [
            (UPDATER, "update response: Object {\"signature\": \"abc\"}"),
            (UPDATER, "parsed release response RemoteRelease { .. }"),
            (UPDATER, "using proxy http://user:pw@proxy:3128"),
            (UPDATER, "disabling proxy"),
            (UPDATER, "Executing updater \"/tmp/x\" with parameters: []"),
            (UPDATER, "running on_before_exit hook"),
            ("reqwest::connect", "checking for updates"),
            ("hyper", "failed to check for updates"),
        ] {
            assert_eq!(bridged(target, Error, message), None, "{target}: {message}");
        }
        assert_eq!(
            bridged(
                UPDATER,
                Error,
                "failed to deserialize update response: invalid type: string \"dW50cnVzdGVk\", expected a map"
            ),
            Some("updater ERROR: failed to deserialize update response: invalid type: string \"…\", expected a map".into())
        );
        // The AppleScript error is the privileged shell's stderr.
        assert_eq!(
            bridged(
                UPDATER,
                Error,
                "failed to move the new app into place: mv: /Applications/secret-name.app: Permission denied (1)"
            ),
            Some("updater ERROR: failed to move the new app into place (AppleScript error not kept)".into())
        );
    }

    #[test]
    fn the_bridge_keeps_tauris_restart_records_without_child_output() {
        use log::Level::{Debug, Error, Info, Warn};
        for (target, level, message, expected) in [
            (
                "tauri::process",
                Error,
                "failed to restart app: No such file or directory (os error 2)",
                "tauri.process ERROR: failed to restart app: No such file or directory (os error 2)",
            ),
            (
                "tauri::process",
                Error,
                "failed to restart app with LaunchServices: Permission denied (os error 13)",
                "tauri.process ERROR: failed to restart app with LaunchServices: Permission denied (os error 13)",
            ),
            (
                "tauri::process",
                Error,
                "failed to restart app with LaunchServices (exit status: 1): The application /Applications/x.app cannot be opened: token=s3cr3t",
                "tauri.process ERROR: failed to restart app with LaunchServices (exit status: 1); stderr not kept",
            ),
            (
                "tauri::process",
                Error,
                "failed to restart app with LaunchServices (signal: 9 (SIGKILL)): killed): again",
                "tauri.process ERROR: failed to restart app with LaunchServices (signal: 9 (SIGKILL)); stderr not kept",
            ),
            (
                "tauri::process",
                Error,
                "failed to restart app with LaunchServices (exit status: 1): ",
                "tauri.process ERROR: failed to restart app with LaunchServices (exit status: 1); stderr not kept",
            ),
            (
                "tauri::process",
                Error,
                "failed to restart app with LaunchServices, someday: s3cr3t",
                "tauri.process ERROR: failed to restart app with LaunchServices; detail not kept",
            ),
            (
                "tauri::app",
                Debug,
                "restart triggered on the main thread",
                "tauri.app DEBUG: restart triggered on the main thread",
            ),
            (
                "tauri::app",
                Debug,
                "restart triggered from a separate thread",
                "tauri.app DEBUG: restart triggered from a separate thread",
            ),
            (
                "tauri::app",
                Error,
                "failed to request exit: event loop closed",
                "tauri.app ERROR: failed to request exit: event loop closed",
            ),
        ] {
            let line = bridged(target, level, message);
            assert_eq!(line.as_deref(), Some(expected), "{target}: {message}");
            assert!(!line.unwrap().contains("s3cr3t"));
        }
        for (target, level, message) in [
            // Other Tauri targets, including a prefix or child of an allowed one.
            ("tauri", Error, "failed to restart app: x"),
            ("tauri::process::inner", Error, "failed to restart app: x"),
            ("tauri::application", Error, "failed to request exit: x"),
            (
                "tauri::manager",
                Debug,
                "restart triggered on the main thread",
            ),
            ("tauri_runtime_wry", Error, "failed to restart app: x"),
            // Other messages from the allowed targets.
            ("tauri::app", Error, "failed to exit: event loop closed"),
            ("tauri::app", Error, "failed to finish the activity: x"),
            (
                "tauri::app",
                Debug,
                "restart triggered on the main thread, twice",
            ),
            ("tauri::app", Debug, "failed to request exit: x"),
            ("tauri::process", Error, "failed to relaunch: x"),
            // The right message at another level.
            ("tauri::process", Warn, "failed to restart app: x"),
            (
                "tauri::process",
                Info,
                "failed to restart app with LaunchServices: x",
            ),
            (
                "tauri::app",
                Info,
                "restart triggered from a separate thread",
            ),
            ("tauri::app", Debug, "failed to request exit: x"),
        ] {
            assert_eq!(
                bridged(target, level, message),
                None,
                "{target} {level}: {message}"
            );
        }
        let bridge = Bridge;
        for (target, enabled) in [
            ("tauri_plugin_updater::updater", true),
            ("tauri::process", true),
            ("tauri::app", true),
            ("tauri", false),
            ("tauri::manager", false),
            ("tauri::process::inner", false),
        ] {
            let metadata = log::Metadata::builder().target(target).level(Debug).build();
            assert_eq!(log::Log::enabled(&bridge, &metadata), enabled, "{target}");
        }
    }
}
