//! Runtime transactions. No PATH-selected executable, shell IPC, or package manager.
use crate::{
    app_paths::{current_link, inside, physical, AppPaths, ChildEnv, EXCLUDE_HOMES},
    terminal::posix_quote,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    cmp::Ordering,
    ffi::OsString,
    fs::{self, File, OpenOptions, TryLockError},
    io,
    os::unix::fs::{symlink, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
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
/// The release's resolution, unchanged: the nearest ancestor that exists,
/// links followed, made physical, plus the missing parts. A dangling link
/// counts as missing, so the app still launches and the install reports it.
fn absolute(path: &Path) -> Result<PathBuf> {
    if !path.is_absolute() {
        return Err(format!("{} must be absolute", path.display()));
    }
    let mut ancestor = path;
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
/// A test build's private HOME, settings folder and `AGT_EXCLUDE_HOMES`; the
/// launcher bakes them in.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Isolated {
    pub home: PathBuf,
    pub config: PathBuf,
    pub exclude: OsString,
}
#[derive(Clone, Debug)]
pub struct InstallPaths {
    pub support: PathBuf,
    pub bin: PathBuf,
    pub agt_home: PathBuf,
    /// The person's own HOME: Applications destinations.
    pub home: PathBuf,
    pub applications: Vec<PathBuf>,
    /// Release: pass AGT_HOME to children only when the person set it.
    pub explicit_agt_home: bool,
    /// Test builds only.
    pub isolated: Option<Isolated>,
}
impl InstallPaths {
    /// Release resolution from the environment: developer checks.
    pub fn discover() -> Result<Self> {
        Self::from_app(&AppPaths::from_env()?)
    }
    /// The installer works on physical paths resolved from the app's folders.
    /// A test build refuses a dangling link; the release walks past one as before.
    pub fn from_app(paths: &AppPaths) -> Result<Self> {
        let resolve: fn(&Path) -> Result<PathBuf> = if paths.test { physical } else { absolute };
        let support = resolve(&paths.support.path)?;
        let bin = resolve(&paths.bin.path)?;
        let agt_home = resolve(&paths.agt_home.path)?;
        let home = resolve(&paths.home)?;
        // As before: the default destinations sit under the resolved HOME, so a
        // HOME link changed after launch can't move them; AGT_APPLICATIONS_DIRS as given.
        let applications = paths
            .applications
            .clone()
            .unwrap_or_else(|| vec![PathBuf::from("/Applications"), home.join("Applications")]);
        Ok(Self {
            bin,
            agt_home,
            home,
            applications,
            explicit_agt_home: paths.explicit_agt_home,
            isolated: if paths.test {
                Some(Isolated {
                    // Never canonicalized: a link there would bake its target into the launcher.
                    home: inside(&support, Path::new("home"))?,
                    config: physical(&paths.config.path)?,
                    exclude: paths
                        .exclude_homes
                        .clone()
                        .ok_or("A test build needs its excluded homes")?,
                })
            } else {
                None
            },
            support,
        })
    }
    /// A path below the app support folder. A test build checks, each time,
    /// that no part of it is a symlink; the release joins as before.
    fn own(&self, relative: impl AsRef<Path>) -> Result<PathBuf> {
        match self.isolated {
            Some(_) => inside(&self.support, relative.as_ref()),
            None => Ok(self.support.join(relative)),
        }
    }
    /// The same below the agt home: its admission lock.
    fn own_agt(&self, relative: &str) -> Result<PathBuf> {
        match self.isolated {
            Some(_) => inside(&self.agt_home, Path::new(relative)),
            None => Ok(self.agt_home.join(relative)),
        }
    }
    /// A test build's private HOME, checked again before a child starts in it.
    fn checked_child_home(&self) -> Result<&Path> {
        if self.isolated.is_some() {
            self.own("home")?;
        }
        Ok(self.child_home())
    }
    /// The HOME and working folder of installer children.
    pub fn child_home(&self) -> &Path {
        self.isolated.as_ref().map_or(&self.home, |i| &i.home)
    }
    fn child_env(&self) -> ChildEnv {
        let mut env: ChildEnv = vec![
            ("AGT_APP_SUPPORT", Some(self.support.clone().into())),
            ("AGT_BIN_DIR", Some(self.bin.clone().into())),
            ("PYTHONPATH", None),
            ("PYTHONHOME", None),
        ];
        match &self.isolated {
            None => env.push((
                "AGT_HOME",
                self.explicit_agt_home
                    .then(|| self.agt_home.clone().into_os_string()),
            )),
            Some(isolated) => env.extend([
                ("AGT_HOME", Some(self.agt_home.clone().into())),
                ("AGT_CONFIG_DIR", Some(isolated.config.clone().into())),
                ("HOME", Some(isolated.home.clone().into())),
                ("CFFIXED_USER_HOME", Some(isolated.home.clone().into())),
                (EXCLUDE_HOMES, Some(isolated.exclude.clone())),
                ("CLAUDE_CONFIG_DIR", None),
                ("CODEX_HOME", None),
                ("GEMINI_CLI_HOME", None),
            ]),
        }
        env
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
/// `nofollow` (test builds): a lock file that is a symlink fails to open
/// instead of creating or locking the file it points to. The release opens
/// its locks as before.
async fn lock(path: &Path, nofollow: bool) -> Result<File> {
    fs::create_dir_all(path.parent().ok_or("Lock has no parent")?).map_err(err)?;
    let mut options = OpenOptions::new();
    options
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .mode(0o600);
    let f = if nofollow {
        options
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)
            .map_err(|e| format!("{}: {e}", path.display()))?
    } else {
        options.open(path).map_err(err)?
    };
    // A forked child can briefly retain a released lock until exec. Give it
    // time to close the inherited descriptor without blocking an async worker.
    let deadline = Instant::now() + Duration::from_secs(1);
    loop {
        if Instant::now() >= deadline {
            return Err("Another agt operation is in progress. Try again.".into());
        }
        match f.try_lock() {
            Ok(()) => return Ok(f),
            Err(TryLockError::WouldBlock) => (),
            Err(e) => return Err(err(e)),
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        tokio::time::sleep(Duration::from_millis(10).min(remaining)).await;
    }
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
/// `relaunch` carries a test build's chosen folders; the release passes none.
pub fn move_application(
    app: &Path,
    paths: &InstallPaths,
    relaunch: &[String],
) -> Result<Vec<String>> {
    if app.to_string_lossy().contains("/Contents/") {
        return Err("Choose the app bundle".into());
    }
    // The release lands as "Agents Router.app", as before. A test build keeps
    // its own bundle name, "Agents Router Test.app", beside the release.
    let name = match (&paths.isolated, app.file_name()) {
        (None, _) => std::ffi::OsStr::new("Agents Router.app"),
        (Some(_), Some(name)) if app.extension().is_some_and(|e| e == "app") => name,
        (Some(_), _) => return Err("Choose the app bundle".into()),
    };
    for dir in &paths.applications {
        if fs::create_dir_all(dir).is_err() || !writable(dir) {
            continue;
        }
        let target = dir.join(name);
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
        let mut argv = vec![
            "/usr/bin/open".into(),
            "-n".into(),
            target.to_string_lossy().into_owned(),
        ];
        if !relaunch.is_empty() {
            argv.push("--args".into());
            argv.extend(relaunch.iter().cloned());
        }
        return Ok(argv);
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
    paths
        .own("install.json")
        .ok()
        .and_then(|p| fs::read(p).ok())
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}
fn save(paths: &InstallPaths, r: &Record) -> Result<()> {
    atomic_bytes(
        &paths.own("install.json")?,
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
        .current_dir(paths.checked_child_home()?)
        .kill_on_drop(true);
    for (name, value) in paths.child_env() {
        match value {
            Some(value) => cmd.env(name, value),
            None => cmd.env_remove(name),
        };
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
    let quote = |p: &Path| posix_quote(&p.to_string_lossy());
    // A test build's launcher carries every folder and its private HOME, so
    // Terminal, hooks and MCP servers started from it stay in the test folders.
    // AGT_EXCLUDE_HOMES keeps agt from taking the person's own HOME, which the
    // private HOME hides from it, as a workspace; the caller's folder stays.
    let isolated = paths.isolated.as_ref().map_or(String::new(), |i| {
        format!(
            "export AGT_HOME={}\nexport AGT_CONFIG_DIR={}\nexport HOME={}\nexport CFFIXED_USER_HOME={}\nexport {EXCLUDE_HOMES}={}\nunset CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME\n",
            quote(&paths.agt_home),
            quote(&i.config),
            quote(&i.home),
            quote(&i.home),
            quote(Path::new(&i.exclude))
        )
    });
    format!("#!/bin/sh\nset -eu\nexport AGT_APP_SUPPORT={}\nexport AGT_BIN_DIR={}\n{isolated}runtime=$(CDPATH='' cd -- \"$AGT_APP_SUPPORT/current\" && pwd -P)\nexec \"$runtime/bin/python3\" -I -m agents_router \"$@\"\n", quote(&paths.support), quote(&paths.bin))
}
/// A test build relaunched with other folders must rewrite the launcher, whose
/// baked folders would otherwise win over the ones the app passes.
pub fn launcher_current(paths: &InstallPaths) -> bool {
    fs::read_to_string(paths.launcher()).is_ok_and(|text| text == launcher(paths))
}
#[derive(Clone, Debug, Serialize)]
pub struct Installed {
    pub first: bool,
    pub version: String,
    pub foreign: Option<ForeignCli>,
}
/// The app and bundle checker install the payload belonging to this exact app.
pub async fn install_bundle(
    paths: &InstallPaths,
    app: &Path,
    confirm_downgrade: bool,
) -> Result<Installed> {
    if app.extension().is_none_or(|ext| ext != "app") || !app.join("Contents").is_dir() {
        return Err(format!("Missing app bundle: {}", app.display()));
    }
    install(
        paths,
        app,
        &app.join("Contents/Resources/runtime"),
        confirm_downgrade,
    )
    .await
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
    let _lock = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
    let first = paths.current().is_none();
    let m = manifest(payload).map_err(|e| install_error(&e, first, None))?;
    if paths
        .version()
        .is_some_and(|v| version_cmp(&m.runtime_version, &v) == Ordering::Less)
        && !confirm_downgrade
    {
        return Err("Downgrade requires confirmation".into());
    }
    let runtimes = paths.own("runtimes")?;
    fs::create_dir_all(&runtimes).map_err(err)?;
    // Only a lock holder may clean incomplete copies from a previous crash.
    for entry in fs::read_dir(&runtimes).map_err(err)?.flatten() {
        if entry.file_name().to_string_lossy().starts_with(".staging-") {
            fs::remove_dir_all(entry.path()).map_err(err)?;
        }
    }
    let target = paths.own(Path::new("runtimes").join(&m.runtime_version))?;
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
    atomic_bytes(&paths.own("bin/agt")?, launcher(paths).as_bytes(), 0o755)?;
    // `current` is a link by design: rename replaces the link itself and never
    // follows it. A test build first checks it is the installer's own.
    if paths.isolated.is_some() {
        current_link(&paths.support)?;
    }
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
pub async fn replace_foreign(paths: &InstallPaths, expected: &ForeignCli) -> Result<()> {
    let _lock = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
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
pub async fn restore_cli(paths: &InstallPaths) -> Result<()> {
    let _lock = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
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
        match lock(
            &paths.own_agt("state/admission.lock")?,
            paths.isolated.is_some(),
        )
        .await
        {
            Ok(f) => return Ok(f),
            Err(e) if start.elapsed() >= Duration::from_secs(5) => return Err(e),
            Err(_) => tokio::time::sleep(Duration::from_millis(25)).await,
        }
    }
}
pub async fn uninstall(paths: &InstallPaths) -> Result<()> {
    let _install = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
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
        // remove_file unlinks `current` itself; the others mustn't sit behind a link.
        let p = if file == "current" {
            paths.support.join(file)
        } else {
            paths.own(file)?
        };
        if present(&p) {
            fs::remove_file(p).map_err(err)?;
        }
    }
    for dir in ["runtimes", "plugins"] {
        let p = paths.own(dir)?;
        if p.exists() {
            fs::remove_dir_all(p).map_err(err)?;
        }
    }
    Ok(())
}
pub async fn cleanup(paths: &InstallPaths) -> Result<Vec<PathBuf>> {
    let _install = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
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
    for entry in fs::read_dir(paths.own("runtimes")?).map_err(err)?.flatten() {
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
    let _lock = lock(&paths.own("install.lock")?, paths.isolated.is_some()).await?;
    let version = paths.version().ok_or("No installed runtime")?;
    let marker = paths.own(Path::new("updated").join(&version))?;
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
    atomic_bytes(&marker, b"updated\n", 0o600)?;
    crate::app::log(&format!("plugins refreshed version={version}"));
    #[cfg(debug_assertions)]
    if let Some(root) = std::env::var_os("AGT_UPDATE_TEST_ROOT").map(PathBuf::from) {
        if paths.support == root.join("support") && root.join("test-only").is_file() {
            use std::io::Write;
            let mut audit = OpenOptions::new()
                .create(true)
                .append(true)
                .open(root.join("refresh-calls"))
                .map_err(err)?;
            writeln!(audit, "{version}").map_err(err)?;
        }
    }
    Ok(())
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
    #[tokio::test]
    async fn refresh_record_is_once_per_version_and_failure_can_retry() {
        let root = std::env::temp_dir().join(format!("agt-refresh-{}", nonce()));
        fs::create_dir_all(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let paths = InstallPaths {
            support: root.join("support"),
            bin: root.join("bin"),
            agt_home: root.join("agt"),
            home: root.clone(),
            applications: vec![root.join("Applications")],
            explicit_agt_home: true,
            isolated: None,
        };
        fs::create_dir_all(paths.launcher().parent().unwrap()).unwrap();
        atomic_bytes(&paths.launcher(), b"#!/bin/sh\n[ \"$1 $2\" = 'update --no-package' ] || exit 99\nprintf 'run\\n' >> calls\n[ ! -f fail ]\n", 0o755).unwrap();
        save(
            &paths,
            &Record {
                previous: Some(root.join("old")),
                ..Default::default()
            },
        )
        .unwrap();
        for version in ["0.17.1", "0.17.2"] {
            let runtime = paths.support.join("runtimes").join(version);
            for name in [
                "bin/python3",
                "plugin/.claude-plugin/plugin.json",
                "plugin/.claude-plugin/marketplace.json",
                "plugin/hooks/hooks.json",
            ] {
                let path = runtime.join(name);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(path, "{}").unwrap();
            }
            fs::write(
                runtime.join("plugin/.claude-plugin/plugin.json"),
                format!("{{\"version\":\"{version}\"}}"),
            )
            .unwrap();
            fs::write(runtime.join("agt-bundle.json"), serde_json::to_vec(&serde_json::json!({"schema_version":1,"source":"app","app_version":version,"runtime_version":version,"python_version":"3.13.18","plugin_path":"plugin"})).unwrap()).unwrap();
            atomic_link(&runtime, &paths.support.join("current")).unwrap();
            if version == "0.17.1" {
                fs::write(root.join("fail"), "").unwrap();
                assert!(refresh_plugins(&paths).await.is_err());
                assert!(!paths.support.join("updated").join(version).exists());
                fs::remove_file(root.join("fail")).unwrap();
            }
            refresh_plugins(&paths).await.unwrap();
            refresh_plugins(&paths).await.unwrap(); // retained webview/relaunch cannot repeat it
            assert!(paths.support.join("updated").join(version).is_file());
        }
        assert_eq!(
            fs::read_to_string(root.join("calls"))
                .unwrap()
                .lines()
                .count(),
            3
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[tokio::test]
    async fn lock_retries_until_contending_descriptor_is_released() {
        let root = crate::terminal::tests::Scratch::new("lock-retry");
        let path = root.0.join("install.lock");
        let held = File::create(&path).unwrap();
        held.try_lock().unwrap();
        let start = Instant::now();
        let release = async {
            tokio::time::sleep(Duration::from_millis(100)).await;
            held.unlock().unwrap();
        };
        // Both futures run on one worker: the retry must yield for release.
        let (_, acquired) = timeout(Duration::from_secs(3), async {
            tokio::join!(release, lock(&path, false))
        })
        .await
        .expect("lock retry hung");
        let acquired = acquired.expect("lock was not acquired after release");
        assert!(start.elapsed() >= Duration::from_millis(100));
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(matches!(held.try_lock(), Err(TryLockError::WouldBlock)));
        drop(acquired);
    }
    #[tokio::test]
    async fn lock_keeps_real_contention_busy_after_one_second() {
        let root = crate::terminal::tests::Scratch::new("lock-busy");
        let path = root.0.join("install.lock");
        let held = File::create(&path).unwrap();
        held.try_lock().unwrap();
        let start = Instant::now();
        let error = timeout(Duration::from_secs(3), lock(&path, false))
            .await
            .expect("lock contention hung")
            .unwrap_err();
        assert_eq!(error, "Another agt operation is in progress. Try again.");
        assert!(start.elapsed() >= Duration::from_secs(1));
        assert!(start.elapsed() < Duration::from_secs(2));
        held.unlock().unwrap();
        timeout(Duration::from_secs(2), lock(&path, false))
            .await
            .expect("lock retry hung after contention")
            .expect("failed lock attempt kept the lock");
    }
    #[tokio::test]
    async fn installer_children_follow_the_build_and_launchers_carry_test_folders() {
        let root = crate::terminal::tests::Scratch::new("install-env");
        let base = root.0.canonicalize().unwrap();
        let probe = base.join("probe");
        atomic_bytes(&probe, b"#!/bin/sh\nprintf '%s|%s|%s|%s|%s|%s|%s' \"${AGT_HOME-unset}\" \"${AGT_CONFIG_DIR-unset}\" \"$HOME\" \"${CODEX_HOME-unset}\" \"$AGT_BIN_DIR\" \"$(pwd -P)\" \"${AGT_EXCLUDE_HOMES-unset}\"\n", 0o755).unwrap();
        let mut paths = InstallPaths {
            support: base.join("support"),
            bin: base.join("bin"),
            agt_home: base.join("agt"),
            home: base.clone(),
            applications: vec![base.join("Applications")],
            explicit_agt_home: false,
            isolated: None,
        };
        // The release passes AGT_HOME only when the person set it.
        let release = output(&paths, &probe, &[], Duration::from_secs(10))
            .await
            .unwrap();
        let text = String::from_utf8(release).unwrap();
        assert!(
            text.starts_with("unset|") && text.ends_with("|unset"),
            "{text}"
        );
        assert!(!launcher(&paths).contains("HOME"));
        let private = base.join("support/home");
        fs::create_dir_all(&private).unwrap();
        let exclude = std::env::join_paths([&private, &base]).unwrap();
        paths.isolated = Some(Isolated {
            home: private.clone(),
            config: base.join("config"),
            exclude: exclude.clone(),
        });
        let test = output(&paths, &probe, &[], Duration::from_secs(10))
            .await
            .unwrap();
        assert_eq!(
            String::from_utf8(test).unwrap(),
            format!(
                "{}|{}|{}|unset|{}|{}|{}",
                base.join("agt").display(),
                base.join("config").display(),
                private.display(),
                base.join("bin").display(),
                private.display(),
                exclude.to_string_lossy()
            )
        );
        let text = launcher(&paths);
        for line in [
            format!(
                "export AGT_HOME={}",
                posix_quote(&base.join("agt").to_string_lossy())
            ),
            format!("export HOME={}", posix_quote(&private.to_string_lossy())),
            format!(
                "export AGT_EXCLUDE_HOMES={}",
                posix_quote(&exclude.to_string_lossy())
            ),
            "unset CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME".into(),
        ] {
            assert!(text.lines().any(|l| l == line), "{line}\n{text}");
        }
        assert!(!launcher_current(&paths));
        atomic_bytes(&paths.launcher(), text.as_bytes(), 0o755).unwrap();
        assert!(launcher_current(&paths));
    }
    fn test_build_paths(base: &Path) -> InstallPaths {
        let private = base.join("support/home");
        fs::create_dir_all(&private).unwrap();
        InstallPaths {
            support: base.join("support"),
            bin: base.join("cli-bin"),
            agt_home: base.join("agt"),
            home: base.join("person"),
            applications: vec![base.join("Applications")],
            explicit_agt_home: true,
            isolated: Some(Isolated {
                exclude: std::env::join_paths([&private, &base.join("person")]).unwrap(),
                home: private,
                config: base.join("config"),
            }),
        }
    }
    #[tokio::test]
    async fn test_build_refuses_derived_links_at_use_time() {
        let root = crate::terminal::tests::Scratch::new("install-derived");
        let base = root.0.canonicalize().unwrap();
        let paths = test_build_paths(&base);
        let outside = base.join("outside");
        fs::create_dir_all(&outside).unwrap();
        // A link planted after launch: the launcher would land in the outside folder.
        symlink(&outside, paths.support.join("bin")).unwrap();
        assert!(paths.own("bin/agt").unwrap_err().contains("is a symlink"));
        fs::remove_file(paths.support.join("bin")).unwrap();
        assert_eq!(paths.own("bin/agt").unwrap(), paths.support.join("bin/agt"));
        symlink(
            outside.join("install.json"),
            paths.support.join("install.json"),
        )
        .unwrap();
        assert!(save(&paths, &Record::default()).is_err());
        symlink(
            outside.join("install.lock"),
            paths.support.join("install.lock"),
        )
        .unwrap();
        assert!(paths.own("install.lock").is_err());
        // A test build's lock never follows a link, even when called directly.
        assert!(lock(&paths.support.join("install.lock"), true)
            .await
            .is_err());
        fs::create_dir_all(paths.agt_home.join("state")).unwrap();
        symlink(
            outside.join("admission.lock"),
            paths.agt_home.join("state/admission.lock"),
        )
        .unwrap();
        assert!(admission(&paths).await.is_err());
        // A child never starts in a private HOME that became a link.
        fs::remove_dir(paths.support.join("home")).unwrap();
        symlink(&outside, paths.support.join("home")).unwrap();
        assert!(output(
            &paths,
            Path::new("/usr/bin/true"),
            &[],
            Duration::from_secs(5)
        )
        .await
        .unwrap_err()
        .contains("is a symlink"));
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
        // The release joins as before.
        let mut release = paths.clone();
        release.isolated = None;
        assert_eq!(
            release.own("install.lock").unwrap(),
            paths.support.join("install.lock")
        );
    }
    #[tokio::test]
    async fn release_locks_open_through_a_symlink_as_before() {
        let root = crate::terminal::tests::Scratch::new("install-release-locks");
        let base = root.0.canonicalize().unwrap();
        let mut paths = test_build_paths(&base);
        paths.isolated = None;
        let elsewhere = base.join("elsewhere");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::create_dir_all(paths.agt_home.join("state")).unwrap();
        symlink(
            elsewhere.join("install.lock"),
            paths.support.join("install.lock"),
        )
        .unwrap();
        symlink(
            elsewhere.join("admission.lock"),
            paths.agt_home.join("state/admission.lock"),
        )
        .unwrap();
        let install = lock(
            &paths.own("install.lock").unwrap(),
            paths.isolated.is_some(),
        )
        .await
        .expect("a release install lock that is a symlink still locks");
        let admitted = admission(&paths)
            .await
            .expect("a release admission lock that is a symlink still locks");
        assert!(
            elsewhere.join("install.lock").is_file() && elsewhere.join("admission.lock").is_file()
        );
        drop((install, admitted));
    }
    #[test]
    fn release_paths_walk_past_a_dangling_link_and_test_builds_refuse_it() {
        use crate::{app_paths::TEST_IDENTIFIER, update::RELEASE_IDENTIFIER};
        let root = crate::terminal::tests::Scratch::new("install-dangling");
        let base = root.0.canonicalize().unwrap();
        let account = base.join("account");
        fs::create_dir_all(&account).unwrap();
        let resolve = |identifier: &str, home: &Path, args: &[OsString]| {
            AppPaths::resolve_with(
                identifier,
                args,
                |name| (name == "HOME").then(|| home.as_os_str().to_owned()),
                || Ok(account.clone()),
            )
        };
        // Each release folder is a link to a folder that doesn't exist.
        let home = base.join("home");
        let folders = [
            "Library/Application Support/Agents Router",
            ".local/bin",
            ".agents-router",
        ];
        for name in folders {
            let link = home.join(name);
            fs::create_dir_all(link.parent().unwrap()).unwrap();
            symlink(base.join("gone").join(name), &link).unwrap();
        }
        let release = resolve(RELEASE_IDENTIFIER, &home, &[]).unwrap();
        let paths = InstallPaths::from_app(&release).expect("the release launches past the links");
        // As before: the parent made physical, the link's own name kept.
        assert_eq!(
            [paths.support, paths.bin, paths.agt_home],
            folders.map(|name| home.join(name))
        );
        assert_eq!(paths.home, home);
        assert!(paths.isolated.is_none());
        // A test build refuses a dangling link among the release's folders it protects...
        let error = resolve(TEST_IDENTIFIER, &home, &[]).unwrap_err();
        assert!(error.contains("No such file"), "{error}");
        // ...and among its own, at launch and again when the installer resolves them.
        let clean = base.join("clean-home");
        let dangling = base.join("dangling");
        fs::create_dir_all(&clean).unwrap();
        symlink(base.join("nowhere"), &dangling).unwrap();
        let flag = OsString::from("--test-agt-home");
        let error = resolve(TEST_IDENTIFIER, &clean, &[flag.clone(), dangling.into()]).unwrap_err();
        assert!(error.contains("No such file"), "{error}");
        let test = resolve(TEST_IDENTIFIER, &clean, &[flag, base.join("agt").into()]).unwrap();
        assert!(InstallPaths::from_app(&test).is_ok());
        symlink(base.join("nowhere"), base.join("agt")).unwrap();
        let error = InstallPaths::from_app(&test).unwrap_err();
        assert!(error.contains("No such file"), "{error}");
    }
    #[test]
    fn default_applications_sit_under_the_resolved_home_and_explicit_ones_stay_as_given() {
        use crate::{app_paths::TEST_IDENTIFIER, update::RELEASE_IDENTIFIER};
        let root = crate::terminal::tests::Scratch::new("install-home-link");
        let base = root.0.canonicalize().unwrap();
        let account = base.join("account");
        let real = base.join("real home");
        fs::create_dir_all(&account).unwrap();
        fs::create_dir_all(&real).unwrap();
        let link = base.join("home link");
        symlink(&real, &link).unwrap();
        let explicit = link.join("Apps");
        let resolve = |identifier: &str, dirs: Option<&Path>| {
            let paths = AppPaths::resolve_with(
                identifier,
                &[],
                |name| match name {
                    "HOME" => Some(link.clone().into_os_string()),
                    "AGT_APPLICATIONS_DIRS" => dirs.map(|d| d.as_os_str().to_owned()),
                    _ => None,
                },
                || Ok(account.clone()),
            )
            .unwrap();
            InstallPaths::from_app(&paths).unwrap()
        };
        let defaults = [PathBuf::from("/Applications"), real.join("Applications")];
        // As before: the defaults come from HOME with its links resolved, so a
        // later change to the link can't move the destination...
        let release = resolve(RELEASE_IDENTIFIER, None);
        assert_eq!(release.home, real);
        assert_eq!(release.applications, defaults);
        assert_eq!(resolve(TEST_IDENTIFIER, None).applications, defaults);
        // ...and AGT_APPLICATIONS_DIRS is taken as given.
        for identifier in [RELEASE_IDENTIFIER, TEST_IDENTIFIER] {
            let paths = resolve(identifier, Some(&explicit));
            assert_eq!(paths.applications, [explicit.clone()]);
        }
    }
    #[test]
    fn test_launcher_keeps_the_callers_folder_and_excludes_the_persons_home() {
        let root = crate::terminal::tests::Scratch::new("launcher-cwd");
        let base = root.0.canonicalize().unwrap();
        let paths = test_build_paths(&base);
        let person = &paths.home;
        let runtime = paths.support.join("runtimes/1.0.0");
        fs::create_dir_all(runtime.join("bin")).unwrap();
        atomic_bytes(
            &runtime.join("bin/python3"),
            b"#!/bin/sh\nprintf '%s|%s|%s' \"$(pwd -P)\" \"$HOME\" \"$AGT_EXCLUDE_HOMES\"\n",
            0o755,
        )
        .unwrap();
        atomic_link(Path::new("runtimes/1.0.0"), &paths.support.join("current")).unwrap();
        atomic_bytes(&paths.launcher(), launcher(&paths).as_bytes(), 0o755).unwrap();
        let checkout = person.join("project/repo");
        let documents = person.join("Documents/inner");
        for dir in [
            person.join(".agt"),
            person.join(".git"),
            checkout.join(".git"),
            checkout.join("src"),
            documents.clone(),
            base.join("elsewhere"),
        ] {
            fs::create_dir_all(dir).unwrap();
        }
        let exclude = std::env::join_paths([paths.child_home(), person.as_path()]).unwrap();
        for cwd in [
            person.clone(),
            documents.clone(),
            checkout.join("src"),
            base.join("elsewhere"),
        ] {
            let out = std::process::Command::new(paths.launcher())
                .env_clear()
                .env("PATH", "/usr/bin:/bin")
                .env("HOME", person)
                .current_dir(&cwd)
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            assert_eq!(
                String::from_utf8(out.stdout).unwrap().trim_end(),
                format!(
                    "{}|{}|{}",
                    cwd.display(),
                    paths.child_home().display(),
                    exclude.to_string_lossy()
                ),
                "from {}",
                cwd.display()
            );
        }
        // The release launcher exports none of it.
        let mut release = paths.clone();
        release.isolated = None;
        let text = launcher(&release);
        assert!(!text.contains("AGT_EXCLUDE_HOMES") && !text.contains("export HOME"));
    }
    /// A runner process that a test stops with SIGTERM and then waits for.
    struct Runner(std::process::Child);
    impl Runner {
        fn stop(&mut self) {
            if matches!(self.0.try_wait(), Ok(None)) {
                unsafe { libc::kill(self.0.id() as libc::pid_t, libc::SIGTERM) };
            }
            self.0.wait().unwrap();
        }
    }
    impl Drop for Runner {
        fn drop(&mut self) {
            self.stop();
        }
    }
    /// The restart after an in-app update, half (b): the relaunched app's cleanup
    /// asks this checkout's real `agt usage --json`. A job whose runner outlived the
    /// old app keeps its pinned runtime; other old runtimes go, and the pinned one
    /// goes only after its runner exits.
    #[tokio::test]
    async fn restart_cleanup_keeps_a_live_jobs_pinned_runtime_and_removes_the_rest() {
        let root = crate::terminal::tests::Scratch::new("restart-cleanup");
        let base = root.0.canonicalize().unwrap();
        let paths = test_build_paths(&base);
        let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .unwrap();
        let python = std::process::Command::new("python3")
            .args(["-c", "import sys; print(sys.executable)"])
            .output()
            .unwrap();
        assert!(python.status.success(), "python3 is required");
        let python = PathBuf::from(String::from_utf8(python.stdout).unwrap().trim());
        let quote = |p: &Path| posix_quote(&p.to_string_lossy());
        for version in ["0.1.0", "0.2.0", "0.3.0"] {
            let runtime = paths.support.join("runtimes").join(version);
            for name in [
                "plugin/.claude-plugin/marketplace.json",
                "plugin/hooks/hooks.json",
            ] {
                let path = runtime.join(name);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(path, "{}").unwrap();
            }
            fs::write(
                runtime.join("plugin/.claude-plugin/plugin.json"),
                format!("{{\"version\":\"{version}\"}}"),
            )
            .unwrap();
            fs::write(runtime.join("agt-bundle.json"), serde_json::to_vec(&serde_json::json!({"schema_version":1,"source":"app","app_version":version,"runtime_version":version,"python_version":"3.13.0","plugin_path":"plugin"})).unwrap()).unwrap();
            // This checkout's agt, with a PATH that holds none of the person's CLIs.
            fs::create_dir_all(runtime.join("bin")).unwrap();
            atomic_bytes(
                &runtime.join("bin/python3"),
                format!(
                    "#!/bin/sh\n[ \"$1\" = -I ] && shift\nexport PATH=/usr/bin:/bin PYTHONPATH={}\nexec {} \"$@\"\n",
                    quote(&repo.join("src")),
                    quote(&python)
                )
                .as_bytes(),
                0o755,
            )
            .unwrap();
            manifest(&runtime).unwrap();
        }
        atomic_link(Path::new("runtimes/0.3.0"), &paths.support.join("current")).unwrap();
        atomic_bytes(&paths.launcher(), launcher(&paths).as_bytes(), 0o755).unwrap();
        // The job's record is an hour old, so only its live runner makes it count.
        let mut runner = Runner(
            std::process::Command::new("/bin/sleep")
                .arg("600")
                .spawn()
                .unwrap(),
        );
        let pinned = paths.support.join("runtimes/0.2.0");
        let job = paths.agt_home.join("jobs/survivor");
        fs::create_dir_all(&job).unwrap();
        let stale = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64() - 3600.0;
        let doc = serde_json::json!({"job_id":"survivor","state":"running","runtime":"codex","account":null,"updated_at":stale,"created_at":stale,"runner_pid":runner.0.id(),"install_runtime":pinned});
        for file in ["status.json", "spec.json"] {
            fs::write(job.join(file), serde_json::to_vec(&doc).unwrap()).unwrap();
        }
        fs::write(job.join("runner.pid"), runner.0.id().to_string()).unwrap();
        let report = usage(&paths).await.unwrap();
        assert_eq!(
            report["uninstall_blockers"][0]["job_id"], "survivor",
            "{report}"
        );
        assert_eq!(
            cleanup(&paths).await.unwrap(),
            [paths.support.join("runtimes/0.1.0")]
        );
        assert!(pinned.is_dir());
        assert_eq!(paths.current(), Some(paths.support.join("runtimes/0.3.0")));
        runner.stop();
        assert_eq!(cleanup(&paths).await.unwrap(), [pinned.clone()]);
        assert_eq!(paths.current(), Some(paths.support.join("runtimes/0.3.0")));
        assert!(paths.current().unwrap().is_dir());
    }
    #[test]
    fn versions_and_disk_error() {
        assert_eq!(version_cmp("0.17", "0.16.99"), Ordering::Greater);
        assert_eq!(version_cmp("0.17.0-rc1", "0.17.0"), Ordering::Less);
        assert!(install_error("os error 28", true, Some(1234)).contains("Free at least 1234 bytes"));
    }
}
