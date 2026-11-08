"""Types shared by runtime adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import errno
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Callable, NoReturn, Protocol

from agents_router.config import Account, RuntimeConfig
from agents_router.fsutil import make_private_dir


@dataclass(frozen=True)
class ModelDiscoveryIO:
    """Injectable read-only inputs for model discovery; no login or refresh operations."""

    read_text: Callable[[Path], str] = lambda path: path.read_text(encoding="utf-8")
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run


ACCESS_LEVELS = ("read", "edit", "full")

# Outcomes a finished attempt can have.
SUCCESS = "success"
USAGE_LIMIT = "usage_limit"
AUTH = "auth"
ERROR = "error"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
MAX_TURNS = "max_turns"
NO_ACCOUNT = "no_account"
LOST = "lost"

# These diagnostics describe a rejected request or the machine, not a broken account.
# This check affects health metadata only; adapters keep their existing outcomes.
HEALTH_REJECTION_RE = re.compile(
    r"\b(?:unknown|invalid|unsupported) (?:model|argument|request|configuration|config)\b|"
    r"\bmodel\b[^\n]*(?:not found|does not exist|not available|not supported|do not have access)|"
    r"\b(?:configuration|config|TOML) (?:error|invalid|parse)|"
    r"(?:failed|unable) to (?:load|parse|read) (?:configuration|config)|"
    r"permission denied|access denied|not permitted|not allowed|auto-denied|"
    r"requires? (?:approval|permission)|cannot prompt for|"
    r"invalid api key|authentication|unauthenticated|unauthori[sz]ed|not logged in|"
    r"no space left|too many open files|cannot allocate memory|out of memory|"
    r"resource temporarily unavailable|exec format error|bad cpu type|"
    r"\b(?:unknown|unrecognized) (?:option|flag)s?\b|"
    r"\b(?:unexpected|unrecognized) arguments?\b|\binvalid value for\b|\bno such option\b|"
    r"no session .+ found|no conversation found|(?:required )?MCP server .+ failed to start",
    re.IGNORECASE,
)


def stream_health_failure(terminal: str, *, no_result: bool, transport: re.Pattern[str]) -> str | None:
    """Classify terminal diagnostics only, after the adapter's outcome checks."""

    if HEALTH_REJECTION_RE.search(terminal):
        return None
    if transport.search(terminal):
        return "transport"
    return "no_result" if no_result else None


class HealthLaunchError(OSError):
    """An explicitly identified account-local failure to start a worker.

    Never wrap generic launch exceptions in this type: missing/incompatible binaries,
    request/configuration/permission/auth rejections and machine-wide errors are neutral.
    """


def prepare_account_home(home: Path, *, create: bool = True) -> None:
    """Validate one account's home, creating a managed home when needed.

    Only a non-directory root or a permission failure proven to belong to that
    root is a health failure. Shared-parent creation and uncertain attribution
    keep their ordinary exceptions; callers may report them as setup warnings.
    """

    home = Path(home)

    def permission_failure(exc: OSError) -> NoReturn:
        if exc.errno in (errno.EACCES, errno.EPERM):
            try:
                # A denied shared ancestor can make a root operation fail too.
                # A link's target may have different ancestors: stay neutral.
                local = (not home.is_symlink() and home.parent.is_dir()
                         and os.access(home.parent, os.R_OK | os.W_OK | os.X_OK))
            except OSError:
                local = False
            if local:
                raise HealthLaunchError(exc.errno, "account home is not accessible", str(home)) from exc
        raise exc

    try:
        mode = home.stat().st_mode
    except FileNotFoundError:
        if not create:
            return
        # Failures here concern shared ancestors, never this account alone.
        make_private_dir(home.parent)
        try:
            try:
                home.mkdir(mode=0o700)
            except FileExistsError:
                pass  # another preparer may have created the root; validate it below
            mode = home.stat().st_mode
        except OSError as exc:
            permission_failure(exc)
    except OSError as exc:
        permission_failure(exc)
    if not stat.S_ISDIR(mode):
        raise HealthLaunchError(errno.ENOTDIR, "account home is not a directory", str(home))
    try:
        with os.scandir(home):
            pass
        if not os.access(home, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, "account home is not accessible", str(home))
    except OSError as exc:
        permission_failure(exc)


# Environment variables that identify or authenticate the *host* session.
# A worker must never inherit them, or it may run as the host's account.
HOST_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC_", "CODEX_COMPANION_", "CODEX_THREAD", "AGT_")
HOST_ENV_EXACT = {
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "CODEX_API_KEY",
    "MCP_CONNECTION_NONBLOCKING",
    "MCP_SERVER_CONNECTION_BATCH_SIZE",
}
KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH", "AGT_HOME"}


PARENT_SERVER = "agt-parent"


def parent_server(job_id: str) -> dict[str, Any]:
    """MCP server entry that gives a worker message_parent, ask_parent and check_parent_messages."""

    import sys
    from pathlib import Path

    from agents_router.config import home

    package_root = str(Path(__file__).resolve().parents[2])
    return {
        "command": sys.executable,
        "args": ["-m", "agents_router", "mcp", "--worker", job_id],
        "env": {"PYTHONPATH": package_root, "AGT_HOME": str(home())},
    }


WORKER_TOOLS_NOTE = (
    " You can talk to the main session with the agt-parent tools: message_parent for short progress "
    "notes or early findings, ask_parent only when you truly can't decide something from the task and "
    "the files, and check_parent_messages between major steps to pick up follow-up instructions."
)


def clean_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of the environment without host-session identity or credentials."""

    source = dict(os.environ if source is None else source)
    result: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in KEEP_ENV:
            result[key] = value
            continue
        if upper in HOST_ENV_EXACT or upper.startswith(HOST_ENV_PREFIXES):
            continue
        result[key] = value
    return result


@dataclass
class JobSpec:
    job_id: str
    runtime: str
    prompt: str
    cwd: str
    account: str | None = None
    model: str | None = None
    effort: str | None = None
    access: str = "edit"
    max_turns: int | None = None
    timeout_seconds: int = 1800
    add_dirs: list[str] = field(default_factory=list)
    resume_session: str | None = None
    resume_account: str | None = None  # name of the account that holds resume_session
    resume_home: str | None = None  # that account's folder: the transcript lives there, whatever it's called now
    parent: dict[str, Any] | None = None  # host CLI of the main session and its context paths
    depth: int = 1
    created: float = 0.0
    override: str | None = None  # user's words authorizing this start to exceed agt's caps
    rules: str | None = None  # the Role rules block the role call put before the task; a fresh-session follow-up repeats it
    _doctor_live: bool = False  # explicit live checks skip circuit rest, while still owning every required trial

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobSpec":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    stdin: str
    session_id: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class AttemptResult:
    outcome: str
    message: str = ""
    text: str = ""
    session_id: str | None = None
    resets_at: float | None = None
    cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    num_turns: int | None = None
    error_text: str = ""  # the error output the outcome was judged from (Retry-After hints and the like)
    before_start: bool = False  # ended by a runtime's preflight check; the worker never ran
    at_capacity: bool = False  # terminal provider overload; stays ERROR, never a quota cooldown
    health_failure: str | None = None  # launch, transport, no_result, parser or exhausted capacity


class Parser(Protocol):
    session_id: str | None
    last_text: str

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Consume one event. May return a usage update for the account pool."""

    def progress(self) -> dict[str, Any]:
        ...

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        ...


class Runtime(Protocol):
    name: str

    def discover_models(self, config: RuntimeConfig, account: Account, *,
                        io: ModelDiscoveryIO | None = None) -> tuple[str, ...] | None:
        """Account's offered model IDs, or None when no list is available."""
        ...

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        ...

    def version(self, config: RuntimeConfig) -> str | None:
        ...

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        ...

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        ...

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        ...

    def new_parser(self) -> Parser:
        ...

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        ...
