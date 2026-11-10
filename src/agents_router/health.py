"""Passive account/model health, admission claims and generation-scoped trial leases.

Callers capture ``HealthState.generation`` before starting an attempt and pass it
to ``record_outcome`` before releasing its ``TrialLease``. I/O and lock errors
propagate so the runner can fail open without changing the attempt's outcome.
All times are epoch seconds; ``now`` can be injected without sleeping in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import time
from typing import Any, BinaryIO

from agents_router.config import Account, RuntimeConfig, state_dir
from agents_router.fsutil import file_lock, read_json, unlock, write_json

CLAIM_SECONDS = 120
REASONS = {
    "capacity": "model at capacity",
    "launch": "worker could not start",
    "transport": "connection failed",
    "no_result": "worker ended without a result",
    "parser": "could not read the result",
}


@dataclass(frozen=True)
class CircuitState:
    scope: str
    state: str
    until: float | None = None
    reason: str | None = None
    trial_taken: bool = False


@dataclass(frozen=True)
class HealthState:
    generation: int
    circuits: tuple[CircuitState, ...] = ()

    @property
    def state(self) -> str:
        return next((state for state in ("unhealthy", "recovering")
                     if any(c.state == state for c in self.circuits)), "healthy")

    @property
    def blocked(self) -> bool:
        return any(c.state == "unhealthy" or c.trial_taken for c in self.circuits)

    @property
    def trials(self) -> tuple[str, ...]:
        return tuple(c.scope for c in self.circuits if c.state == "recovering")


@dataclass
class TrialLease:
    runtime: str
    account: Account
    generation: int
    job_id: str
    scopes: tuple[str, ...]
    _slots: list[tuple[BinaryIO, Path]] = field(default_factory=list, repr=False)
    _released: bool = field(default=False, repr=False)

    def set_owner(self, pid: int, started: int | None) -> None:
        """Keep the trial busy if its runner dies while this worker survives."""

        from agents_router.accounts import _owner_path

        if self._released:
            raise ValueError("trial lease has been released")
        for _handle, path in self._slots:
            write_json(_owner_path(path), {"pid": pid, "started": started, "job_id": self.job_id})

    def release(self) -> None:
        """Release every trial; safe to call again after cleanup or a neutral exit."""

        from agents_router.accounts import _owner_path

        # Always unlock every handle, including when removing one owner file fails.
        error = None
        for handle, path in self._slots:
            try:
                _owner_path(path).unlink(missing_ok=True)
            except OSError as exc:
                error = exc
            finally:
                unlock(handle)
        self._slots.clear()
        self._released = True
        if error is not None:
            raise error


def model_scope(runtime: RuntimeConfig, model: str | None = None) -> str:
    """Resolve the same model scope at admission, acquisition and completion."""

    return "model:" + (model or runtime.default_model or "default")


def _key(account: Account) -> str:
    # Local imports let accounts use health without introducing an import cycle.
    from agents_router.accounts import _home_key

    return _home_key(account.home)


def _path(runtime: RuntimeConfig) -> Path:
    return state_dir() / f"{runtime.runtime}-health.json"


def _lease_path(runtime: RuntimeConfig, account: Account, scope: str, generation: int) -> Path:
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return (state_dir() / "leases" / runtime.runtime / "trial" / _key(account)
            / f"{digest}.g{generation}.lock")


def _empty(runtime: RuntimeConfig) -> dict[str, Any]:
    return {"failures": 0, "first_failure": None, "opened_at": None,
            "open_until": None, "open_seconds": runtime.circuit_open_seconds,
            "reason": None, "claim": None}


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _load(runtime: RuntimeConfig, account: Account, now: float):
    """Read under the health lock; distinguish malformed data from clock repairs."""

    data = read_json(_path(runtime), {})
    if not isinstance(data, dict):
        data = {}
    raw = data.get(_key(account))
    raw = raw if isinstance(raw, dict) else {}
    generation = raw.get("generation", 0)
    generation = generation if _integer(generation) else 0
    record = {"account": account.name, "generation": generation, "circuits": {}}
    circuits = raw.get("circuits")
    changed = False
    if not isinstance(circuits, dict):
        return data, record, changed
    for scope, original in circuits.items():
        if scope != "account" and not scope.startswith("model:"):
            continue
        if not isinstance(original, dict):
            continue
        c = {**_empty(runtime), **original}
        if (not _integer(c["failures"]) or not _number(c["open_seconds"]) or c["open_seconds"] <= 0
                or any(c[k] is not None and not _number(c[k])
                       for k in ("first_failure", "opened_at", "open_until"))
                or (c["opened_at"] is None) != (c["open_until"] is None)
                or (c["reason"] is not None and not isinstance(c["reason"], str))):
            continue
        if c["first_failure"] is not None and c["first_failure"] > now:
            c.update(failures=0, first_failure=None)
            changed = True
        if c["opened_at"] is not None and c["opened_at"] > now:
            c.update(opened_at=now, open_until=now)
            changed = True
        claim = c["claim"]
        if claim is not None and (
                not isinstance(claim, dict) or not isinstance(claim.get("job"), str)
                or not _integer(claim.get("generation")) or claim["generation"] != generation
                or not _number(claim.get("deadline")) or claim["deadline"] <= now
                or claim["deadline"] - CLAIM_SECONDS > now + 1e-6):
            c["claim"] = None
            changed = True
        record["circuits"][scope] = c
    return data, record, changed


def _save(runtime: RuntimeConfig, account: Account, data: dict, record: dict) -> None:
    data[_key(account)] = record
    write_json(_path(runtime), data)


def _state(circuit: dict, now: float, skip_rest: bool = False) -> str:
    if circuit["opened_at"] is None:
        return "healthy"
    return "recovering" if skip_rest or now >= circuit["open_until"] else "unhealthy"


def _busy(path: Path) -> bool:
    from agents_router.accounts import _take_slot

    if not path.exists():
        return False
    handle = _take_slot(path)
    if handle is None:
        return True
    unlock(handle)
    return False


def _states(runtime, account, model, record, now, job_id, skip_rest):
    result = []
    for scope in ("account", model_scope(runtime, model)):
        c = record["circuits"].get(scope, _empty(runtime))
        state = _state(c, now, skip_rest)
        claim = c["claim"]
        taken = (state != "healthy" and (
            (claim is not None and claim["job"] != job_id)
            or _busy(_lease_path(runtime, account, scope, record["generation"]))))
        result.append(CircuitState(scope, state, c["open_until"], c["reason"], bool(taken)))
    return HealthState(record["generation"], tuple(result))


def read_record(runtime: RuntimeConfig, account: Account, *, now: float | None = None,
                read_only: bool = False) -> dict[str, Any]:
    """Return the folder's record; read_only skips locks and never saves repairs."""

    if not runtime.circuit_breaker:
        return {"account": account.name, "generation": 0, "circuits": {}}
    now = time.time() if now is None else now
    if read_only:
        return _load(runtime, account, now)[1]
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        if changed:
            _save(runtime, account, data, record)
        return record


def query_state(runtime: RuntimeConfig, account: Account, model: str | None = None, *,
                job_id: str | None = None, now: float | None = None, skip_rest: bool = False) -> HealthState:
    """Rank-time state and blockers; skip_rest is reserved for explicit live checks."""

    if not runtime.circuit_breaker:
        return HealthState(0)
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        if changed:
            _save(runtime, account, data, record)
        return _states(runtime, account, model, record, now, job_id, skip_rest)


def claim_trial(runtime: RuntimeConfig, account: Account, model: str | None, job_id: str, *,
                generation: int | None = None, now: float | None = None,
                skip_rest: bool = False) -> HealthState | None:
    """Claim all required trials atomically; None means blocked or stale generation.

    A healthy (or disabled) runtime returns an unblocked state with no trials.
    Calling again for the same job preserves its original claim deadline.
    """

    if not runtime.circuit_breaker:
        return HealthState(0)
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        state = _states(runtime, account, model, record, now, job_id, skip_rest)
        if state.blocked or (generation is not None and generation != state.generation):
            if changed:
                _save(runtime, account, data, record)
            return None
        for scope in state.trials:
            c = record["circuits"][scope]
            if c["claim"] is None:
                c["claim"] = {"job": job_id, "generation": state.generation, "deadline": now + CLAIM_SECONDS}
                changed = True
        if changed:
            _save(runtime, account, data, record)
        return state


def release_claim(runtime: RuntimeConfig, account: Account, job_id: str, *,
                  generation: int, now: float | None = None) -> None:
    """Drop only this job's claims, for failed creation or abandoned admission."""

    if not runtime.circuit_breaker:
        return
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        if generation != record["generation"]:
            return
        for c in record["circuits"].values():
            if c["claim"] is not None and c["claim"]["job"] == job_id:
                c["claim"] = None
                changed = True
        if changed:
            _save(runtime, account, data, record)


def acquire_trial(runtime: RuntimeConfig, account: Account, model: str | None, job_id: str, *,
                  generation: int | None = None, now: float | None = None,
                  skip_rest: bool = False) -> TrialLease | None:
    """Adopt a claim or freshly acquire every required trial, all or none.

    Rechecks health, generation and claim ownership under the health lock. The
    caller takes its ordinary job slot first and releases it on a None result.
    """

    from agents_router.accounts import _owner_path, _take_slot

    if not runtime.circuit_breaker:
        return TrialLease(runtime.runtime, account, 0, job_id, ())
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        state = _states(runtime, account, model, record, now, job_id, skip_rest)
        if state.blocked or (generation is not None and generation != state.generation):
            if changed:
                _save(runtime, account, data, record)
            return None
        lease = TrialLease(runtime.runtime, account, state.generation, job_id, state.trials)
        try:
            for scope in state.trials:
                path = _lease_path(runtime, account, scope, state.generation)
                handle = _take_slot(path)
                if handle is None:
                    lease.release()
                    return None
                lease._slots.append((handle, path))
                write_json(_owner_path(path), {"pid": None, "started": None, "job_id": job_id})
            # Adopt only after every OS lock is held. A failed acquisition leaves
            # all admission claims in place for the owning job's next attempt.
            for scope in state.trials:
                if record["circuits"][scope]["claim"] is not None:
                    record["circuits"][scope]["claim"] = None
                    changed = True
            if changed:
                _save(runtime, account, data, record)
            return lease
        except BaseException:
            lease.release()
            raise


def _open(runtime: RuntimeConfig, c: dict, now: float, *, trial: bool, reason: str) -> None:
    seconds = (min(2 * c["open_seconds"], runtime.circuit_max_open_seconds)
               if trial else runtime.circuit_open_seconds)
    c.update(opened_at=now, open_until=now + seconds, open_seconds=seconds, reason=reason, claim=None)


def record_outcome(runtime: RuntimeConfig, account: Account, model: str | None = None, *,
                   outcome: str, generation: int, health_failure: str | None = None,
                   trials: TrialLease | None = None, now: float | None = None) -> bool:
    """Apply both outcome-table columns in one write; False means ignored/off.

    Neutral outcomes do not count. The caller releases leases after this call,
    even when it raises; stale generations never write or resurrect reset state.
    """

    if not runtime.circuit_breaker:
        return False
    if outcome != "success" and health_failure not in REASONS:
        return False
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, changed = _load(runtime, account, now)
        if generation != record["generation"]:
            return False
        before = {scope: c.copy() for scope, c in record["circuits"].items()}
        held = ()
        if (trials is not None and not trials._released and trials.generation == generation
                and trials.runtime == runtime.runtime and _key(trials.account) == _key(account)):
            held = trials.scopes
        model_key = model_scope(runtime, model)
        for scope in ("account", model_key):
            if outcome == "success":
                record["circuits"][scope] = _empty(runtime)
                continue
            targeted = scope == (model_key if health_failure == "capacity" else "account")
            if not targeted and scope not in held:
                continue
            c = record["circuits"].setdefault(scope, _empty(runtime))
            reason = REASONS[health_failure]
            if scope in held:
                _open(runtime, c, now, trial=True, reason=reason)
            elif targeted and _state(c, now) == "healthy":
                first = c["first_failure"]
                if first is None or now - first > runtime.circuit_failure_window_seconds:
                    c.update(failures=0, first_failure=now)
                c["failures"] += 1
                c["reason"] = reason
                if c["failures"] >= runtime.circuit_failure_threshold:
                    _open(runtime, c, now, trial=False, reason=reason)
        if changed or record["circuits"] != before:
            _save(runtime, account, data, record)
        return True


def reset(runtime: RuntimeConfig, account: Account, *, now: float | None = None) -> bool:
    """Clear circuits/claims and keep a generation tombstone; return whether any existed."""

    if not runtime.circuit_breaker:
        return False
    now = time.time() if now is None else now
    with file_lock(_path(runtime).with_suffix(".lock")):
        data, record, _changed = _load(runtime, account, now)
        cleared = any(c["failures"] or c["opened_at"] is not None or c["claim"] is not None
                      for c in record["circuits"].values())
        record.update(generation=record["generation"] + 1, circuits={})
        _save(runtime, account, data, record)
        return cleared
