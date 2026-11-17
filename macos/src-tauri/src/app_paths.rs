//! The app's install, data and settings folders, resolved once at launch.
//!
//! The release (`RELEASE_IDENTIFIER`) keeps its environment-variable behaviour
//! and production defaults, and refuses the `--test-*` flags before touching the
//! filesystem. Every other bundle identifier is a test build: it gets its own
//! folders, takes overrides from a launch flag or an environment variable, and
//! refuses any folder that is, contains or sits inside a production one, under
//! both the launch HOME and the account's home from the user database.
use crate::update::RELEASE_IDENTIFIER;
use serde::Serialize;
use std::{
    ffi::{CStr, OsStr, OsString},
    fs,
    os::unix::ffi::OsStrExt,
    path::{Component, Path, PathBuf},
};

pub const TEST_IDENTIFIER: &str = "io.github.watcharagorn.agents-router.test";
pub const TEST_SUPPORT_NAME: &str = "Agents Router Test";
/// agt's list of folders that, like HOME, are never a workspace (levels.py).
pub const EXCLUDE_HOMES: &str = "AGT_EXCLUDE_HOMES";

/// Only the release identifier keeps the shared production folders.
pub fn is_test_build(identifier: &str) -> bool {
    identifier != RELEASE_IDENTIFIER
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Key {
    AppSupport,
    BinDir,
    AgtHome,
    ConfigDir,
}
impl Key {
    pub const ALL: [Key; 4] = [Key::AppSupport, Key::BinDir, Key::AgtHome, Key::ConfigDir];
    pub fn flag(self) -> &'static str {
        match self {
            Key::AppSupport => "--test-app-support",
            Key::BinDir => "--test-bin-dir",
            Key::AgtHome => "--test-agt-home",
            Key::ConfigDir => "--test-config-dir",
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Key::AppSupport => "app_support",
            Key::BinDir => "bin_dir",
            Key::AgtHome => "agt_home",
            Key::ConfigDir => "config_dir",
        }
    }
    pub fn variable(self) -> &'static str {
        match self {
            Key::AppSupport => "AGT_APP_SUPPORT",
            Key::BinDir => "AGT_BIN_DIR",
            Key::AgtHome => "AGT_HOME",
            Key::ConfigDir => "AGT_CONFIG_DIR",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    Flag,
    Env,
    Default,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub path: PathBuf,
    pub source: Source,
}

/// One row of the `test_build_paths` invoke.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Row {
    pub key: Key,
    pub path: PathBuf,
    /// The path with the person's home shown as `~`.
    pub display: String,
    pub source: Source,
    pub origin: Option<&'static str>,
}

/// An environment change for a child process: `Some` sets, `None` removes.
pub type ChildEnv = Vec<(&'static str, Option<OsString>)>;

#[derive(Clone, Debug)]
pub struct AppPaths {
    pub test: bool,
    /// The person's own HOME: Applications destinations, LaunchAgents, `~` display.
    pub home: PathBuf,
    pub support: Entry,
    pub bin: Entry,
    pub agt_home: Entry,
    pub config: Entry,
    /// Release only: pass AGT_HOME to children only when the person set it.
    pub explicit_agt_home: bool,
    /// `AGT_APPLICATIONS_DIRS` as given. `None`: the defaults, which the
    /// installer puts under the HOME it resolves, as before.
    pub applications: Option<Vec<PathBuf>>,
    /// Test builds: the account's home from the user database.
    pub account_home: Option<PathBuf>,
    /// Test builds: `AGT_EXCLUDE_HOMES` for every agt child, so agt never takes
    /// the private HOME, the launch HOME or the account's home as a workspace.
    pub exclude_homes: Option<OsString>,
}

/// The folders a test build must never reach, under the person's own HOME.
pub fn production(home: &Path) -> [PathBuf; 5] {
    [
        home.join(".local/bin"),
        home.join("Library/Application Support/Agents Router"),
        home.join(".agents-router"),
        home.join(".agt"),
        release_log_dir(home),
    ]
}

/// The release's diagnostic log folder (diag_log.rs). Test builds log inside
/// their app support folder instead.
pub fn release_log_dir(home: &Path) -> PathBuf {
    home.join("Library/Logs").join(RELEASE_IDENTIFIER)
}

/// The account's home folder from the user database (getpwuid_r). Unlike
/// HOME and CFFIXED_USER_HOME, a launch can't change it.
pub fn account_home() -> Result<PathBuf, String> {
    // SAFETY: getuid has no preconditions and never fails.
    let uid = unsafe { libc::getuid() };
    let mut size = 4096;
    loop {
        let mut buffer: Vec<libc::c_char> = vec![0; size];
        // SAFETY: passwd is plain data; getpwuid_r fills it, pointing into `buffer`.
        let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
        let mut found: *mut libc::passwd = std::ptr::null_mut();
        // SAFETY: every pointer is valid for the call, and `buffer.len()` is its size.
        let code = unsafe {
            libc::getpwuid_r(
                uid,
                &mut entry,
                buffer.as_mut_ptr(),
                buffer.len(),
                &mut found,
            )
        };
        if code == libc::ERANGE && size < 1 << 20 {
            size *= 2;
            continue;
        }
        if code != 0 {
            return Err(format!(
                "the user database lookup failed: {}",
                std::io::Error::from_raw_os_error(code)
            ));
        }
        if found.is_null() || entry.pw_dir.is_null() {
            return Err(format!("the user database has no entry for user {uid}"));
        }
        // SAFETY: pw_dir is a NUL-terminated string inside `buffer`, still alive here.
        let home = PathBuf::from(OsStr::from_bytes(
            unsafe { CStr::from_ptr(entry.pw_dir) }.to_bytes(),
        ));
        if !home.is_absolute() {
            return Err(format!(
                "the user database gives {} as your home folder; it must be absolute",
                home.display()
            ));
        }
        return Ok(home);
    }
}

/// The nearest existing ancestor's physical path plus the missing components.
/// A dangling symlink is refused: the folder created through it could be anywhere.
pub fn physical(path: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err(format!("{} must be absolute", path.display()));
    }
    let mut ancestor = path;
    let mut suffix = Vec::new();
    while fs::symlink_metadata(ancestor).is_err() {
        suffix.push(ancestor.file_name().ok_or("Invalid path")?.to_owned());
        ancestor = ancestor.parent().ok_or("Invalid path")?;
    }
    let mut resolved = ancestor
        .canonicalize()
        .map_err(|e| format!("{}: {e}", ancestor.display()))?;
    for component in suffix.into_iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

/// What a test build creates or replaces below its app support folder.
pub const SUPPORT_DERIVED: [&str; 9] = [
    "home",
    "bin",
    "bin/agt",
    "handoffs",
    "runtimes",
    "updated",
    "plugins",
    "install.json",
    "install.lock",
];
/// What the app watches or locks below the agt home, and the settings file it reads.
const AGT_HOME_DERIVED: [&str; 3] = ["state", "state/admission.lock", "jobs"];
const CONFIG_DERIVED: [&str; 1] = ["settings.toml"];

/// `root/relative` when no part below `root` is a symlink. `root` is physical,
/// so the path can't lead out of it or into a production folder. lstat never
/// follows a link, and nothing below a missing part can exist.
pub fn inside(root: &Path, relative: &Path) -> Result<PathBuf, String> {
    let mut path = root.to_path_buf();
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return Err(format!("Invalid folder below {}", root.display()));
        };
        path.push(name);
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.file_type().is_symlink() => {
                return Err(format!(
                    "{} is a symlink; a test build keeps everything inside {}. Remove the link, then try again.",
                    path.display(),
                    root.display()
                ))
            }
            Ok(_) => (),
            Err(_) => break,
        }
    }
    Ok(root.join(relative))
}

/// `current` is the one symlink the installer makes: `runtimes/<version>`,
/// relative, onto a runtime folder that is itself no symlink. A dangling one is
/// fine; the next install replaces it.
pub fn current_link(support: &Path) -> Result<(), String> {
    let path = support.join("current");
    let Ok(meta) = fs::symlink_metadata(&path) else {
        return Ok(());
    };
    let refuse = || {
        format!(
            "{} must be the installer's link to runtimes/<version>. Remove it, then try again.",
            path.display()
        )
    };
    if !meta.file_type().is_symlink() {
        return Err(refuse());
    }
    let target = fs::read_link(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    match target.components().collect::<Vec<_>>().as_slice() {
        [Component::Normal(first), Component::Normal(_)] if *first == "runtimes" => {
            inside(support, &target).map(|_| ())
        }
        _ => Err(refuse()),
    }
}

/// Every folder and file a test build derives from its four folders.
pub fn check_derived(support: &Path, agt_home: &Path, config: &Path) -> Result<(), String> {
    for (root, names) in [
        (support, &SUPPORT_DERIVED[..]),
        (agt_home, &AGT_HOME_DERIVED[..]),
        (config, &CONFIG_DERIVED[..]),
    ] {
        for name in names {
            inside(root, Path::new(name))?;
        }
    }
    current_link(support)?;
    // A runtime folder reached through a link would run code from elsewhere.
    if let Ok(entries) = fs::read_dir(support.join("runtimes")) {
        for entry in entries.flatten() {
            inside(support, &Path::new("runtimes").join(entry.file_name()))?;
        }
    }
    Ok(())
}

fn lowered(path: &Path) -> PathBuf {
    // APFS is usually case-insensitive: ~/.Agents-Router is the production home.
    PathBuf::from(path.to_string_lossy().to_lowercase())
}
fn overlaps(a: &Path, b: &Path) -> bool {
    let (a, b) = (lowered(a), lowered(b));
    a.starts_with(&b) || b.starts_with(&a)
}

fn flag_value(key: Key, args: &[OsString]) -> Result<Option<OsString>, String> {
    let flag = key.flag();
    let mut found: Option<OsString> = None;
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].to_string_lossy();
        let value = if arg == flag {
            index += 1;
            Some(
                args.get(index)
                    .filter(|v| !v.to_string_lossy().starts_with("--"))
                    .cloned()
                    .ok_or(format!("{flag} needs a folder"))?,
            )
        } else {
            arg.strip_prefix(flag)
                .and_then(|rest| rest.strip_prefix('='))
                .map(OsString::from)
        };
        if let Some(value) = value {
            if found.is_some() {
                return Err(format!("{flag} is given twice"));
            }
            found = Some(value);
        }
        index += 1;
    }
    Ok(found)
}

fn checked(value: &OsStr, origin: &str) -> Result<PathBuf, String> {
    let text = value.to_string_lossy();
    let path = PathBuf::from(value);
    if text.is_empty() {
        return Err(format!("{origin} is empty; give an absolute folder"));
    }
    if text.contains(['\0', '\n', '\r']) {
        return Err(format!("{origin} has a control character"));
    }
    if !path.is_absolute() {
        return Err(format!("{origin} must be an absolute folder: {text}"));
    }
    if path
        .components()
        .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
    {
        return Err(format!("{origin} must not contain . or ..: {text}"));
    }
    Ok(path)
}

impl AppPaths {
    /// Reads this process's arguments and environment.
    pub fn from_process(identifier: &str) -> Result<Self, String> {
        let args: Vec<OsString> = std::env::args_os().skip(1).collect();
        Self::resolve(identifier, &args, |name| std::env::var_os(name))
    }
    /// The release resolution without launch arguments: developer checks.
    pub fn from_env() -> Result<Self, String> {
        Self::resolve(RELEASE_IDENTIFIER, &[], |name| std::env::var_os(name))
    }

    pub fn resolve(
        identifier: &str,
        args: &[OsString],
        env: impl Fn(&str) -> Option<OsString>,
    ) -> Result<Self, String> {
        Self::resolve_with(identifier, args, env, account_home)
    }

    /// `resolve` with the account-home lookup given; only a test build calls it.
    pub fn resolve_with(
        identifier: &str,
        args: &[OsString],
        env: impl Fn(&str) -> Option<OsString>,
        account: impl FnOnce() -> Result<PathBuf, String>,
    ) -> Result<Self, String> {
        let test = is_test_build(identifier);
        if !test {
            // Before any filesystem access: the release has no test overrides.
            if let Some(arg) = args
                .iter()
                .map(|a| a.to_string_lossy())
                .find(|a| a.starts_with("--test-"))
            {
                return Err(format!(
                    "{} works only in {TEST_SUPPORT_NAME}.app",
                    arg.split('=').next().unwrap_or(&arg)
                ));
            }
        } else if let Some(arg) = args.iter().map(|a| a.to_string_lossy()).find(|a| {
            a.starts_with("--test-")
                && !Key::ALL
                    .iter()
                    .any(|k| a.as_ref() == k.flag() || a.starts_with(&format!("{}=", k.flag())))
        }) {
            return Err(format!("Unknown test-build flag {arg}"));
        }
        let home = env("HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .ok_or("HOME must be absolute")?;
        let applications = match env("AGT_APPLICATIONS_DIRS") {
            Some(paths) => {
                let dirs: Vec<PathBuf> = std::env::split_paths(&paths).collect();
                if dirs.is_empty() || dirs.iter().any(|p| !p.is_absolute()) {
                    return Err("Applications destinations must be absolute".into());
                }
                Some(dirs)
            }
            None => None,
        };
        if test {
            let account = account().map_err(|e| {
                format!("Couldn't find your account's home folder, so the release's folders can't be protected: {e}")
            })?;
            if !account.is_absolute() {
                return Err(format!(
                    "Your account's home folder {} must be absolute",
                    account.display()
                ));
            }
            Self::resolve_test(home, account, applications, args, env)
        } else {
            Self::resolve_release(home, applications, env)
        }
    }

    fn resolve_release(
        home: PathBuf,
        applications: Option<Vec<PathBuf>>,
        env: impl Fn(&str) -> Option<OsString>,
    ) -> Result<Self, String> {
        let entry = |key: Key, default: PathBuf| match env(key.variable()) {
            Some(value) => Entry {
                path: PathBuf::from(value),
                source: Source::Env,
            },
            None => Entry {
                path: default,
                source: Source::Default,
            },
        };
        let support = entry(
            Key::AppSupport,
            home.join("Library/Application Support/Agents Router"),
        );
        let bin = entry(Key::BinDir, home.join(".local/bin"));
        let agt_home = entry(Key::AgtHome, home.join(".agents-router"));
        let explicit_agt_home = agt_home.source == Source::Env;
        // agt's own order: AGT_CONFIG_DIR, then an explicit AGT_HOME, then ~/.agt.
        let config = entry(
            Key::ConfigDir,
            if explicit_agt_home {
                agt_home.path.clone()
            } else {
                home.join(".agt")
            },
        );
        if !support.path.is_absolute() {
            return Err("AGT_APP_SUPPORT must be absolute".into());
        }
        if !agt_home.path.is_absolute() || !config.path.is_absolute() {
            return Err("agt home and config paths must be absolute".into());
        }
        if !bin.path.is_absolute() {
            return Err("AGT_BIN_DIR must be absolute".into());
        }
        Ok(Self {
            test: false,
            home,
            support,
            bin,
            agt_home,
            config,
            explicit_agt_home,
            applications,
            account_home: None,
            exclude_homes: None,
        })
    }

    fn resolve_test(
        home: PathBuf,
        account: PathBuf,
        applications: Option<Vec<PathBuf>>,
        args: &[OsString],
        env: impl Fn(&str) -> Option<OsString>,
    ) -> Result<Self, String> {
        let chosen = |key: Key| -> Result<Option<Entry>, String> {
            if let Some(value) = flag_value(key, args)? {
                let path = checked(&value, key.flag())?;
                return Ok(Some(Entry {
                    path,
                    source: Source::Flag,
                }));
            }
            match env(key.variable()) {
                Some(value) => Ok(Some(Entry {
                    path: checked(&value, key.variable())?,
                    source: Source::Env,
                })),
                None => Ok(None),
            }
        };
        let default = |path: PathBuf| Entry {
            path,
            source: Source::Default,
        };
        let support = chosen(Key::AppSupport)?.unwrap_or_else(|| {
            default(
                home.join("Library/Application Support")
                    .join(TEST_SUPPORT_NAME),
            )
        });
        let bin = chosen(Key::BinDir)?.unwrap_or_else(|| default(support.path.join("cli-bin")));
        let agt_home =
            chosen(Key::AgtHome)?.unwrap_or_else(|| default(support.path.join("agt-home")));
        let config = chosen(Key::ConfigDir)?.unwrap_or_else(|| default(agt_home.path.clone()));
        // The launch HOME can be changed; the account's home can't.
        let protected: Vec<PathBuf> = production(&home)
            .iter()
            .chain(production(&account).iter())
            .map(|p| physical(p))
            .collect::<Result<_, _>>()?;
        let mut resolved = Vec::new();
        for (key, entry) in [
            (Key::AppSupport, support),
            (Key::BinDir, bin),
            (Key::AgtHome, agt_home),
            (Key::ConfigDir, config),
        ] {
            let path = physical(&entry.path)?;
            let named = match entry.source {
                Source::Flag => key.flag().to_string(),
                Source::Env => key.variable().to_string(),
                Source::Default => format!("The default {}", label(key)),
            };
            if let Some(hit) = protected.iter().find(|p| overlaps(&path, p)) {
                return Err(format!(
                    "{named} {} overlaps the release's {}; choose a folder outside it",
                    path.display(),
                    hit.display()
                ));
            }
            resolved.push(Entry {
                path,
                source: entry.source,
            });
        }
        let [support, bin, agt_home, config]: [Entry; 4] =
            resolved.try_into().expect("four test paths");
        if lowered(&bin.path) == lowered(&support.path.join("bin")) {
            return Err(format!(
                "The bin folder can't be {}: the stable launcher lives there",
                bin.path.display()
            ));
        }
        // The roots are physical and clear of production; so is everything
        // below them while no derived part is a symlink.
        check_derived(&support.path, &agt_home.path, &config.path)?;
        let mut homes = vec![support.path.join("home")];
        for folder in [&home, &account] {
            let folder = physical(folder)?;
            if !homes.contains(&folder) {
                homes.push(folder);
            }
        }
        let exclude_homes = std::env::join_paths(&homes).map_err(|_| {
            format!(
                "A home folder or the app support folder contains ':': {}",
                homes
                    .iter()
                    .map(|p| p.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })?;
        Ok(Self {
            test: true,
            home,
            support,
            bin,
            agt_home,
            config,
            explicit_agt_home: true,
            applications,
            account_home: Some(account),
            exclude_homes: Some(exclude_homes),
        })
    }

    /// The stable launcher every child runs.
    pub fn launcher(&self) -> PathBuf {
        self.support.path.join("bin/agt")
    }
    pub fn handoffs(&self) -> PathBuf {
        self.support.path.join("handoffs")
    }
    pub fn settings_file(&self) -> PathBuf {
        self.config.path.join("settings.toml")
    }
    /// HOME for agt children: private in a test build, so provider discovery
    /// can't reach the person's real Claude, Codex or Gemini setup.
    pub fn child_home(&self) -> PathBuf {
        if self.test {
            self.support.path.join("home")
        } else {
            self.home.clone()
        }
    }
    /// Creates the private HOME of a test build. Nothing in the release.
    pub fn prepare(&self) -> Result<(), String> {
        if !self.test {
            return Ok(());
        }
        use std::os::unix::fs::DirBuilderExt;
        let support = &self.support.path;
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(support)
            .map_err(|e| format!("{}: {e}", support.display()))?;
        // mkdir never follows an existing link; lstat then proves a real folder.
        let home = inside(support, Path::new("home"))?;
        match fs::DirBuilder::new().mode(0o700).create(&home) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                match fs::symlink_metadata(&home) {
                    Ok(meta) if meta.is_dir() => Ok(()),
                    _ => Err(format!(
                        "{} must be a folder, not a link or file",
                        home.display()
                    )),
                }
            }
            Err(e) => Err(format!("{}: {e}", home.display())),
        }
    }

    /// The environment every agt child gets. The release passes AGT_HOME only
    /// when the person set it, as before; a test build passes every folder.
    pub fn child_env(&self) -> ChildEnv {
        let mut env: ChildEnv = vec![("PYTHONPATH", None), ("PYTHONHOME", None)];
        if !self.test {
            env.push((
                "AGT_HOME",
                self.explicit_agt_home
                    .then(|| self.agt_home.path.clone().into_os_string()),
            ));
            return env;
        }
        let home = self.child_home();
        env.extend([
            ("AGT_APP_SUPPORT", Some(self.support.path.clone().into())),
            ("AGT_BIN_DIR", Some(self.bin.path.clone().into())),
            ("AGT_HOME", Some(self.agt_home.path.clone().into())),
            ("AGT_CONFIG_DIR", Some(self.config.path.clone().into())),
            ("HOME", Some(home.clone().into())),
            ("CFFIXED_USER_HOME", Some(home.into())),
            // With HOME private, agt would take the person's home as a workspace.
            (EXCLUDE_HOMES, self.exclude_homes.clone()),
            // Providers then follow the private HOME instead of the person's own folders.
            ("CLAUDE_CONFIG_DIR", None),
            ("CODEX_HOME", None),
            ("GEMINI_CLI_HOME", None),
        ]);
        env
    }

    /// Launch arguments that carry the chosen (non-default) test folders
    /// through Move to Applications, restart and the login item.
    pub fn relaunch_args(&self) -> Vec<String> {
        if !self.test {
            return Vec::new();
        }
        self.entries()
            .into_iter()
            .filter(|(_, e)| e.source != Source::Default)
            .flat_map(|(key, e)| {
                [
                    key.flag().to_string(),
                    e.path.to_string_lossy().into_owned(),
                ]
            })
            .collect()
    }

    fn entries(&self) -> [(Key, &Entry); 4] {
        [
            (Key::AppSupport, &self.support),
            (Key::BinDir, &self.bin),
            (Key::AgtHome, &self.agt_home),
            (Key::ConfigDir, &self.config),
        ]
    }

    /// Settings → General's Test build rows; `None` in the release.
    pub fn rows(&self) -> Option<Vec<Row>> {
        self.test.then(|| {
            self.entries()
                .into_iter()
                .map(|(key, e)| Row {
                    key,
                    path: e.path.clone(),
                    display: tilde(&e.path, &self.home),
                    source: e.source,
                    origin: match e.source {
                        Source::Flag => Some(key.flag()),
                        Source::Env => Some(key.variable()),
                        Source::Default => None,
                    },
                })
                .collect()
        })
    }

    /// One startup log line naming each folder and where it came from.
    pub fn describe(&self) -> String {
        let mut line = String::from("test build paths");
        for (key, e) in self.entries() {
            let source = match e.source {
                Source::Flag => key.flag(),
                Source::Env => key.variable(),
                Source::Default => "default",
            };
            line.push_str(&format!(" {}={} ({source})", key.name(), e.path.display()));
        }
        line.push_str(&format!(" home={}", self.child_home().display()));
        if let Some(account) = &self.account_home {
            line.push_str(&format!(" account_home={}", account.display()));
        }
        line
    }
}

fn label(key: Key) -> &'static str {
    match key {
        Key::AppSupport => "app support folder",
        Key::BinDir => "bin folder",
        Key::AgtHome => "agt home",
        Key::ConfigDir => "settings folder",
    }
}

/// Shows the person's home as `~`.
pub fn tilde(path: &Path, home: &Path) -> String {
    let home = physical(home).unwrap_or_else(|_| home.to_path_buf());
    match path.strip_prefix(&home) {
        Ok(rest) if rest.as_os_str().is_empty() => "~".into(),
        Ok(rest) => format!("~/{}", rest.display()),
        Err(_) => path.display().to_string(),
    }
}

/// Rebuilds launch arguments for a relaunch: drops the old `--test-*` flags and
/// their values, keeps the rest (for example `--self-test-update <root>`), then
/// appends the effective selections.
pub fn relaunch_argv(args: &[OsString], relaunch: &[String]) -> Vec<OsString> {
    let mut kept = Vec::new();
    let mut skip = false;
    for arg in args {
        if skip {
            skip = false;
            continue;
        }
        let text = arg.to_string_lossy();
        if Key::ALL.iter().any(|k| text == k.flag()) {
            skip = true;
            continue;
        }
        if text.starts_with("--test-") {
            continue;
        }
        kept.push(arg.clone());
    }
    kept.extend(relaunch.iter().map(OsString::from));
    kept
}

/// auto-launch writes each argument into the plist's `<string>` verbatim.
pub fn plist_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::terminal::tests::Scratch;
    use std::collections::HashMap;

    struct World {
        _root: Scratch,
        home: PathBuf,
        /// The account's home from the user database, apart from the launch HOME.
        account: PathBuf,
        vars: HashMap<String, OsString>,
    }
    impl World {
        fn new(label: &str) -> Self {
            let root = Scratch::new(label);
            let base = root.0.canonicalize().unwrap();
            let home = base.join("home");
            let account = base.join("account");
            fs::create_dir_all(&home).unwrap();
            fs::create_dir_all(&account).unwrap();
            let mut vars = HashMap::new();
            vars.insert("HOME".into(), home.clone().into_os_string());
            Self {
                _root: root,
                home,
                account,
                vars,
            }
        }
        fn base(&self) -> PathBuf {
            self.home.parent().unwrap().to_path_buf()
        }
        fn set(&mut self, name: &str, value: impl Into<OsString>) -> &mut Self {
            self.vars.insert(name.into(), value.into());
            self
        }
        fn resolve(&self, identifier: &str, args: &[&str]) -> Result<AppPaths, String> {
            let args: Vec<OsString> = args.iter().map(OsString::from).collect();
            AppPaths::resolve_with(
                identifier,
                &args,
                |name| self.vars.get(name).cloned(),
                || Ok(self.account.clone()),
            )
        }
        fn test(&self, args: &[&str]) -> Result<AppPaths, String> {
            self.resolve(TEST_IDENTIFIER, args)
        }
    }
    fn s(path: &Path) -> String {
        path.to_string_lossy().into_owned()
    }

    #[test]
    fn identifiers_decide_test_and_release() {
        assert!(!is_test_build(RELEASE_IDENTIFIER));
        for id in [
            TEST_IDENTIFIER,
            "io.github.watcharagorn.agents-router.test.update-42",
            "com.tauri.dev",
            "",
        ] {
            assert!(is_test_build(id), "{id}");
        }
        assert_eq!(TEST_IDENTIFIER, format!("{RELEASE_IDENTIFIER}.test"));
    }

    #[test]
    fn test_defaults_are_isolated_from_production() {
        let w = World::new("paths-defaults");
        let p = w.test(&[]).unwrap();
        let support = w
            .home
            .join("Library/Application Support/Agents Router Test");
        assert!(p.test);
        assert_eq!(
            p.support,
            Entry {
                path: support.clone(),
                source: Source::Default
            }
        );
        assert_eq!(p.bin.path, support.join("cli-bin"));
        assert_eq!(p.agt_home.path, support.join("agt-home"));
        assert_eq!(p.config.path, support.join("agt-home"));
        assert_eq!(p.launcher(), support.join("bin/agt"));
        assert_eq!(p.settings_file(), support.join("agt-home/settings.toml"));
        assert_eq!(p.child_home(), support.join("home"));
        assert!(p.relaunch_args().is_empty());
        for path in [
            &p.support.path,
            &p.bin.path,
            &p.agt_home.path,
            &p.config.path,
        ] {
            assert!(production(&w.home).iter().all(|d| !overlaps(path, d)));
        }
        // Resolving writes nothing; prepare() makes only the private HOME.
        assert!(!w.home.join("Library").exists());
        p.prepare().unwrap();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(p.child_home()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert!(!w.home.join(".agents-router").exists() && !w.home.join(".local").exists());
    }

    #[test]
    fn flag_beats_env_beats_default_per_folder() {
        let mut w = World::new("paths-precedence");
        let base = w.base();
        w.set("AGT_APP_SUPPORT", base.join("env-support"))
            .set("AGT_HOME", base.join("env-home"));
        let p = w
            .test(&[
                "--test-app-support",
                &s(&base.join("flag-support")),
                "--self-test-wake",
            ])
            .unwrap();
        assert_eq!(
            p.support,
            Entry {
                path: base.join("flag-support"),
                source: Source::Flag
            }
        );
        assert_eq!(
            p.agt_home,
            Entry {
                path: base.join("env-home"),
                source: Source::Env
            }
        );
        // Partial overrides: the rest follow the effective folders.
        assert_eq!(
            p.bin,
            Entry {
                path: base.join("flag-support/cli-bin"),
                source: Source::Default
            }
        );
        assert_eq!(
            p.config,
            Entry {
                path: base.join("env-home"),
                source: Source::Default
            }
        );
        assert_eq!(p.child_home(), base.join("flag-support/home"));
        let eq = w
            .test(&[&format!("--test-config-dir={}", s(&base.join("cfg")))])
            .unwrap();
        assert_eq!(
            eq.config,
            Entry {
                path: base.join("cfg"),
                source: Source::Flag
            }
        );
        assert_eq!(eq.support.source, Source::Env);
        let rows = p.rows().unwrap();
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[0].origin, Some("--test-app-support"));
        assert_eq!(rows[2].origin, Some("AGT_HOME"));
        assert_eq!(rows[1].origin, None);
        let json = serde_json::to_value(&rows).unwrap();
        assert_eq!(json[0]["key"], "app_support");
        assert_eq!(json[0]["source"], "flag");
        assert_eq!(json[2]["source"], "env");
        assert_eq!(json[3]["key"], "config_dir");
        assert_eq!(json[3]["source"], "default");
        assert!(json[3]["origin"].is_null());
        // Only chosen folders travel to the next launch, env selections included.
        assert_eq!(
            p.relaunch_args(),
            vec![
                "--test-app-support".to_string(),
                s(&base.join("flag-support")),
                "--test-agt-home".to_string(),
                s(&base.join("env-home")),
            ]
        );
        assert!(p.describe().contains("app_support="));
        assert!(p.describe().contains("(--test-app-support)"));
        assert!(p.describe().contains("(AGT_HOME)"));
    }

    #[test]
    fn invalid_overrides_are_refused() {
        let mut w = World::new("paths-invalid");
        for (args, needle) in [
            (vec!["--test-agt-home"], "needs a folder"),
            (
                vec!["--test-agt-home", "--test-bin-dir", "/tmp/x"],
                "needs a folder",
            ),
            (
                vec!["--test-agt-home", "relative/home"],
                "must be an absolute folder",
            ),
            (vec!["--test-agt-home="], "is empty"),
            (
                vec!["--test-agt-home", "/tmp/a/../b"],
                "must not contain . or ..",
            ),
            (
                vec!["--test-agt-home=/tmp/a", "--test-agt-home", "/tmp/b"],
                "given twice",
            ),
            (vec!["--test-agt-hom", "/tmp/a"], "Unknown test-build flag"),
            (vec!["--test-bin-dir", "/tmp/a\nb"], "control character"),
        ] {
            let error = w.test(&args).unwrap_err();
            assert!(error.contains(needle), "{args:?}: {error}");
        }
        w.set("AGT_HOME", "");
        assert!(w.test(&[]).unwrap_err().contains("AGT_HOME is empty"));
        w.set("AGT_HOME", "relative");
        assert!(w
            .test(&[])
            .unwrap_err()
            .contains("AGT_HOME must be an absolute"));
        // A valid flag replaces the bad environment value.
        let base = w.base();
        assert!(w.test(&["--test-agt-home", &s(&base.join("ok"))]).is_ok());
    }

    #[test]
    fn production_folders_and_their_aliases_are_refused() {
        let mut w = World::new("paths-overlap");
        let home = w.home.clone();
        let base = w.base();
        fs::create_dir_all(home.join(".agents-router")).unwrap();
        fs::create_dir_all(home.join("Library/Application Support/Agents Router")).unwrap();
        std::os::unix::fs::symlink(home.join(".agents-router"), base.join("alias")).unwrap();
        std::os::unix::fs::symlink(base.join("nowhere"), base.join("dangling")).unwrap();
        std::os::unix::fs::symlink(base.join("elsewhere"), base.join("elsewhere-link")).unwrap();
        fs::create_dir_all(base.join("elsewhere")).unwrap();
        for (flag, path) in [
            ("--test-agt-home", home.join(".agents-router")),
            ("--test-agt-home", home.join(".agents-router/sub")),
            ("--test-agt-home", home.join(".Agents-Router")),
            ("--test-agt-home", base.join("alias")),
            ("--test-agt-home", base.join("alias/jobs")),
            ("--test-config-dir", home.join(".agt")),
            ("--test-bin-dir", home.join(".local/bin")),
            ("--test-bin-dir", home.join(".local")),
            (
                "--test-app-support",
                home.join("Library/Application Support/Agents Router"),
            ),
            (
                "--test-app-support",
                home.join("Library/Application Support"),
            ),
            ("--test-app-support", home.clone()),
            ("--test-app-support", PathBuf::from("/")),
        ] {
            let error = w.test(&[flag, &s(&path)]).unwrap_err();
            assert!(
                error.contains("overlaps the release's"),
                "{flag} {}: {error}",
                path.display()
            );
        }
        let error = w
            .test(&["--test-agt-home", &s(&base.join("dangling"))])
            .unwrap_err();
        assert!(
            error.contains("dangling") || error.contains("No such file"),
            "{error}"
        );
        // A symlink to a safe folder resolves to its physical path.
        let p = w
            .test(&["--test-agt-home", &s(&base.join("elsewhere-link/home"))])
            .unwrap();
        assert_eq!(p.agt_home.path, base.join("elsewhere/home"));
        // "Agents Router Test" is a sibling of the production folder, not inside it.
        assert!(w.test(&[]).is_ok());
        w.set(
            "AGT_APP_SUPPORT",
            home.join("Library/Application Support/Agents Router"),
        );
        assert!(w.test(&[]).unwrap_err().contains("AGT_APP_SUPPORT"));
        let error = w
            .test(&[
                "--test-app-support",
                &s(&base.join("s")),
                "--test-bin-dir",
                &s(&base.join("s/bin")),
            ])
            .unwrap_err();
        assert!(error.contains("stable launcher"), "{error}");
    }

    #[test]
    fn production_folders_under_the_account_home_are_refused_too() {
        let mut w = World::new("paths-account");
        let account = w.account.clone();
        let base = w.base();
        for folder in production(&account) {
            fs::create_dir_all(folder).unwrap();
        }
        std::os::unix::fs::symlink(account.join(".agents-router"), base.join("alias")).unwrap();
        std::os::unix::fs::symlink(&account, base.join("account-link")).unwrap();
        for (flag, path) in [
            ("--test-agt-home", account.join(".agents-router")),
            ("--test-agt-home", account.join(".agents-router/jobs")),
            ("--test-agt-home", account.join(".AGENTS-ROUTER")),
            ("--test-agt-home", base.join("alias")),
            ("--test-config-dir", account.join(".agt")),
            ("--test-config-dir", base.join("account-link/.agt")),
            ("--test-bin-dir", account.join(".local/bin")),
            ("--test-bin-dir", account.join(".local")),
            (
                "--test-app-support",
                account.join("Library/Application Support/Agents Router"),
            ),
            ("--test-app-support", account.clone()),
            ("--test-app-support", base.join("account-link")),
        ] {
            let error = w.test(&[flag, &s(&path)]).unwrap_err();
            assert!(
                error.contains("overlaps the release's")
                    && error.contains(&*account.to_string_lossy()),
                "{flag} {}: {error}",
                path.display()
            );
        }
        w.set("AGT_HOME", account.join(".agents-router"));
        assert!(w.test(&[]).unwrap_err().contains("AGT_HOME"));
        w.vars.remove("AGT_HOME");
        // A launch HOME inside the account's own Application Support support folder.
        w.set(
            "HOME",
            account.join("Library/Application Support/Agents Router"),
        );
        assert!(w.test(&[]).unwrap_err().contains("overlaps the release's"));
        // Scratch folders, and folders elsewhere under the account's home, are fine.
        w.set("HOME", w.home.clone());
        let p = w
            .test(&[
                "--test-app-support",
                &s(&base.join("scratch/support")),
                "--test-agt-home",
                &s(&account.join("scratch agt home")),
            ])
            .unwrap();
        assert_eq!(p.agt_home.path, account.join("scratch agt home"));
        assert!(w.test(&[]).is_ok());
        // One home for both: listed once.
        w.account = w.home.clone();
        let p = w.test(&[]).unwrap();
        assert_eq!(
            p.exclude_homes,
            Some(std::env::join_paths([p.child_home(), w.home.clone()]).unwrap())
        );
    }

    #[test]
    fn the_release_log_folder_is_a_production_folder() {
        let mut w = World::new("paths-logs");
        let (home, account) = (w.home.clone(), w.account.clone());
        let logs = home.join("Library/Logs/io.github.watcharagorn.agents-router");
        assert_eq!(release_log_dir(&home), logs);
        assert!(production(&home).contains(&logs));
        for root in [&home, &account] {
            let logs = release_log_dir(root);
            fs::create_dir_all(&logs).unwrap();
            for path in [
                logs.clone(),
                logs.join("support"),
                root.join("Library/Logs"),
            ] {
                let error = w.test(&["--test-app-support", &s(&path)]).unwrap_err();
                assert!(
                    error.contains("overlaps the release's") && error.contains("Library/Logs"),
                    "{}: {error}",
                    path.display()
                );
            }
        }
        w.set("AGT_APP_SUPPORT", release_log_dir(&account).join("support"));
        assert!(w.test(&[]).unwrap_err().contains("AGT_APP_SUPPORT"));
        w.vars.remove("AGT_APP_SUPPORT");
        // The test build's own logs folder is a sibling, under its app support folder.
        assert!(w
            .test(&[
                "--test-app-support",
                &s(&home.join("Library/Logs/Agents Router Test"))
            ])
            .is_ok());
    }

    #[test]
    fn a_failed_account_home_lookup_stops_a_test_build_only() {
        let w = World::new("paths-lookup");
        let env = |name: &str| w.vars.get(name).cloned();
        let error = AppPaths::resolve_with(TEST_IDENTIFIER, &[], env, || Err("no entry".into()))
            .unwrap_err();
        assert!(
            error.contains("Couldn't find your account's home folder")
                && error.contains("no entry"),
            "{error}"
        );
        let error = AppPaths::resolve_with(TEST_IDENTIFIER, &[], env, || Ok("relative".into()))
            .unwrap_err();
        assert!(error.contains("must be absolute"), "{error}");
        // The release never looks the account up.
        let looked = std::cell::Cell::new(false);
        let release = AppPaths::resolve_with(RELEASE_IDENTIFIER, &[], env, || {
            looked.set(true);
            Err("unused".into())
        })
        .unwrap();
        assert!(!release.test && !looked.get());
        // A launch argument the release refuses is refused before any lookup.
        let args = [OsString::from("--test-agt-home=/tmp/x")];
        assert!(AppPaths::resolve_with(RELEASE_IDENTIFIER, &args, env, || {
            looked.set(true);
            Err("unused".into())
        })
        .is_err());
        assert!(!looked.get());
    }

    /// The child half of the test below: prints the lookup under a changed HOME.
    const PROBE: &str = "AGT_ACCOUNT_HOME_PROBE";

    #[test]
    fn account_home_comes_from_the_user_database_not_the_environment() {
        if let Some(out) = std::env::var_os(PROBE) {
            fs::write(out, account_home().unwrap().as_os_str().as_bytes()).unwrap();
            return;
        }
        let ours = account_home().unwrap();
        assert!(ours.is_absolute());
        let root = Scratch::new("account-home");
        let fake = root.0.canonicalize().unwrap().join("fake home");
        fs::create_dir(&fake).unwrap();
        let probe = root.0.join("probe");
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "app_paths::tests::account_home_comes_from_the_user_database_not_the_environment",
                "--test-threads=1",
                "--quiet",
            ])
            .env(PROBE, &probe)
            .env("HOME", &fake)
            .env("CFFIXED_USER_HOME", &fake)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
        let seen = PathBuf::from(OsStr::from_bytes(&fs::read(&probe).unwrap()));
        assert_eq!(seen, ours);
        assert_ne!(seen, fake);
    }

    #[test]
    fn derived_symlinks_to_production_or_outside_are_refused() {
        use std::os::unix::fs::symlink;
        let w = World::new("paths-derived");
        let home = w.home.clone();
        let base = w.base();
        let outside = base.join("outside");
        fs::create_dir_all(&outside).unwrap();
        // The fixture HOME's own production folders stand in for the person's.
        let production = production(&home);
        for folder in &production {
            fs::create_dir_all(folder).unwrap();
        }
        let support = base.join("support");
        let agt_home = base.join("agt-home");
        let config = base.join("config");
        let args = [
            "--test-app-support".to_string(),
            s(&support),
            "--test-agt-home".to_string(),
            s(&agt_home),
            "--test-config-dir".to_string(),
            s(&config),
        ];
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        let reset = || {
            for root in [&support, &agt_home, &config] {
                let _ = fs::remove_dir_all(root);
                fs::create_dir_all(root).unwrap();
            }
        };
        let targets = [
            ("home", home.clone()),
            ("bin", production[0].clone()),
            ("bin/agt", production[0].join("agt")),
            ("runtimes", production[1].join("runtimes")),
            ("install.lock", production[1].join("install.lock")),
        ];
        let mut cases: Vec<(PathBuf, PathBuf)> = Vec::new();
        for name in SUPPORT_DERIVED {
            let production_target = targets
                .iter()
                .find(|(n, _)| *n == name)
                .map_or(production[1].join(name), |(_, t)| t.clone());
            cases.push((support.join(name), production_target));
            cases.push((support.join(name), outside.join(name)));
        }
        for name in AGT_HOME_DERIVED {
            cases.push((agt_home.join(name), production[2].join(name)));
            cases.push((agt_home.join(name), outside.join(name)));
        }
        cases.push((
            config.join("settings.toml"),
            production[3].join("settings.toml"),
        ));
        cases.push((config.join("settings.toml"), outside.join("settings.toml")));
        cases.push((
            support.join("runtimes/0.17.0"),
            production[1].join("runtimes/0.17.0"),
        ));
        cases.push((
            support.join("current"),
            production[1].join("runtimes/0.17.0"),
        ));
        cases.push((support.join("current"), PathBuf::from("../outside")));
        for (link, target) in cases {
            reset();
            fs::create_dir_all(link.parent().unwrap()).unwrap();
            symlink(&target, &link).unwrap();
            let error = w.test(&args).unwrap_err();
            assert!(
                error.contains(&*link.to_string_lossy()),
                "{} -> {}: {error}",
                link.display(),
                target.display()
            );
        }
        // A real `current` folder isn't the installer's link either.
        reset();
        fs::create_dir_all(support.join("current")).unwrap();
        assert!(w.test(&args).unwrap_err().contains("runtimes/<version>"));
        // The installer's own links resolve, and a dangling `current` waits for the next install.
        reset();
        symlink("runtimes/0.17.0", support.join("current")).unwrap();
        let p = w.test(&args).unwrap();
        fs::create_dir_all(support.join("runtimes/0.17.0")).unwrap();
        assert!(w.test(&args).is_ok());
        // previous-cli keeps a replaced foreign CLI, often a link; the installer only renames it.
        symlink(&outside, support.join("previous-cli")).unwrap();
        assert!(w.test(&args).is_ok());
        // prepare() makes a real private HOME and refuses one that turned into a link later.
        p.prepare().unwrap();
        assert!(fs::symlink_metadata(p.child_home()).unwrap().is_dir());
        fs::remove_dir(p.child_home()).unwrap();
        symlink(&home, p.child_home()).unwrap();
        assert!(p.prepare().unwrap_err().contains("is a symlink"));
        assert!(fs::read_dir(&home).unwrap().all(|e| {
            let name = e.unwrap().file_name();
            [".local", "Library", ".agents-router", ".agt"].contains(&name.to_str().unwrap())
        }));
        // Nothing reached the stand-in production folders or the outside one.
        for folder in production.iter().chain([&outside]) {
            assert_eq!(
                fs::read_dir(folder).unwrap().count(),
                0,
                "{}",
                folder.display()
            );
        }
    }

    #[test]
    fn release_keeps_its_environment_behaviour_and_refuses_test_flags() {
        let mut w = World::new("paths-release");
        let home = w.home.clone();
        let p = w
            .resolve(RELEASE_IDENTIFIER, &["--self-test-wake", "-psn_0_1"])
            .unwrap();
        assert!(!p.test && p.rows().is_none() && p.relaunch_args().is_empty());
        assert_eq!(
            p.support.path,
            home.join("Library/Application Support/Agents Router")
        );
        assert_eq!(p.bin.path, home.join(".local/bin"));
        assert_eq!(p.agt_home.path, home.join(".agents-router"));
        assert_eq!(p.settings_file(), home.join(".agt/settings.toml"));
        assert_eq!(p.child_home(), home);
        assert_eq!(
            p.child_env(),
            vec![
                ("PYTHONPATH", None),
                ("PYTHONHOME", None),
                ("AGT_HOME", None)
            ]
        );
        assert!(p.account_home.is_none() && p.exclude_homes.is_none());
        p.prepare().unwrap();
        assert!(!home.join("Library").exists());
        // Env overrides apply as before, without overlap checks.
        w.set("AGT_HOME", home.join(".agents-router"))
            .set("AGT_APP_SUPPORT", "/somewhere/support");
        let p = w.resolve(RELEASE_IDENTIFIER, &[]).unwrap();
        assert_eq!(p.agt_home.path, home.join(".agents-router"));
        assert_eq!(p.settings_file(), home.join(".agents-router/settings.toml"));
        assert_eq!(p.support.path, PathBuf::from("/somewhere/support"));
        assert_eq!(
            p.child_env()[2],
            ("AGT_HOME", Some(home.join(".agents-router").into()))
        );
        w.set("AGT_CONFIG_DIR", "/cfg");
        assert_eq!(
            w.resolve(RELEASE_IDENTIFIER, &[]).unwrap().settings_file(),
            PathBuf::from("/cfg/settings.toml")
        );
        w.set("AGT_APP_SUPPORT", "relative");
        assert_eq!(
            w.resolve(RELEASE_IDENTIFIER, &[]).unwrap_err(),
            "AGT_APP_SUPPORT must be absolute"
        );
        // The release refuses test flags before touching the filesystem, even with a missing HOME.
        let mut bare = World::new("paths-release-flags");
        bare.vars.clear();
        for args in [
            vec!["--test-app-support", "/tmp/x"],
            vec!["--test-agt-home=/tmp/x"],
            vec!["--self-test-wake", "--test-anything"],
        ] {
            let error = bare.resolve(RELEASE_IDENTIFIER, &args).unwrap_err();
            assert!(
                error.contains("works only in Agents Router Test.app"),
                "{error}"
            );
        }
    }

    #[test]
    fn test_children_get_every_folder_and_a_private_home() {
        let w = World::new("paths-child");
        let p = w.test(&[]).unwrap();
        let env: HashMap<_, _> = p.child_env().into_iter().collect();
        let support = &p.support.path;
        assert_eq!(env["AGT_APP_SUPPORT"], Some(support.clone().into()));
        assert_eq!(env["AGT_BIN_DIR"], Some(support.join("cli-bin").into()));
        assert_eq!(env["AGT_HOME"], Some(support.join("agt-home").into()));
        assert_eq!(env["AGT_CONFIG_DIR"], Some(support.join("agt-home").into()));
        assert_eq!(env["HOME"], Some(support.join("home").into()));
        assert_eq!(env["CFFIXED_USER_HOME"], Some(support.join("home").into()));
        // The private HOME, the launch HOME and the account's home, in that order.
        assert_eq!(
            env[EXCLUDE_HOMES],
            Some(
                std::env::join_paths([support.join("home"), w.home.clone(), w.account.clone()])
                    .unwrap()
            )
        );
        assert_eq!(p.account_home, Some(w.account.clone()));
        assert!(p
            .describe()
            .contains(&format!("account_home={}", w.account.display())));
        for name in [
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            "GEMINI_CLI_HOME",
            "PYTHONPATH",
        ] {
            assert_eq!(env[name], None, "{name}");
        }
        assert_eq!(
            tilde(&support.join("agt-home"), &w.home),
            "~/Library/Application Support/Agents Router Test/agt-home"
        );
        assert_eq!(tilde(Path::new("/opt/x"), &w.home), "/opt/x");
        assert_eq!(tilde(&w.home, &w.home), "~");
    }

    #[test]
    fn relaunch_replaces_old_test_flags_and_keeps_the_rest() {
        let args: Vec<OsString> = [
            "--self-test-update",
            "/root",
            "--test-agt-home",
            "/old",
            "--test-bin-dir=/old-bin",
        ]
        .iter()
        .map(OsString::from)
        .collect();
        let relaunch = vec!["--test-agt-home".to_string(), "/new".to_string()];
        assert_eq!(
            relaunch_argv(&args, &relaunch),
            ["--self-test-update", "/root", "--test-agt-home", "/new"]
                .iter()
                .map(OsString::from)
                .collect::<Vec<_>>()
        );
        assert_eq!(plist_escape("/a & <b>"), "/a &amp; &lt;b&gt;");
    }
}
