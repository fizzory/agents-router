"""Account pools: usage state, cooldowns, per-account job slots and selection."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, BinaryIO

from agents_router import health
from agents_router.config import Account, QuotaPool, RuntimeConfig, state_dir
from agents_router.plans import account_cap, account_limits, model_allowed
from agents_router.fsutil import file_lock, read_json, same_process, try_lock, unlock, write_json

# Default for `usage_ceiling`: above this utilization (0..1) an account gets no new jobs
# (admission) and sorts after every other account (ranking).
NEARLY_FULL = 0.95
MAX_COOLDOWN_SECONDS = 7 * 86400
RETRY_AFTER_RE = re.compile(
    r"retry[- ]after\b[\s:=]*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)?\b", re.IGNORECASE
)


def _usage_path(runtime: str):
    return state_dir() / f"{runtime}-usage.json"


def load_usage(runtime: str) -> dict[str, dict[str, Any]]:
    data = read_json(_usage_path(runtime), {})
    return data if isinstance(data, dict) else {}


def update_usage(runtime: str, account: str, *, clear_cooldown_if: str | None = None,
                 deadline: float | None = None,
                 **fields: Any) -> dict[str, Any]:
    """Merge under the usage lock; fresh Codex windows may clear only an expected cooldown."""

    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=10 if deadline is None else max(0, deadline - time.monotonic())):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("usage update deadline passed")
        data = load_usage(runtime)
        record = dict(data.get(account) or {})
        fresh_windows = False
        if runtime == "codex":
            observed = _number(record.get("observed_at") or (record.get("updated") if record.get("windows") else None)) or 0
            if record.get("windows") and "observed_at" not in record:
                record["observed_at"] = observed  # metadata changes must not freshen legacy windows
            if fields.get("windows") is not None:
                incoming = _number(fields.get("observed_at"))
                incoming = time.time() if incoming is None else incoming
                if incoming > observed:
                    fields["observed_at"] = incoming
                    fresh_windows = True
                else:
                    fields.pop("windows")
                    fields.pop("observed_at", None)
            else:
                fields.pop("observed_at", None)  # only a window observation can advance its timestamp
        if fields.get("cooldown_until") is not None:
            fields["cooldown_revision"] = secrets.token_hex(16)
        record.update({key: value for key, value in fields.items() if value is not None})
        if (fresh_windows and clear_cooldown_if is not None
                and record.get("cooldown_revision") == clear_cooldown_if):
            record.pop("cooldown_until", None)
            record.pop("cooldown_reason", None)
            record.pop("cooldown_revision", None)
        record["updated"] = time.time()
        data[account] = record
        write_json(path, data)
    return record


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_rate_limit_event(runtime: str, account: str, info: dict[str, Any], login: str | None = None) -> None:
    """Store the windows reported by a `rate_limit_event` (Claude) or equivalent."""

    windows: dict[str, Any] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, window in unified.items():
            if isinstance(window, dict):
                windows[str(name)] = {
                    "utilization": _number(window.get("utilization")),
                    "resets_at": _number(window.get("resetsAt")),
                }
    elif info.get("rateLimitType"):
        windows[str(info["rateLimitType"])] = {
            "utilization": _number(info.get("utilization")),
            "resets_at": _number(info.get("resetsAt")),
        }
    # Only usage figures are stored here. A cooldown comes from the runner, and only
    # when an attempt actually ended with a usage limit.
    update_usage(runtime, account, status=info.get("status"), windows=windows or None, login=login)


def retry_after_seconds(text: str) -> float | None:
    """A Retry-After value in error text: "Retry-After: 120" or "retry after 5 minutes"."""

    match = RETRY_AFTER_RE.search(text or "")
    if not match:
        return None
    unit = (match.group(2) or "s").lower()
    scale = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    return float(match.group(1)) * scale


def cooldown_end(error_text: str, resets_at: float | None, cooldown_minutes: int, now: float | None = None) -> float:
    """When an account that hit its limit is usable again.

    In order (OpenCodex's shape): a Retry-After value in the error text, then the
    reset time the CLI reported, then the configured cooldown; never more than 7 days.
    """

    now = now or time.time()
    retry = retry_after_seconds(error_text)
    if retry is not None and retry > 0:
        until = now + retry
    elif resets_at and resets_at > now:
        until = float(resets_at)
    else:
        until = now + cooldown_minutes * 60
    return min(until, now + MAX_COOLDOWN_SECONDS)


def set_cooldown(runtime: str, account: str, until: float, reason: str) -> None:
    update_usage(runtime, account, cooldown_until=until, cooldown_reason=reason)


def pool_cooldown_until(record: dict[str, Any] | None, pool: str, now: float | None = None) -> float | None:
    return cooldown_until(((record or {}).get("pools") or {}).get(pool), now)


def set_pool_cooldown(runtime: str, account: str, pool: str, until: float, reason: str) -> None:
    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = data.setdefault(account, {})
        record.setdefault("pools", {}).setdefault(pool, {}).update(cooldown_until=until, cooldown_reason=reason)
        record["updated"] = time.time()
        write_json(path, data)


def clear_cooldown(runtime: str, account: str, *, reason: str | None = None, keep_pools: bool = False) -> bool:
    """Forget a cooldown, optionally only the expected reason and preserving pool cooldowns."""

    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = data.get(account)
        if not isinstance(record, dict) or ("cooldown_until" not in record and not record.get("pools")):
            return False
        if reason is not None and record.get("cooldown_reason") != reason:
            return False
        record.pop("cooldown_until", None)
        record.pop("cooldown_reason", None)
        record.pop("cooldown_revision", None)
        if not keep_pools:
            record.pop("pools", None)
        record["updated"] = time.time()
        write_json(path, data)
    return True


def max_utilization(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    values = [
        float(window["utilization"])
        for window in (record.get("windows") or {}).values()
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
    ]
    return max(values) if values else None


def account_record(runtime: RuntimeConfig, account: Account, usage: dict[str, Any], *,
                   now: float | None = None) -> dict[str, Any] | None:
    """The account's usage record, merged with every folder that has the same login.

    Folders logged in to one account share its limits, so the busiest window and the
    latest cooldown of any of them hold for all.
    """

    def current(name: str) -> dict[str, Any] | None:
        record = usage.get(name)
        if not isinstance(record, dict):
            return None
        if account.login and record.get("login") and record["login"] != account.login:
            return None  # recorded under another login: the folder was logged in to a different account since
        return record

    records = [current(name) for name in runtime.login_group(account)]
    records = [record for record in records if record is not None]
    if len(records) <= 1:
        own = current(account.name)
        return own if own is not None else (records[0] if records else None)
    merged: dict[str, Any] = {"windows": {}, "pools": {}}
    now = time.time() if now is None else now

    def used(window: dict[str, Any]) -> float:
        # A window that has reset since it was recorded counts for nothing, so it never hides a current one.
        resets = window.get("resets_at")
        if isinstance(resets, (int, float)) and resets <= now:
            return -1.0
        return float(window["utilization"]) if isinstance(window.get("utilization"), (int, float)) else -1.0

    for record in records:
        for name, window in (record.get("windows") or {}).items():
            if not isinstance(window, dict):
                continue
            current = merged["windows"].get(name)
            if current is None or used(window) > used(current):
                merged["windows"][name] = window
        for key in ("cooldown_until", "last_used"):
            value = record.get(key)
            if isinstance(value, (int, float)) and value > float(merged.get(key) or 0):
                merged[key] = value
                if key == "cooldown_until":
                    merged["cooldown_reason"] = record.get("cooldown_reason")
        for pool, entry in (record.get("pools") or {}).items():
            until = (entry or {}).get("cooldown_until") if isinstance(entry, dict) else None
            if isinstance(until, (int, float)) and until > float((merged["pools"].get(pool) or {}).get("cooldown_until") or 0):
                merged["pools"][pool] = entry
    return merged


def cooldown_until(record: dict[str, Any] | None, now: float | None = None) -> float | None:
    now = now or time.time()
    until = (record or {}).get("cooldown_until")
    return float(until) if isinstance(until, (int, float)) and until > now else None


def _owner_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.stem + ".owner.json")


@dataclass
class Lease:
    runtime: str
    account: Account
    handle: BinaryIO
    path: Path | None = None  # the slot's lock file

    pool_slots: list[tuple[BinaryIO, Path]] = field(default_factory=list)
    trial: health.TrialLease | None = None

    def set_owner(self, pid: int, started: int | None, job_id: str | None = None) -> None:
        """Record the worker process, so the slot stays busy if the runner dies before it."""

        paths = ([self.path] if self.path is not None else []) + [path for _handle, path in self.pool_slots]
        for path in paths:
            write_json(_owner_path(path), {"pid": pid, "started": started, "job_id": job_id})
        if self.trial is not None:
            self.trial.set_owner(pid, started)

    def release(self) -> None:
        if self.trial is not None:
            try:
                self.trial.release()
            except OSError:
                pass  # trial cleanup must not strand ordinary execution slots
        try:
            if self.path is not None:
                _owner_path(self.path).unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            unlock(self.handle)
            for handle, path in self.pool_slots:
                try:
                    _owner_path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                finally:
                    unlock(handle)
            self.pool_slots.clear()


def _home_key(home: Path) -> str:
    """A short key for an account folder, the same for every name that points to it."""

    try:
        resolved = str(Path(home).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = os.path.abspath(str(home))
    return hashlib.sha256(os.path.normcase(resolved).encode("utf-8")).hexdigest()[:16]


def _slot_path(runtime: RuntimeConfig, account: Account, slot: int) -> Path:
    # Keyed on the folder, not the name: two names for one folder share its slots.
    return state_dir() / "leases" / runtime.runtime / _home_key(account.home) / f"{slot}.lock"


def _orphan_alive(lock_path: Path) -> bool:
    """True if the slot's recorded worker still runs (its runner died without cleaning up)."""

    owner = read_json(_owner_path(lock_path))
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid") or 0)
        started = owner.get("started")
        return same_process(pid, int(started) if started is not None else None)
    except (TypeError, ValueError):
        return False


def _take_slot(lock_path: Path) -> BinaryIO | None:
    """Lock a free slot. A slot whose recorded worker still runs counts as busy."""

    handle = try_lock(lock_path)
    if handle is None:
        return None
    if _orphan_alive(lock_path):
        unlock(handle)
        return None
    try:
        _owner_path(lock_path).unlink(missing_ok=True)  # stale record of a finished worker
    except OSError:
        pass
    return handle


def _take_capped_slot(folder: Path, cap: int | None, *, override: bool = False) -> tuple[BinaryIO, Path] | None:
    """Lease a slot, counting overflow slots too. Only override may exceed the cap.

    Serialize selection so a normal job cannot use a gap while overflow jobs
    still occupy the full allowance. Owner records keep orphaned workers counted.
    A cap of None never refuses, but the slot is still taken so the job stays counted.
    """

    available = None
    with file_lock(folder / "selection.lock", timeout=30):
        indices = sorted(int(path.stem) for path in folder.glob("*.lock") if path.stem.isdecimal())
        busy = 0
        try:
            for index in indices:
                path = folder / f"{index}.lock"
                handle = _take_slot(path)
                if handle is None:
                    busy += 1
                elif available is None:
                    available = (handle, path)
                else:
                    unlock(handle)
            if not override and cap is not None and busy >= cap:
                return None
            if available is not None:
                result, available = available, None
                return result
            index = next((i for i, used in enumerate(indices) if i != used), len(indices))
            path = folder / f"{index}.lock"
            handle = _take_slot(path)
            return (handle, path) if handle is not None else None
        finally:
            if available is not None:
                unlock(available[0])


def try_lease(runtime: RuntimeConfig, account: Account, *, override: str | None = None) -> Lease | None:
    """Take one of the account's job slots, or None if all are busy."""

    slot = _take_capped_slot(_slot_path(runtime, account, 0).parent, account_cap(runtime, account), override=bool(override))
    return Lease(runtime.runtime, account, *slot) if slot is not None else None


def _lease_pool(lease: Lease, runtime: RuntimeConfig, pool: QuotaPool, *, override: str | None = None) -> bool:
    root = state_dir() / "leases" / runtime.runtime / "pools" / pool.name
    for folder, cap in ((root / _home_key(lease.account.home), pool.per_account), (root / "total", pool.total)):
        if cap is None:
            continue
        slot = _take_capped_slot(folder, cap, override=bool(override))
        if slot is None:
            lease.release()
            return False
        lease.pool_slots.append(slot)
    return True


def busy_slots(runtime: RuntimeConfig, account: Account) -> int:
    busy = 0
    folder = _slot_path(runtime, account, 0).parent
    for path in folder.glob("*.lock"):
        if not path.stem.isdecimal():
            continue
        handle = try_lock(path)
        if handle is None:
            busy += 1
            continue
        if _orphan_alive(path):
            busy += 1
        unlock(handle)
    return busy


@dataclass(frozen=True)
class Blocker:
    account: str
    kind: str  # quota, health, or model (model restrictions are never health failures)
    until: float | None = None
    reason: str | None = None
    scope: str = "account"
    state: str | None = None  # unhealthy or recovering for health blockers


@dataclass
class Ranking:
    accounts: list[Account] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    health: dict[str, health.HealthState] = field(default_factory=dict)

    @property
    def cooldown_end(self) -> float | None:
        return min((b.until for b in self.blockers if b.kind == "quota" and b.until is not None), default=None)

    @property
    def trial_busy(self) -> bool:
        return any(b.kind == "health" and b.state == "recovering" for b in self.blockers)


def ranked_accounts(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
    prefer: str | None = None,
    model: str | None = None,
    pool: QuotaPool | None = None,
    now: float | None = None,
    job_id: str | None = None,
    skip_rest: bool = False,
) -> Ranking:
    """Usable accounts best-first, with distinct quota, health and model blockers."""

    now = time.time() if now is None else now
    usage = load_usage(runtime.runtime)
    candidates = runtime.worker_accounts()
    if pinned:
        account = runtime.find(pinned)
        if account is None:
            raise ValueError(_unknown_account(runtime, pinned))
        if runtime.is_session(account):
            raise ValueError(_own_profile(runtime, account.name))
        candidates = [account]
    elif not candidates and runtime.session_home is not None:
        raise ValueError(_no_other_profile(runtime))
    exclude = exclude or set()
    result = Ranking()
    usable = result.accounts
    for account in candidates:
        if account.name in exclude:
            continue
        if not account.enabled:
            continue
        if model and not model_allowed(runtime, account_limits(runtime, account), model):
            result.blockers.append(Blocker(account.name, "model", reason="model not allowed", scope="model:" + model))
            continue  # the account's model list does not include it
        record = account_record(runtime, account, usage)
        ends = [cooldown_until(record, now), pool_cooldown_until(record, pool.name, now) if pool else None]
        until = max((end for end in ends if end is not None), default=None)
        if until is not None:
            pool_blocked = pool is not None and ends[1] == until
            source = ((record or {}).get("pools") or {}).get(pool.name, {}) if pool_blocked else (record or {})
            result.blockers.append(Blocker(account.name, "quota", until, source.get("cooldown_reason"),
                                          "pool:" + pool.name if pool_blocked else "account"))
            continue
        try:
            state = health.query_state(runtime, account, model, job_id=job_id, now=now, skip_rest=skip_rest)
        except OSError:
            usable.append(account)  # no trustworthy generation: route without a health lease
            continue
        result.health[account.name] = state
        blocked = [c for c in state.circuits if c.state == "unhealthy" or c.trial_taken]
        if blocked:
            # A blocked account is not busy just because its other circuit has a trial.
            unhealthy = [c for c in blocked if c.state == "unhealthy"]
            for circuit in unhealthy or blocked:
                result.blockers.append(Blocker(account.name, "health", circuit.until if unhealthy else None,
                                              circuit.reason, circuit.scope, circuit.state))
            continue
        usable.append(account)

    found = [account.name for account in runtime.found_accounts()]

    def rank(account: Account) -> int:
        # `order` first; without it agt's own accounts (least used first), then found profiles in their order.
        if runtime.order:
            return runtime.order.index(account.name) if account.name in runtime.order else len(runtime.order)
        return 1 + found.index(account.name) if account.name in found else 0

    def key(account: Account) -> tuple[bool, bool, bool, int, float, float]:
        record = account_record(runtime, account, usage) or {}
        utilization = max_utilization(record)
        nearly_full = utilization is not None and utilization >= runtime.usage_ceiling
        return (
            account.fallback,  # fallback accounts only after every regular one
            nearly_full,
            account.name != prefer,
            rank(account),
            utilization if utilization is not None else 0.0,
            float(record.get("last_used") or 0.0),
        )

    usable.sort(key=key)
    return result


def _others(runtime: RuntimeConfig) -> list[str]:
    return [account.name for account in runtime.listed_accounts() if not runtime.is_session(account)]


def _session_name(runtime: RuntimeConfig) -> str:
    session = next((account for account in runtime.all_accounts() if runtime.is_session(account)), None)
    return session.name if session else str(runtime.session_home)


def _own_profile(runtime: RuntimeConfig, name: str) -> str:
    others = _others(runtime)
    tail = f" Other profiles: {', '.join(others)}." if others else ""
    return (f"'{name}' is the Claude profile this session runs in, so it can't take this session's own jobs."
            f"{tail} Name another with account=NAME.")


def _no_other_profile(runtime: RuntimeConfig) -> str:
    return (f"No other Claude profile can take this job: {_session_name(runtime)} is the profile this session runs in. "
            "Name one with account=NAME, log in to another Claude folder, use a built-in subagent, "
            "or start it with runtime codex.")


def _unknown_account(runtime: RuntimeConfig, name: str) -> str:
    near = runtime.matches(name)
    if len(near) > 1:
        return f"'{name}' matches several {runtime.runtime} profiles: {', '.join(near)}. Name one in full."
    known = [account.name for account in runtime.listed_accounts()]
    tail = f" Known: {', '.join(known)}." if known else ""
    return f"no {runtime.runtime} account named '{name}'.{tail}"


def _acquire(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
    prefer: str | None = None,
    model: str | None = None,
    job_id: str | None = None,
    override: str | None = None,
    skip_rest: bool = False,
) -> tuple[Lease | None, float | None, bool]:
    """Lease the best free account.

    Returns (lease, earliest_cooldown_end, all_busy). A None lease with
    all_busy=True means accounts exist but every slot is taken right now.
    """

    from agents_router import limits, quota

    pool = quota.pool_for(runtime, model)
    state = quota.pool_state(runtime, limits.count_active(), time.time(), exclude_job=job_id) if pool else None
    rejected: set[tuple[str, health.HealthState | None]] = set()
    while True:
        ranking = ranked_accounts(
            runtime, pinned=pinned, exclude=exclude, prefer=prefer, model=model or runtime.default_model,
            pool=pool, job_id=job_id, skip_rest=skip_rest,
        )
        earliest = ranking.cooldown_end if not ranking.accounts else None
        if not override and pool and pool.total is not None and state.executing_totals.get(pool.name, 0) >= pool.total:
            return None, earliest, bool(ranking.accounts) or ranking.trial_busy
        budget_blocked = 0
        rerank = False
        for account in ranking.accounts:
            snapshot = ranking.health.get(account.name)
            if (account.name, snapshot) in rejected:
                continue
            if not override and pool and pool.weekly_jobs is not None and state.budget_used(account.name, pool.name) >= pool.weekly_jobs:
                budget_blocked += 1
                continue
            if not override and pool and pool.per_account is not None and state.executing.get((account.name, pool.name), 0) >= pool.per_account:
                continue
            lease = try_lease(runtime, account, override=override)
            if lease is None:
                continue
            try:
                if pool and not _lease_pool(lease, runtime, pool, override=override):
                    continue
                if snapshot is not None:
                    try:
                        lease.trial = health.acquire_trial(
                            runtime, account, model, job_id or "", generation=snapshot.generation, skip_rest=skip_rest,
                        )
                    except OSError:
                        pass  # fail open on health I/O, as at ranking and admission
                    else:
                        if lease.trial is None:
                            lease.release()
                            rejected.add((account.name, snapshot))
                            rerank = True
                            break
                if pool and job_id:
                    from agents_router.jobs import update_status

                    update_status(job_id, account=None, assigned_account=account.name, pool=pool.name)
                update_usage(runtime.runtime, account.name, last_used=time.time())
                return lease, None, False
            except BaseException:
                lease.release()
                raise
        if not rerank:
            return None, earliest, len(ranking.accounts) > budget_blocked or ranking.trial_busy


def acquire(runtime: RuntimeConfig, **kwargs: Any) -> tuple[Lease | None, float | None, bool]:
    if runtime.pools:
        # Share admission's lock: a lease and a new job cannot both reserve the last weekly start.
        with file_lock(state_dir() / "admission.lock", timeout=30):
            return _acquire(runtime, **kwargs)
    return _acquire(runtime, **kwargs)
