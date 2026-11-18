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
    ffi::{CStr, CString},
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        },
    },
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock, TryLockError},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub const FILE_NAME: &str = "agents-router.log";
pub const ROTATED_NAME: &str = "agents-router.log.1";
/// Never rotated or removed: launches flock it around each record.
pub const LOCK_NAME: &str = "agents-router.log.lock";
/// How often a record tries the lock, 1 ms apart, before it is written
/// without rotating.
const LOCK_TRIES: u32 = 50;
/// The log file's open flags, with open_at's own.
const APPEND: libc::c_int = libc::O_WRONLY | libc::O_APPEND;
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
/// Its serde_json text quotes the feed: only its position is kept.
pub const DESERIALIZE_FAILED: &str = "failed to deserialize update response";
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
        // A URL runs to whitespace, a quote or a bracket that can't be in one,
        // or, before its query, to the scheme of the next URL: two joined by
        // a comma, or by nothing, are two URLs, each sanitized on its own.
        // Commas and parentheses stay inside: they are legal in a path or
        // query. A URL inside the query goes with it: cutting later is safe.
        let mut end = body
            .find(|c: char| c.is_whitespace() || c.is_control() || "\"'<>`".contains(c))
            .unwrap_or(body.len());
        let kept = body[..end].find(['?', '#']).unwrap_or(end);
        if let Some(next) = body[..kept].find("://") {
            end = scheme_start(&body[..next]);
        }
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

/// Where the scheme that ends `text` (just before a "://") starts.
fn scheme_start(text: &str) -> usize {
    text.trim_end_matches(|c: char| c.is_ascii_alphanumeric() || "+-.".contains(c))
        .len()
}

/// A list of URLs for one log line: each sanitized on its own, separated by a
/// space, which no URL contains.
pub fn url_list(urls: &[&str]) -> String {
    if urls.is_empty() {
        return "none".into();
    }
    urls.iter()
        .map(|url| sanitize(url))
        .collect::<Vec<_>>()
        .join(" ")
}

/// A JSON error in the update feed: its category and position, never its
/// message, which quotes the feed's own values ("…", `…`) in any form.
pub fn json_error(error: &serde_json::Error) -> String {
    use serde_json::error::Category;
    let category = match error.classify() {
        Category::Io => "io",
        Category::Syntax => "syntax",
        Category::Data => "data",
        Category::Eof => "eof",
    };
    match error.line() {
        // serde_json::from_value, as the updater uses it, has no position.
        0 => format!("JSON {category} error; detail not kept"),
        line => format!(
            "JSON {category} error at line {line} column {}; detail not kept",
            error.column()
        ),
    }
}

/// The updater's own deserialize record has only serde_json's text: keep its
/// trailing " at line N column M" when it has one, nothing else of it.
fn deserialize_failed(message: &str) -> String {
    let digits = |n: &str| !n.is_empty() && n.len() <= 20 && n.bytes().all(|b| b.is_ascii_digit());
    let position = message
        .rsplit_once(" at line ")
        .and_then(|(_, tail)| tail.split_once(" column "))
        .filter(|(line, column)| digits(line) && digits(column))
        .map(|(line, column)| format!(" at line {line} column {column}"))
        .unwrap_or_default();
    format!("{DESERIALIZE_FAILED}; detail not kept{position}")
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

/// The open log file and its rotation. The folder is opened once, after the
/// checks: every later open, stat and rename is relative to that descriptor,
/// so a folder swapped for a link afterwards (into the release's log folder,
/// say) can't redirect a write.
pub struct Sink {
    path: PathBuf,
    name: CString,
    dir: File,
    file: Option<File>,
    /// LOCK_NAME, held around each size check, rotation and append: two
    /// launches (logging starts before single-instance) rotate one at a time.
    lock: Option<File>,
    max: u64,
    failed: bool,
}

/// The cross-launch lock, released when dropped, an early `?` included.
struct Held(libc::c_int);
impl Drop for Held {
    fn drop(&mut self) {
        // SAFETY: the Sink keeps this lock file open for the guard's whole life.
        unsafe { libc::flock(self.0, libc::LOCK_UN) };
    }
}

fn c_name(name: &str) -> io::Result<CString> {
    Ok(CString::new(name)?)
}

/// Opens or creates `name` in the folder: never through a link, never
/// waiting, a regular file only, 0600. Without O_NONBLOCK, a FIFO at the name
/// would hold an open for writing until a reader came.
fn open_at(dir: &File, name: &CStr, flags: libc::c_int) -> io::Result<File> {
    let flags = flags | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK;
    // SAFETY: a valid folder descriptor and a NUL-terminated name; the mode is
    // passed as c_uint, as the variadic call promotes it.
    let fd = unsafe { libc::openat(dir.as_raw_fd(), name.as_ptr(), flags, 0o600 as libc::c_uint) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: openat just returned this descriptor; nothing else owns it.
    let file = unsafe { File::from_raw_fd(fd) };
    // A FIFO, socket or device at the name: closed unwritten, its mode kept.
    if !file.metadata()?.file_type().is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "not a regular file",
        ));
    }
    // The mode applies only when the file is created.
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    Ok(file)
}

/// Whether `name` in the folder (not followed) is the file open as `file`.
fn names(dir: &File, name: &CStr, file: &File) -> io::Result<bool> {
    // SAFETY: an all-zero stat is valid; fstatat only writes it.
    let mut disk: libc::stat = unsafe { std::mem::zeroed() };
    let fd = dir.as_raw_fd();
    let found = unsafe { libc::fstatat(fd, name.as_ptr(), &mut disk, libc::AT_SYMLINK_NOFOLLOW) };
    if found < 0 {
        return Ok(false);
    }
    let open = file.metadata()?;
    Ok(open.dev() == disk.st_dev as u64 && open.ino() == disk.st_ino as u64)
}

fn rename_at(dir: &File, from: &CStr, to: &CStr) -> io::Result<()> {
    let fd = dir.as_raw_fd();
    // SAFETY: a valid folder descriptor and NUL-terminated names.
    if unsafe { libc::renameat(fd, from.as_ptr(), fd, to.as_ptr()) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

impl Sink {
    /// Makes the folder (0700) and opens the file (0600). `within` is a test
    /// build's physical app support folder: nothing below it may be a symlink.
    pub fn open(path: PathBuf, within: Option<&Path>) -> Result<Self, String> {
        Self::with_limit(path, within, MAX_BYTES)
    }
    pub fn with_limit(path: PathBuf, within: Option<&Path>, max: u64) -> Result<Self, String> {
        let folder = path.parent().ok_or("log path has no folder")?.to_path_buf();
        let name = path
            .file_name()
            .ok_or("log path has no file name")
            .and_then(|n| CString::new(n.as_bytes()).map_err(|_| "log file name has a NUL"))?;
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
            .create(&folder)
            .map_err(|e| format!("{}: {e}", folder.display()))?;
        check("after creating the log folder")?;
        let not_a_folder = || format!("{} must be a folder, not a link", folder.display());
        let dir = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&folder)
            .map_err(|e| format!("{}: {e}", not_a_folder()))?;
        // The folder held is the one checked: still no link below the root,
        // and the path still names this folder.
        check("after opening the log folder")?;
        let opened = dir
            .metadata()
            .map_err(|e| format!("{}: {e}", folder.display()))?;
        match fs::symlink_metadata(&folder) {
            Ok(meta)
                if meta.is_dir() && meta.dev() == opened.dev() && meta.ino() == opened.ino() => {}
            _ => return Err(not_a_folder()),
        }
        dir.set_permissions(fs::Permissions::from_mode(0o700))
            .map_err(|e| format!("{}: {e}", folder.display()))?;
        let file = open_at(&dir, &name, APPEND).map_err(|e| format!("{}: {e}", path.display()))?;
        let lock = c_name(LOCK_NAME)
            .and_then(|lock| open_at(&dir, &lock, libc::O_RDWR))
            .map_err(|e| format!("{}: {e}", folder.join(LOCK_NAME).display()))?;
        Ok(Self {
            path,
            name,
            dir,
            file: Some(file),
            lock: Some(lock),
            max,
            failed: false,
        })
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    /// The open file, reopened when its name in the folder no longer names
    /// it: another instance rotated it, or someone removed it.
    fn current(&mut self) -> io::Result<&mut File> {
        let same = match &self.file {
            Some(file) => names(&self.dir, &self.name, file)?,
            None => false,
        };
        if !same {
            self.file = None;
            self.file = Some(open_at(&self.dir, &self.name, APPEND)?);
        }
        Ok(self.file.as_mut().expect("log file open"))
    }
    /// LOCK_EX on the lock file, tried LOCK_TRIES times 1 ms apart: None when
    /// another launch keeps it longer, or the lock file can't be opened.
    fn lock(&mut self) -> Option<Held> {
        let lock_name = c_name(LOCK_NAME).ok()?;
        for _ in 0..LOCK_TRIES {
            if self.lock.is_none() {
                self.lock = Some(open_at(&self.dir, &lock_name, libc::O_RDWR).ok()?);
            }
            let lock = self.lock.as_ref()?;
            // SAFETY: a descriptor the Sink owns and keeps open.
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                let held = Held(lock.as_raw_fd());
                // Removed or replaced while open: another launch would lock the
                // file now at that name, so lock that one.
                if names(&self.dir, &lock_name, lock).unwrap_or(false) {
                    return Some(held);
                }
                drop(held);
                self.lock = None;
                continue;
            }
            match io::Error::last_os_error().kind() {
                io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(1)),
                io::ErrorKind::Interrupted => {}
                _ => return None,
            }
        }
        None
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
        // Without the lock: never wait longer, never rotate a file another
        // launch may be rotating, and write only while the record fits.
        let held = self.lock();
        // Under the lock: another launch may have rotated since our last write.
        let size = self.current()?.metadata()?.len();
        if size > 0 && size + record.len() as u64 > self.max {
            if held.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "full while another launch holds the rotation lock; record dropped",
                ));
            }
            self.file = None;
            rename_at(&self.dir, &self.name, &c_name(ROTATED_NAME)?)?;
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
    Some(if message.starts_with(DESERIALIZE_FAILED) {
        deserialize_failed(message)
    } else if message.starts_with(MOVE_FAILED) {
        format!("{MOVE_FAILED} (AppleScript error not kept)")
    } else {
        message.to_string()
    })
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
            // Two URLs joined by a comma, or by nothing: each sanitized alone.
            (
                "https://a/feed,https://user:secret@b/feed",
                "https://a/feed,https://b/feed",
            ),
            ("https://a,https://user:secret@b/f", "https://a,https://b/f"),
            (
                "https://a/feedhttps://user:secret@b/feed",
                "https://a/feedhttps://b/feed",
            ),
            ("https://ahttps://user:secret@b", "https://ahttps://b"),
            (
                "https://a/x?q=1,https://user:secret@b/y#frag",
                "https://a/x",
            ),
            // A URL inside a query or fragment goes with it.
            ("https://h/cb?next=https://x/secret", "https://h/cb"),
            ("https://h/p#https://u:secret@x/k=v", "https://h/p"),
            // A comma inside one URL keeps its query out all the same.
            ("https://h/a,b?token=S#frag", "https://h/a,b"),
            ("https://h/p?list=a,b&token=S", "https://h/p"),
        ] {
            assert_eq!(sanitize(input), expected, "{input}");
            let line = record("T", 1, input);
            for secret in ["secret", "token=", "ghs_1", "s3cr3t", "frag", "k=v"] {
                assert!(!line.contains(secret), "{line}");
            }
        }
    }

    #[test]
    fn an_endpoint_list_sanitizes_each_url_and_separates_them_with_spaces() {
        assert_eq!(url_list(&[]), "none");
        let list = url_list(&[
            "https://a/feed?k=v",
            "https://user:secret@b/feed",
            "https://c/x,https://user:secret@d/y",
        ]);
        assert_eq!(
            list,
            "https://a/feed https://b/feed https://c/x,https://d/y"
        );
        // The line it goes into is sanitized again: nothing changes.
        assert_eq!(sanitize(&list), list);
        assert!(!record("T", 1, &format!("endpoints={list}")).contains("secret"));
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
        let mut names: Vec<_> = fs::read_dir(path.parent().unwrap())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        assert_eq!(names, [FILE_NAME, ROTATED_NAME, LOCK_NAME]);
        assert_eq!(mode(&path.with_file_name(LOCK_NAME)), 0o600);
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
    fn a_log_folder_swapped_for_a_link_never_redirects_writes() {
        let scratch = Scratch::new("swap");
        let support = scratch.0.join("support");
        let elsewhere = scratch.0.join("release-logs");
        fs::create_dir_all(&support).unwrap();
        fs::create_dir_all(&elsewhere).unwrap();
        let path = log_path(crate::app_paths::TEST_IDENTIFIER, &scratch.0, &support);
        let mut sink = Sink::with_limit(path.clone(), Some(&support), 200).unwrap();
        sink.append("first\n").unwrap();
        // After init, the checked folder is moved away and a link to another
        // log folder (the release's, say) takes its name.
        let folder = path.parent().unwrap();
        let moved = folder.with_file_name("moved");
        fs::rename(folder, &moved).unwrap();
        std::os::unix::fs::symlink(&elsewhere, folder).unwrap();
        // Appends, a rotation and a removed file: all stay in the folder opened.
        for i in 0..10 {
            sink.append(&format!("record {i} {}\n", "x".repeat(40)))
                .unwrap();
        }
        assert!(moved.join(ROTATED_NAME).is_file());
        fs::remove_file(moved.join(FILE_NAME)).unwrap();
        sink.append("after removal\n").unwrap();
        assert_eq!(fs::read_dir(&elsewhere).unwrap().count(), 0);
        assert_eq!(
            fs::read_to_string(moved.join(FILE_NAME)).unwrap(),
            "after removal\n"
        );
    }

    #[test]
    fn another_launchs_lock_defers_rotation_and_never_blocks() {
        use std::os::fd::AsRawFd;
        let scratch = Scratch::new("lock");
        let path = scratch.0.join("logs").join(FILE_NAME);
        let rotated = path.with_file_name(ROTATED_NAME);
        let mut sink = Sink::with_limit(path.clone(), None, 100).unwrap();
        let full = format!("{}\n", "a".repeat(89));
        sink.append(&full).unwrap();
        // Another launch's lock: flock locks belong to the open file, so this
        // one conflicts with the sink's in the same process too. Taking it at
        // once also shows the sink let go of its own after the append.
        let other = File::open(path.with_file_name(LOCK_NAME)).unwrap();
        let flock = |op| unsafe { libc::flock(other.as_raw_fd(), op) };
        assert_eq!(flock(libc::LOCK_EX | libc::LOCK_NB), 0);
        // While locked: a record that fits is written; one past the limit is
        // dropped with an error, neither written nor rotated.
        sink.append("fits\n").unwrap();
        assert!(sink.append("while locked\n").is_err());
        assert!(!rotated.exists());
        assert_eq!(fs::read_to_string(&path).unwrap(), format!("{full}fits\n"));
        // Released: the next record past the limit rotates.
        assert_eq!(flock(libc::LOCK_UN), 0);
        sink.append("after release\n").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "after release\n");
        assert_eq!(
            fs::read_to_string(&rotated).unwrap(),
            format!("{full}fits\n")
        );
    }

    #[test]
    fn a_busy_lock_never_takes_the_file_past_the_cap() {
        use std::os::fd::AsRawFd;
        let scratch = Scratch::new("cap");
        let path = scratch.0.join("logs").join(FILE_NAME);
        let rotated = path.with_file_name(ROTATED_NAME);
        let mut sink = Sink::open(path.clone(), None).unwrap();
        let line = format!("{}\n", "r".repeat(99));
        let size = || fs::metadata(&path).unwrap().len();
        // Near the cap: room for exactly two more records.
        OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(MAX_BYTES - 2 * line.len() as u64)
            .unwrap();
        // Another launch keeps the lock for every record below.
        let other = File::open(path.with_file_name(LOCK_NAME)).unwrap();
        let flock = |op| unsafe { libc::flock(other.as_raw_fd(), op) };
        assert_eq!(flock(libc::LOCK_EX | libc::LOCK_NB), 0);
        for i in 0..6 {
            assert_eq!(sink.append(&line).is_ok(), i < 2, "record {i}");
            assert!(size() <= MAX_BYTES, "record {i}: {} bytes", size());
        }
        assert_eq!(size(), MAX_BYTES);
        assert!(!rotated.exists());
        // Released: the full file is rotated, at the cap, not past it.
        assert_eq!(flock(libc::LOCK_UN), 0);
        sink.append(&line).unwrap();
        assert_eq!(size(), line.len() as u64);
        assert_eq!(fs::metadata(&rotated).unwrap().len(), MAX_BYTES);
    }

    /// A FIFO at `path`, mode 0644 whatever the umask.
    fn fifo(path: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let name = CString::new(path.as_os_str().as_bytes()).unwrap();
        // SAFETY: a NUL-terminated path.
        let made = unsafe { libc::mkfifo(name.as_ptr(), 0o644) };
        assert_eq!(made, 0, "{}", io::Error::last_os_error());
        fs::set_permissions(path, fs::Permissions::from_mode(0o644)).unwrap();
    }

    /// The FIFO's read end, opened without waiting for a writer.
    fn reader(path: &Path) -> File {
        OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .unwrap()
    }

    /// Runs `work` on its own thread, so an open stalled on a FIFO fails the
    /// test instead of hanging it. On a stall, a reader lets the open go on.
    fn promptly<T: Send + 'static>(fifo: &Path, work: impl FnOnce() -> T + Send + 'static) -> T {
        let (send, receive) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = send.send(work());
        });
        match receive.recv_timeout(Duration::from_secs(30)) {
            Ok(result) => result,
            Err(_) => {
                let _reader = reader(fifo);
                let _ = receive.recv_timeout(Duration::from_secs(30));
                panic!("stalled on the FIFO at {}", fifo.display());
            }
        }
    }

    fn mode(path: &Path) -> u32 {
        use std::os::unix::fs::PermissionsExt;
        fs::symlink_metadata(path).unwrap().permissions().mode() & 0o777
    }

    /// Asserts nothing was written into the FIFO this read end belongs to.
    fn unread(mut reader: File) {
        use std::io::Read;
        let mut got = Vec::new();
        match reader.read_to_end(&mut got) {
            Ok(_) => assert!(got.is_empty(), "{got:?}"),
            Err(e) => assert_eq!(e.kind(), io::ErrorKind::WouldBlock, "{e}"),
        }
    }

    /// A log path in its own new folder of the scratch.
    fn log_in(scratch: &Scratch, folder: &str) -> PathBuf {
        let path = scratch.0.join(folder).join(FILE_NAME);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        path
    }

    #[test]
    fn a_fifo_at_the_log_name_never_stalls_the_start() {
        // No reader: an open for writing would wait for one.
        let scratch = Scratch::new("fifo-alone");
        let path = log_in(&scratch, "logs");
        fifo(&path);
        let opening = path.clone();
        assert!(promptly(&path, move || Sink::open(opening, None).is_err()));
        assert_eq!(mode(&path), 0o644);
    }

    #[test]
    fn a_fifo_at_the_log_name_with_a_reader_is_refused_unchanged() {
        // The open succeeds, so only the file type stops it, before its mode
        // changes or anything is written.
        let scratch = Scratch::new("fifo-read");
        let path = log_in(&scratch, "logs");
        fifo(&path);
        let read_end = reader(&path);
        let error = Sink::open(path.clone(), None).err().unwrap();
        assert!(error.contains("not a regular file"), "{error}");
        assert_eq!(mode(&path), 0o644);
        unread(read_end);
    }

    #[test]
    fn a_fifo_at_the_lock_name_is_refused_unchanged() {
        // O_RDWR opens a FIFO at once: only its type stops it, before its
        // mode changes.
        let scratch = Scratch::new("fifo-lock");
        let path = log_in(&scratch, "logs");
        let lock = path.with_file_name(LOCK_NAME);
        fifo(&lock);
        let error = Sink::open(path, None).err().unwrap();
        assert!(error.contains("not a regular file"), "{error}");
        assert_eq!(mode(&lock), 0o644);
    }

    #[test]
    fn a_fifo_met_on_reopen_under_the_lock_never_stalls_or_is_written() {
        use std::os::fd::AsRawFd;
        let scratch = Scratch::new("fifo-reopen");
        let path = log_in(&scratch, "logs");
        let mut sink = Sink::open(path.clone(), None).unwrap();
        sink.append("first\n").unwrap();
        fs::remove_file(&path).unwrap();
        fifo(&path);
        // No reader: refused at once, under the rotation lock.
        let (mut sink, appended) = promptly(&path, move || {
            let appended = sink.append("second\n");
            (sink, appended)
        });
        assert!(appended.is_err());
        // A reader: refused all the same, nothing written, mode kept.
        let read_end = reader(&path);
        assert!(sink.append("third\n").is_err());
        assert_eq!(mode(&path), 0o644);
        unread(read_end);
        // The lock was let go.
        let other = File::open(path.with_file_name(LOCK_NAME)).unwrap();
        // SAFETY: a descriptor this test owns.
        let locked = unsafe { libc::flock(other.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        assert_eq!(locked, 0);
    }

    #[test]
    fn a_launch_writes_into_the_file_another_launch_rotated_in() {
        let scratch = Scratch::new("two");
        let path = scratch.0.join("logs").join(FILE_NAME);
        let mut first = Sink::with_limit(path.clone(), None, 100).unwrap();
        let mut second = Sink::with_limit(path.clone(), None, 100).unwrap();
        let one = format!("{}\n", "1".repeat(80));
        let two = format!("{}\n", "2".repeat(80));
        first.append(&one).unwrap();
        second.append(&two).unwrap();
        // The first still holds the rotated file open: it reopens the new one
        // and doesn't rotate again over the kept `.1`.
        first.append("first again\n").unwrap();
        assert_eq!(
            fs::read_to_string(path.with_file_name(ROTATED_NAME)).unwrap(),
            one
        );
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            format!("{two}first again\n")
        );
    }

    #[test]
    fn the_bridge_keeps_only_allowed_updater_records() {
        use log::Level::{Debug, Error};
        const UPDATER: &str = "tauri_plugin_updater::updater";
        for message in BRIDGED
            .iter()
            .filter(|m| ![MOVE_FAILED, DESERIALIZE_FAILED].contains(*m))
        {
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
            Some("updater ERROR: failed to deserialize update response; detail not kept".into())
        );
        // serde_json quotes feed values with escaped quotes and backticks too.
        for (message, expected) in [
            (
                r#"failed to deserialize update response: invalid value: string "before \"TOKEN=SECRET\" after", expected a version"#,
                "updater ERROR: failed to deserialize update response; detail not kept",
            ),
            (
                "failed to deserialize update response: unknown variant `SECRET`, expected one of `a`, `b`",
                "updater ERROR: failed to deserialize update response; detail not kept",
            ),
            (
                r#"failed to deserialize update response: invalid type: string "SECRET", expected a map at line 3 column 14"#,
                "updater ERROR: failed to deserialize update response; detail not kept at line 3 column 14",
            ),
            (
                r#"failed to deserialize update response: invalid type: string "SECRET at line 1 column 2x", expected a map"#,
                "updater ERROR: failed to deserialize update response; detail not kept",
            ),
        ] {
            let line = bridged(UPDATER, Error, message);
            assert_eq!(line.as_deref(), Some(expected), "{message}");
        }
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
    fn a_feed_json_error_keeps_its_category_and_position_only() {
        #[derive(serde::Deserialize, Debug)]
        #[allow(dead_code)]
        struct Release {
            version: u32,
        }
        // serde_json::from_value, as the updater calls it: no position.
        let value = serde_json::json!({"version": "before \"TOKEN=SECRET\" after"});
        let error = serde_json::from_value::<Release>(value).unwrap_err();
        assert!(error.to_string().contains("SECRET"), "{error}");
        assert_eq!(json_error(&error), "JSON data error; detail not kept");
        let value = serde_json::json!({"version": 1, "SECRET": true});
        #[derive(serde::Deserialize, Debug)]
        #[serde(deny_unknown_fields)]
        #[allow(dead_code)]
        struct Strict {
            version: u32,
        }
        let error = serde_json::from_value::<Strict>(value).unwrap_err();
        assert!(error.to_string().contains("`SECRET`"), "{error}");
        assert_eq!(json_error(&error), "JSON data error; detail not kept");
        // Parsed from text: the position stays.
        let error = serde_json::from_str::<Release>("{\n  \"version\": \"SECRET\"}").unwrap_err();
        assert_eq!(
            json_error(&error),
            "JSON data error at line 2 column 21; detail not kept"
        );
        let error = serde_json::from_str::<Release>("{\"version\": SECRET").unwrap_err();
        assert_eq!(
            json_error(&error),
            "JSON syntax error at line 1 column 13; detail not kept"
        );
        let error = serde_json::from_str::<Release>("{\"version\": 1").unwrap_err();
        assert_eq!(
            json_error(&error),
            "JSON eof error at line 1 column 13; detail not kept"
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
