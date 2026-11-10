//! Runtime transactions. No PATH-selected executable, shell IPC, or package manager.
use crate::terminal::posix_quote;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    cmp::Ordering,
    fs::{self, File, OpenOptions},
    io,
    os::unix::fs::{symlink, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{process::Command, time::timeout};

type Result<T> = std::result::Result<T, String>;
fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn nonce() -> String {
    format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    )
}
fn present(p: &Path) -> bool {
    fs::symlink_metadata(p).is_ok()
}
fn absolute(name: &str, fallback: PathBuf) -> Result<PathBuf> {
    let p = std::env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or(fallback);
    if !p.is_absolute() {
        return Err(format!("{name} must be absolute"));
    }
    let mut ancestor = p.as_path();
    let mut suffix = Vec::new();
    while !ancestor.exists() {
        suffix.push(ancestor.file_name().ok_or("Invalid path")?.to_owned());
        ancestor = ancestor.parent().ok_or("Invalid path")?;
    }
    let mut physical = ancestor.canonicalize().map_err(err)?;
    for component in suffix.into_iter().rev() {
        physical.push(component);
    }
    Ok(physical)
}
#[derive(Clone, Debug)]
pub struct InstallPaths {
    pub support: PathBuf,
    pub bin: PathBuf,
    pub agt_home: PathBuf,
    pub home: PathBuf,
    pub applications: Vec<PathBuf>,
}
impl InstallPaths {
    pub fn discover() -> Result<Self> {
        let home = absolute("HOME", PathBuf::new())?;
        let applications = match std::env::var_os("AGT_APPLICATIONS_DIRS") {
            Some(paths) => std::env::split_paths(&paths).collect(),
            None => vec![PathBuf::from("/Applications"), home.join("Applications")],
        };
        if applications.is_empty() || applications.iter().any(|p| !p.is_absolute()) {
            return Err("Applications destinations must be absolute".into());
        }
        Ok(Self {
            support: absolute(
                "AGT_APP_SUPPORT",
                home.join("Library/Application Support/Agents Router"),
            )?,
            bin: absolute("AGT_BIN_DIR", home.join(".local/bin"))?,
            agt_home: absolute("AGT_HOME", home.join(".agents-router"))?,
            home,
            applications,
        })
    }
    pub fn launcher(&self) -> PathBuf {
        self.support.join("bin/agt")
    }
    pub fn public(&self) -> PathBuf {
        self.bin.join("agt")
    }
    pub fn current(&self) -> Option<PathBuf> {
        self.support
            .join("current")
            .canonicalize()
            .ok()
            .filter(|p| p.parent() == Some(self.support.join("runtimes").as_path()))
    }
    pub fn version(&self) -> Option<String> {
        self.current()
            .and_then(|p| manifest(&p).ok())
            .map(|m| m.runtime_version)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub schema_version: u32,
    pub source: String,
    pub app_version: String,
    pub runtime_version: String,
    pub python_version: String,
    pub plugin_path: PathBuf,
}
fn version_valid(s: &str) -> bool {
    s.starts_with(|c: char| c.is_ascii_digit())
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || ".+-".contains(c))
}
pub fn manifest(root: &Path) -> Result<Manifest> {
    let m: Manifest = serde_json::from_slice(&fs::read(root.join("agt-bundle.json")).map_err(err)?)
        .map_err(err)?;
    if m.schema_version != 1
        || m.source != "app"
        || [&m.app_version, &m.runtime_version, &m.python_version]
            .iter()
            .any(|s| !version_valid(s))
        || m.plugin_path.is_absolute()
        || m.plugin_path
            .components()
            .any(|c| !matches!(c, std::path::Component::Normal(_)))
    {
        return Err("Invalid bundled runtime manifest. Reinstall Agents Router.".into());
    }
    let physical = root.canonicalize().map_err(err)?;
    for required in [
        PathBuf::from("bin/python3"),
        m.plugin_path.join(".claude-plugin/plugin.json"),
        m.plugin_path.join(".claude-plugin/marketplace.json"),
        m.plugin_path.join("hooks/hooks.json"),
    ] {
        let path = root.join(required).canonicalize().map_err(err)?;
        if !path.starts_with(&physical) || !path.is_file() {
            return Err("Bundled runtime escapes its folder. Reinstall Agents Router.".into());
        }
    }
    let plugin: Value = serde_json::from_slice(
        &fs::read(root.join(&m.plugin_path).join(".claude-plugin/plugin.json")).map_err(err)?,
    )
    .map_err(err)?;
    if plugin["version"] != m.runtime_version {
        return Err("Bundled plugin version differs from agt. Reinstall Agents Router.".into());
    }
    Ok(m)
}
// Numeric components, with releases sorting after their prereleases (0.17 > 0.16).
fn version_cmp(a: &str, b: &str) -> Ordering {
    let parts = |s: &str| {
        s.split('+')
            .next()
            .unwrap_or(s)
            .split_once('-')
            .map(|(v, p)| (v.to_owned(), Some(p.to_owned())))
            .unwrap_or((s.split('+').next().unwrap_or(s).to_owned(), None))
    };
    let (a, ap) = parts(a);
    let (b, bp) = parts(b);
    let an: Vec<_> = a
        .split('.')
        .map(|v| v.parse::<u64>().unwrap_or(0))
        .collect();
    let bn: Vec<_> = b
        .split('.')
        .map(|v| v.parse::<u64>().unwrap_or(0))
        .collect();
    for i in 0..an.len().max(bn.len()) {
        let o = an.get(i).unwrap_or(&0).cmp(bn.get(i).unwrap_or(&0));
        if o != Ordering::Equal {
            return o;
        }
    }
    match (ap, bp) {
        (None, Some(_)) => Ordering::Greater,
        (Some(_), None) => Ordering::Less,
        (a, b) => a.cmp(&b),
    }
}
fn lock(path: &Path) -> Result<File> {
    fs::create_dir_all(path.parent().ok_or("Lock has no parent")?).map_err(err)?;
    let f = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .mode(0o600)
        .open(path)
        .map_err(err)?;
    f.try_lock()
        .map_err(|_| "Another agt operation is in progress. Try again.".to_owned())?;
    Ok(f)
}
fn atomic_bytes(path: &Path, bytes: &[u8], mode: u32) -> Result<()> {
    fs::create_dir_all(path.parent().ok_or("No parent")?).map_err(err)?;
    let temporary = path.with_file_name(format!(".write-{}", nonce()));
    let result = (|| {
        fs::write(&temporary, bytes).map_err(err)?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(mode)).map_err(err)?;
        File::open(&temporary)
            .and_then(|f| f.sync_all())
            .map_err(err)?;
        fs::rename(&temporary, path).map_err(err)
    })();
    let _ = fs::remove_file(temporary);
    result
}
fn atomic_link(target: &Path, path: &Path) -> Result<()> {
    let temp = path.with_file_name(format!(".link-{}", nonce()));
    symlink(target, &temp).map_err(err)?;
    let result = fs::rename(&temp, path).map_err(err);
    let _ = fs::remove_file(temp);
    result
}
/// Copies internal relative symlinks without following them. Reject external links/devices.
pub fn copy_tree(source: &Path, destination: &Path) -> io::Result<()> {
    fn copy(source: &Path, dest: &Path, root: &Path) -> io::Result<()> {
        let meta = fs::symlink_metadata(source)?;
        if meta.file_type().is_symlink() {
            let target = fs::read_link(source)?;
            if target.is_absolute() || !source.canonicalize()?.starts_with(root) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Payload has an external symlink",
                ));
            }
            symlink(target, dest)
        } else if meta.is_dir() {
            fs::create_dir(dest)?;
            for entry in fs::read_dir(source)? {
                let entry = entry?;
                copy(&entry.path(), &dest.join(entry.file_name()), root)?;
            }
            fs::set_permissions(dest, meta.permissions())
        } else if meta.is_file() {
            fs::copy(source, dest)?;
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Unsupported payload file",
            ))
        }
    }
    copy(source, destination, &source.canonicalize()?)
}
fn payload_size(root: &Path) -> io::Result<u64> {
    let mut total = 0;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let m = entry.file_type()?;
        total += if m.is_dir() {
            payload_size(&entry.path())?
        } else {
            entry.metadata()?.len()
        };
    }
    Ok(total)
}
pub fn install_error(e: &str, first: bool, needed: Option<u64>) -> String {
    let title = if first {
        "Couldn't install agt."
    } else {
        "Install didn't finish. Your previous agt still works."
    };
    if e.contains("os error 28") {
        format!(
            "{title} Free at least {} bytes, then try again.",
            needed.unwrap_or(0)
        )
    } else {
        format!("{title} {e}")
    }
}
fn writable(path: &Path) -> bool {
    // Permission bits also let the scratch check exercise a read-only volume as root.
    if fs::metadata(path).is_ok_and(|m| m.permissions().mode() & 0o222 == 0) {
        return false;
    }
    let probe = path.join(format!(".agt-write-{}", nonce()));
    match OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&probe)
    {
        Ok(_) => {
            let _ = fs::remove_file(probe);
            true
        }
        Err(_) => false,
    }
}
pub fn valid_location(app: &Path, paths: &InstallPaths) -> bool {
    let Ok(app) = app.canonicalize() else {
        return false;
    };
    !app.to_string_lossy().contains("AppTranslocation")
        && paths.applications.iter().any(|dir| {
            dir.canonicalize()
                .is_ok_and(|p| app.parent() == Some(p.as_path()))
        })
        && writable(&app)
}
/// Returns an argv intent; production opens it with /usr/bin/open then exits.
pub fn move_application(app: &Path, paths: &InstallPaths) -> Result<Vec<String>> {
    if app.to_string_lossy().contains("/Contents/") {
        return Err("Choose the app bundle".into());
    }
    for dir in &paths.applications {
        if fs::create_dir_all(dir).is_err() || !writable(dir) {
            continue;
        }
        let target = dir.join("Agents Router.app");
        if present(&target) {
            return Err(format!(
                "{} already exists. Move it aside, then try again.",
                target.display()
            ));
        }
        let staging = dir.join(format!(".Agents-Router-{}.app", nonce()));
        if let Err(e) = copy_tree(app, &staging) {
            let _ = fs::remove_dir_all(&staging);
            return Err(err(e));
        }
        fs::rename(&staging, &target).map_err(err)?;
        return Ok(vec![
            "/usr/bin/open".into(),
            "-n".into(),
            target.to_string_lossy().into_owned(),
        ]);
    }
    Err(
        "Couldn't copy Agents Router. Make your Applications folder writable, then try again."
            .into(),
    )
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ForeignCli {
    pub target: PathBuf,
    pub version: String,
    pub source: String,
}
#[derive(Clone, Default, Serialize, Deserialize)]
struct Record {
    previous: Option<PathBuf>,
    foreign: Option<ForeignCli>,
    replaced: bool,
    setup_unknown: bool,
    setup_pending: bool,
    first_complete: bool,
}
fn record(paths: &InstallPaths) -> Record {
    fs::read(paths.support.join("install.json"))
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}
fn save(paths: &InstallPaths, r: &Record) -> Result<()> {
    atomic_bytes(
        &paths.support.join("install.json"),
        &serde_json::to_vec(r).map_err(err)?,
        0o600,
    )
}
pub fn owns_public(paths: &InstallPaths) -> bool {
    fs::read_link(paths.public()).is_ok_and(|t| t == paths.launcher())
}
fn foreign_exists(paths: &InstallPaths, f: &ForeignCli) -> bool {
    let backup = paths.support.join("previous-cli");
    (fs::symlink_metadata(&backup).is_ok_and(|m| m.is_file() && !m.file_type().is_symlink()))
        || (f.target != paths.public() && f.target.exists())
}
pub fn foreign_cli(paths: &InstallPaths) -> Option<ForeignCli> {
    if !present(&paths.public()) || owns_public(paths) {
        return None;
    }
    let target = paths.public().canonicalize().ok()?;
    let text = target.to_string_lossy();
    let source = if text.contains("/uv/") {
        "uv"
    } else if text.contains("/pipx/") {
        "pipx"
    } else {
        "source"
    };
    // Read metadata only; never execute a foreign agt.
    fn metadata_version(folder: &Path) -> Option<String> {
        fs::read_dir(folder).ok()?.flatten().find_map(|e| {
            let name = e.file_name().to_string_lossy().into_owned();
            name.strip_prefix("agents_router-")
                .or_else(|| name.strip_prefix("agents-router-"))
                .and_then(|s| s.strip_suffix(".dist-info"))
                .filter(|v| version_valid(v))
                .map(str::to_owned)
        })
    }
    let version = target
        .ancestors()
        .take(6)
        .find_map(|p| {
            metadata_version(p).or_else(|| {
                fs::read_dir(p.join("lib"))
                    .ok()?
                    .flatten()
                    .filter(|e| e.file_name().to_string_lossy().starts_with("python"))
                    .find_map(|e| metadata_version(&e.path().join("site-packages")))
            })
        })
        .unwrap_or_else(|| "unknown".into());
    Some(ForeignCli {
        target,
        version,
        source: source.into(),
    })
}
async fn output(
    paths: &InstallPaths,
    exe: &Path,
    args: &[&str],
    deadline: Duration,
) -> Result<Vec<u8>> {
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .current_dir(&paths.home)
        .env("AGT_APP_SUPPORT", &paths.support)
        .env("AGT_BIN_DIR", &paths.bin)
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME")
        .kill_on_drop(true);
    if std::env::var_os("AGT_HOME").is_some() {
        cmd.env("AGT_HOME", &paths.agt_home);
    } else {
        cmd.env_remove("AGT_HOME");
    }
    let out = timeout(deadline, cmd.output())
        .await
        .map_err(|_| "Timed out".to_owned())?
        .map_err(err)?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_owned());
    }
    Ok(out.stdout)
}
async fn validate(paths: &InstallPaths, runtime: &Path) -> Result<Manifest> {
    let m = manifest(runtime)?;
    let out = output(
        paths,
        &runtime.join("bin/python3"),
        &["-I", "-m", "agents_router", "--version"],
        Duration::from_secs(15),
    )
    .await?;
    if String::from_utf8_lossy(&out).split_whitespace().last() != Some(&m.runtime_version) {
        return Err(
            "Bundled agt version differs from its manifest. Reinstall Agents Router.".into(),
        );
    }
    Ok(m)
}
fn launcher(paths: &InstallPaths) -> String {
    format!("#!/bin/sh\nset -eu\nexport AGT_APP_SUPPORT={}\nexport AGT_BIN_DIR={}\nruntime=$(CDPATH='' cd -- \"$AGT_APP_SUPPORT/current\" && pwd -P)\nexec \"$runtime/bin/python3\" -I -m agents_router \"$@\"\n", posix_quote(&paths.support.to_string_lossy()), posix_quote(&paths.bin.to_string_lossy()))
}
#[derive(Clone, Debug, Serialize)]
pub struct Installed {
    pub first: bool,
    pub version: String,
    pub foreign: Option<ForeignCli>,
}
pub async fn install(
    paths: &InstallPaths,
    app: &Path,
    payload: &Path,
    confirm_downgrade: bool,
) -> Result<Installed> {
    let first = paths.current().is_none();
    let needed = payload_size(payload).ok();
    install_inner(paths, app, payload, confirm_downgrade)
        .await
        .map_err(|e| {
            if e == "Downgrade requires confirmation"
                || e == "Move Agents Router to Applications to continue."
                || e.starts_with("Couldn't install agt.")
                || e.starts_with("Install didn't finish.")
            {
                e
            } else {
                install_error(&e, first, needed)
            }
        })
}
async fn install_inner(
    paths: &InstallPaths,
    app: &Path,
    payload: &Path,
    confirm_downgrade: bool,
) -> Result<Installed> {
    if !valid_location(app, paths) {
        return Err("Move Agents Router to Applications to continue.".into());
    }
    let _lock = lock(&paths.support.join("install.lock"))?;
    let first = paths.current().is_none();
    let m = manifest(payload).map_err(|e| install_error(&e, first, None))?;
    if paths
        .version()
        .is_some_and(|v| version_cmp(&m.runtime_version, &v) == Ordering::Less)
        && !confirm_downgrade
    {
        return Err("Downgrade requires confirmation".into());
    }
    let runtimes = paths.support.join("runtimes");
    fs::create_dir_all(&runtimes).map_err(err)?;
    // Only a lock holder may clean incomplete copies from a previous crash.
    for entry in fs::read_dir(&runtimes).map_err(err)?.flatten() {
        if entry.file_name().to_string_lossy().starts_with(".staging-") {
            fs::remove_dir_all(entry.path()).map_err(err)?;
        }
    }
    let target = runtimes.join(&m.runtime_version);
    if !target.exists() {
        let staged = runtimes.join(format!(".staging-{}", nonce()));
        let needed = payload_size(payload).ok();
        if let Err(e) = copy_tree(payload, &staged) {
            let _ = fs::remove_dir_all(&staged);
            return Err(install_error(&err(e), first, needed));
        }
        if let Err(e) = validate(paths, &staged).await {
            let _ = fs::remove_dir_all(&staged);
            return Err(install_error(&e, first, needed));
        }
        fs::rename(&staged, &target).map_err(|e| install_error(&err(e), first, needed))?;
    } else {
        validate(paths, &target)
            .await
            .map_err(|e| install_error(&e, first, None))?;
    }
    let mut r = record(paths);
    let old = paths.current();
    if old.as_ref() != Some(&target) {
        r.previous = old;
    }
    if first {
        r.setup_pending = true;
    }
    // Persist recovery/retention information before the commit point.
    save(paths, &r)?;
    atomic_bytes(&paths.launcher(), launcher(paths).as_bytes(), 0o755)?;
    atomic_link(
        &PathBuf::from("runtimes").join(&m.runtime_version),
        &paths.support.join("current"),
    )?;
    fs::create_dir_all(&paths.bin).map_err(err)?;
    // create_new symlink does not overwrite a CLI installed while copying.
    if !present(&paths.public()) {
        match symlink(paths.launcher(), paths.public()) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(err(e)),
        }
    }
    Ok(Installed {
        first,
        version: m.runtime_version,
        foreign: foreign_cli(paths),
    })
}
pub fn replace_foreign(paths: &InstallPaths, expected: &ForeignCli) -> Result<()> {
    let _lock = lock(&paths.support.join("install.lock"))?;
    if foreign_cli(paths).as_ref().map(|f| &f.target) != Some(&expected.target) {
        return Err("agt changed elsewhere. Check again before replacing it.".into());
    }
    let mut r = record(paths);
    r.foreign = Some(expected.clone());
    r.replaced = true;
    save(paths, &r)?;
    // Keep the actual file/link as well as the recorded physical target.
    let backup = paths.support.join("previous-cli");
    if present(&backup) {
        return Err("A previous agt is already saved. Restore it before replacing another.".into());
    }
    fs::rename(paths.public(), &backup).map_err(err)?;
    if let Err(e) = symlink(paths.launcher(), paths.public()) {
        let _ = fs::rename(&backup, paths.public());
        return Err(err(e));
    }
    Ok(())
}
pub fn restore_cli(paths: &InstallPaths) -> Result<()> {
    let _lock = lock(&paths.support.join("install.lock"))?;
    let mut r = record(paths);
    let foreign = r
        .foreign
        .as_ref()
        .filter(|f| foreign_exists(paths, f))
        .ok_or("Previous agt no longer exists")?;
    if !owns_public(paths) {
        return Err("agt changed elsewhere. Keeping it untouched.".into());
    }
    fs::remove_file(paths.public()).map_err(err)?;
    let backup = paths.support.join("previous-cli");
    if present(&backup) {
        fs::rename(backup, paths.public()).map_err(err)?;
    } else {
        symlink(&foreign.target, paths.public()).map_err(err)?;
    }
    r.replaced = false;
    save(paths, &r)
}
pub async fn usage(paths: &InstallPaths) -> Result<Value> {
    let bytes = output(
        paths,
        &paths.launcher(),
        &["usage", "--json"],
        Duration::from_secs(15),
    )
    .await?;
    let v: Value = serde_json::from_slice(&bytes).map_err(err)?;
    if v["schema_version"] != 1 || !v["uninstall_blockers"].is_array() {
        return Err("Couldn't check running jobs. Repair agt, then try again.".into());
    }
    Ok(v)
}
async fn admission(paths: &InstallPaths) -> Result<File> {
    let start = tokio::time::Instant::now();
    loop {
        match lock(&paths.agt_home.join("state/admission.lock")) {
            Ok(f) => return Ok(f),
            Err(e) if start.elapsed() >= Duration::from_secs(5) => return Err(e),
            Err(_) => tokio::time::sleep(Duration::from_millis(25)).await,
        }
    }
}
pub async fn uninstall(paths: &InstallPaths) -> Result<()> {
    let _install = lock(&paths.support.join("install.lock"))?;
    let _admission = admission(paths).await?;
    // Never trust the UI's cached count. Plain usage does not take admission.lock.
    let current = paths.current().ok_or("agt wasn't installed by this app")?;
    manifest(&current)?;
    let report = usage(paths).await?;
    let blockers = report["uninstall_blockers"]
        .as_array()
        .ok_or("Can't check jobs")?;
    if !blockers.is_empty() {
        return Err(format!(
            "Can't uninstall while {} job{} run.",
            blockers.len(),
            if blockers.len() == 1 { "" } else { "s" }
        ));
    }
    if owns_public(paths) {
        let r = record(paths);
        let restore = r.replaced && r.foreign.as_ref().is_some_and(|f| foreign_exists(paths, f));
        fs::remove_file(paths.public()).map_err(err)?;
        if restore {
            let backup = paths.support.join("previous-cli");
            if present(&backup) {
                fs::rename(backup, paths.public()).map_err(err)?;
            } else {
                symlink(&r.foreign.unwrap().target, paths.public()).map_err(err)?;
            }
        }
    }
    // Keep settings, jobs, saved foreign CLI and the install lock's inode.
    for file in ["current", "bin/agt", "install.json"] {
        let p = paths.support.join(file);
        if present(&p) {
            fs::remove_file(p).map_err(err)?;
        }
    }
    for dir in ["runtimes", "plugins"] {
        let p = paths.support.join(dir);
        if p.exists() {
            fs::remove_dir_all(p).map_err(err)?;
        }
    }
    Ok(())
}
pub async fn cleanup(paths: &InstallPaths) -> Result<Vec<PathBuf>> {
    let _install = lock(&paths.support.join("install.lock"))?;
    let _admission = admission(paths).await?;
    let report = usage(paths).await?;
    let mut keep: Vec<PathBuf> = paths
        .current()
        .into_iter()
        .chain(record(paths).previous)
        .collect();
    for blocker in report["uninstall_blockers"]
        .as_array()
        .ok_or("Can't check jobs")?
    {
        let Some(id) = blocker["job_id"].as_str() else {
            return Err("Unknown job reference; keeping all runtimes".into());
        };
        if id.contains('/') || id.contains("..") {
            return Err("Invalid job reference".into());
        }
        let mut found = false;
        for file in ["spec.json", "status.json"] {
            let doc: Value = fs::read(paths.agt_home.join("jobs").join(id).join(file))
                .ok()
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or(Value::Null);
            if let Some(runtime) = doc["install_runtime"].as_str() {
                keep.push(PathBuf::from(runtime));
                found = true;
            }
        }
        if !found {
            return Err("Job has no runtime reference; keeping all runtimes".into());
        }
    }
    let mut removed = Vec::new();
    for entry in fs::read_dir(paths.support.join("runtimes"))
        .map_err(err)?
        .flatten()
    {
        let p = entry.path();
        if !keep.contains(&p)
            && manifest(&p).is_ok()
            && !entry.file_type().map_err(err)?.is_symlink()
        {
            fs::remove_dir_all(&p).map_err(err)?;
            removed.push(p);
        }
    }
    Ok(removed)
}
/// Dry-run uses a deadline independently of installation. Only positive planned writes show onboarding.
pub async fn setup_status(paths: &InstallPaths, deadline: Duration) -> Result<bool> {
    let result = output(
        paths,
        &paths.launcher(),
        &["setup", "--dry-run", "--json"],
        deadline,
    )
    .await;
    let mut r = record(paths);
    match result.and_then(|b| serde_json::from_slice::<Value>(&b).map_err(err)) {
        Ok(v)
            if v["dry_run"] == true
                && v["plugin"]["planned"].is_array()
                && v["plugin"]["failed"]
                    .as_array()
                    .is_some_and(|a| a.is_empty()) =>
        {
            let plugin = !v["plugin"]["planned"].as_array().unwrap().is_empty();
            let roles = v["roles"]["planned"]
                .as_array()
                .is_some_and(|a| !a.is_empty());
            let sharing = [
                &v["share"]["instructions"]["writes"],
                &v["share"]["changes"],
                &v["share"]["mcp"]["writes"],
                &v["share"]["plugins"]["installs"],
                &v["share"]["would_save"],
            ]
            .iter()
            .any(|v| v.as_array().is_some_and(|a| !a.is_empty()));
            r.setup_unknown = false;
            r.setup_pending = plugin || roles || sharing;
            save(paths, &r)?;
            Ok(r.setup_pending)
        }
        _ => {
            r.setup_unknown = true;
            save(paths, &r)?;
            Err("Setup status unknown".into())
        }
    }
}
pub async fn refresh_plugins(paths: &InstallPaths) -> Result<()> {
    if record(paths).previous.is_none() {
        return Ok(());
    }
    let _lock = lock(&paths.support.join("install.lock"))?;
    let version = paths.version().ok_or("No installed runtime")?;
    let marker = paths.support.join("updated").join(&version);
    if marker.is_file() {
        return Ok(());
    }
    output(
        paths,
        &paths.launcher(),
        &["update", "--no-package"],
        Duration::from_secs(120),
    )
    .await?;
    atomic_bytes(&marker, b"updated\n", 0o600)
}
pub fn finish_first_launch(paths: &InstallPaths) -> Result<()> {
    let mut r = record(paths);
    r.first_complete = true;
    r.setup_pending = false;
    save(paths, &r)
}
pub fn first_launch_pending(paths: &InstallPaths) -> bool {
    !record(paths).first_complete
}
pub fn setup_unknown(paths: &InstallPaths) -> bool {
    record(paths).setup_unknown
}
pub fn saved_foreign(paths: &InstallPaths) -> Option<ForeignCli> {
    record(paths).foreign
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn versions_and_disk_error() {
        assert_eq!(version_cmp("0.17", "0.16.99"), Ordering::Greater);
        assert_eq!(version_cmp("0.17.0-rc1", "0.17.0"), Ordering::Less);
        assert!(install_error("os error 28", true, Some(1234)).contains("Free at least 1234 bytes"));
    }
}
