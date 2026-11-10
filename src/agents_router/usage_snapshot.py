"""The versioned, local-only usage snapshot. Plain reads never repair state."""

from __future__ import annotations

from dataclasses import replace
import math
import time
from typing import Any

from agents_router import __version__, accounts, bundle, health, profiles, usage_view
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig, jobs_dir, state_dir
from agents_router.fsutil import lock_busy, read_json, same_process
from agents_router.limits import ACTIVE_STATES, NAMES, STALE_RUNNER_SECONDS
from agents_router.plans import account_cap


def _store(path):
    data = read_json(path)
    return {} if data is None and not path.exists() else data


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number in account state")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def _usage(record: Any) -> dict:
    if not isinstance(record, dict):
        raise ValueError("malformed usage record")
    _finite(record)
    for key in ("observed_at", "updated", "cooldown_until"):
        if record.get(key) is not None and not _number(record[key]):
            raise ValueError(f"malformed usage {key}")
    for key in ("login", "cooldown_reason"):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(f"malformed usage {key}")
    pools = record.get("pools", {})
    if not isinstance(pools, dict):
        raise ValueError("malformed usage pools")
    for pool in pools.values():
        if (not isinstance(pool, dict)
                or (pool.get("cooldown_until") is not None and not _number(pool["cooldown_until"]))):
            raise ValueError("malformed usage pool")
    windows = record.get("windows", {})
    if not isinstance(windows, dict):
        raise ValueError("malformed usage windows")
    for window in windows.values():
        if not isinstance(window, dict) or not _number(window.get("utilization")):
            raise ValueError("malformed usage window")
        for key in ("observed_at", "resets_at"):
            if window.get(key) is not None and not _number(window[key]):
                raise ValueError(f"malformed window {key}")
    return record


def _runtime(runtime: RuntimeConfig) -> RuntimeConfig:
    """Retain canonical profile names, but never run discovery's auth checks."""

    result = replace(runtime, accounts=[replace(a) for a in runtime.accounts],
                     _found=[replace(a) for a in runtime._found] if runtime._found is not None else None)
    if result._found is None:
        result._found = result._discover(cached_only=True) if result.runtime == "claude" else []
    return result


def _listed(runtime: RuntimeConfig) -> list[Account]:
    # Reports include disabled accounts and accounts excluded from unpinned jobs.
    listed = [runtime.primary_account(), *runtime.all_accounts()]
    return [a for i, a in enumerate(listed)
            if not any(profiles.same_folder(a.home, b.home) for b in listed[:i])]


def _alive(pid: Any, started: Any = None) -> bool:
    try:
        return same_process(int(pid), int(started) if started is not None else None) if pid else False
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _jobs(now: float) -> dict[str, dict]:
    """Read jobs without read_status (which can stop orphaned workers).

    Include a surviving worker even after final status, and starts whose runner
    has not yet written its PID. Stale records with no live process are ignored.
    """

    found = {}
    for path in sorted(jobs_dir().glob("*")):
        status = read_json(path / "status.json", {}) if path.is_dir() else {}
        if not isinstance(status, dict):
            continue
        worker = _alive(status.get("child_pid"), status.get("child_started"))
        try:
            pid = (path / "runner.pid").read_text(encoding="utf-8").strip()
        except OSError:
            pid = status.get("runner_pid")
        runner = _alive(pid)
        updated = status.get("updated_at", status.get("created_at"))
        recent = _number(updated) and 0 <= now - updated <= STALE_RUNNER_SECONDS
        active = (status.get("state") in ACTIVE_STATES and not (path / "inbox-closed.json").exists()
                  and (runner or recent))
        if not active and not worker:
            continue
        spec = read_json(path / "spec.json", {})
        spec = spec if isinstance(spec, dict) else {}
        found[path.name] = {
            "job_id": path.name, "runtime": status.get("runtime") or spec.get("runtime"),
            "account": status.get("account") or status.get("assigned_account") or spec.get("account"),
            "state": status.get("state") if active else "running",
            "surviving_worker": worker and not runner,
            "worker_key": f"worker:{status.get('child_pid')}:{status.get('child_started')}" if worker else None,
        }
    return found


def _leases() -> list[dict]:
    """Occupied execution leases, including workers whose runner has exited."""

    found = []
    root = state_dir() / "leases"
    for path in sorted(root.rglob("*.lock")):
        if not path.stem.isdecimal():
            continue  # excludes selection and health-trial locks
        owner = read_json(accounts._owner_path(path), {})
        owner = owner if isinstance(owner, dict) else {}
        if not lock_busy(path) and not _alive(owner.get("pid"), owner.get("started")):
            continue
        parts = path.relative_to(root).parts
        worker = _alive(owner.get("pid"), owner.get("started"))
        found.append({"path": path, "runtime": parts[0] if parts[0] in RUNTIMES else None,
                      "home_key": parts[1] if len(parts) == 3 and parts[0] in RUNTIMES else None,
                      "job_id": owner.get("job_id") if isinstance(owner.get("job_id"), str) else None,
                      "worker": worker,
                      "worker_key": f"worker:{owner.get('pid')}:{owner.get('started')}" if worker else None})
    return found


def _windows(runtime: str, record: dict, now: float) -> list[dict]:
    if runtime in usage_view.NO_USAGE:
        return []
    result = []
    for name, window in record.get("windows", {}).items():
        observed = window.get("observed_at", record.get("observed_at", record.get("updated")))
        reset = window.get("resets_at")
        result.append({"name": name, "label": usage_view.WINDOW_LABELS.get(name, name),
                       "used_percent": round(window["utilization"] * 100),
                       "resets_at": float(reset) if reset is not None else None,
                       "observed_at": float(observed) if observed is not None else None,
                       "stale": observed is None or now - observed > usage_view.STALE_AFTER_SECONDS,
                       "reset": reset is not None and reset <= now})
    return result


def _health(runtime: RuntimeConfig, account: Account, now: float) -> dict:
    if not runtime.circuit_breaker:
        return {}
    raw = _store(health._path(runtime))
    if not isinstance(raw, dict):
        raise ValueError("malformed health store")
    original = raw.get(accounts._home_key(account.home), {})
    if not isinstance(original, dict) or not isinstance(original.get("circuits", {}), dict):
        raise ValueError("malformed health record")
    _finite(original)
    if "generation" in original and not health._integer(original["generation"]):
        raise ValueError("malformed health generation")
    for circuit in original.get("circuits", {}).values():
        if not isinstance(circuit, dict):
            raise ValueError("malformed health circuit")
        for key in ("opened_at", "open_until", "first_failure", "open_seconds"):
            if circuit.get(key) is not None and not _number(circuit[key]):
                raise ValueError(f"malformed health {key}")
        if ((circuit.get("opened_at") is None) != (circuit.get("open_until") is None)
                or ("failures" in circuit and not health._integer(circuit["failures"]))
                or (circuit.get("open_seconds") is not None and circuit["open_seconds"] <= 0)
                or (circuit.get("reason") is not None and not isinstance(circuit["reason"], str))):
            raise ValueError("malformed health circuit")
    return health.read_record(runtime, account, now=now, read_only=True)["circuits"]


def _merged(runtime: RuntimeConfig, account: Account, usage: dict, now: float) -> dict:
    # account_record's shared-login window selection needs each source's own
    # timestamp retained, rather than one timestamp from the merged record.
    prepared = {}
    for name, record in usage.items():
        prepared[name] = {**record, "windows": {
            key: {**window, "observed_at": window.get("observed_at", record.get("observed_at", record.get("updated")))}
            for key, window in record.get("windows", {}).items()}}
    return accounts.account_record(runtime, account, prepared, now=now) or {}


def _account(runtime: RuntimeConfig, account: Account, usage: dict, problems: dict,
             leases: list[dict], active: dict, now: float) -> dict:
    occupied = sum(lease["runtime"] == runtime.runtime and lease["home_key"] == accounts._home_key(account.home)
                   for lease in leases)
    capacity = account_cap(runtime, account)
    running = sum(job["runtime"] == runtime.runtime and bool(job["account"])
                  and runtime.named(account, str(job["account"])) for job in active.values())
    result = {"name": account.name, "enabled": account.enabled, "availability": "unknown",
              "reason": None, "until": None, "running_jobs": running, "busy_slots": occupied,
              "capacity": capacity, "unhealthy_models": [], "windows": []}
    try:
        if account.name in problems:
            raise ValueError(problems[account.name])
        record = _merged(runtime, account, usage, now)
        result["windows"] = _windows(runtime.runtime, record, now)
        circuits = _health(runtime, account, now)
        for scope, circuit in sorted(circuits.items()):
            if scope.startswith("model:") and circuit["opened_at"] is not None:
                result["unhealthy_models"].append({
                    "model": scope.removeprefix("model:"), "reason": circuit["reason"],
                    "until": circuit["open_until"],
                    "availability": "recovering" if circuit["open_until"] <= now else "unhealthy"})
        circuit = circuits.get("account", {})
        opened = circuit.get("opened_at") is not None
        cooling = accounts.cooldown_until(record, now)
        from agents_router import account_check

        auth = account_check.cached(runtime, account)
        if auth is None and runtime.runtime != "claude":
            auth = account.logged_in
        if not account.enabled:
            state, reason, until = "off", "disabled in agt settings", None
        elif auth is False:
            state, reason, until = "logged_out", "cached login check says logged out", None
        elif opened and circuit["open_until"] > now:
            state, reason, until = "unhealthy", circuit["reason"], circuit["open_until"]
        elif cooling:
            state, reason, until = "cooling", record.get("cooldown_reason"), cooling
        elif opened:
            state, reason, until = "recovering", circuit["reason"], circuit["open_until"]
        elif capacity is not None and occupied >= capacity:
            state, reason, until = "busy", "every execution slot is occupied", None
        elif auth is True:
            state, reason, until = "ok", None, None
        else:
            state, reason, until = "unknown", "no cached login check", None
        result.update(availability=state, reason=reason, until=until)
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        result.update(availability="unknown", reason="could not read account state", problem=str(exc),
                      windows=[], unhealthy_models=[])
    return result


def snapshot(config: Config, *, now: float | None = None) -> dict:
    """Schema v1: usage and availability, global jobs and uninstall blockers."""

    now = time.time() if now is None else now
    active = _jobs(now)
    leases = _leases()
    unidentified = {}
    workers = {job["worker_key"]: key for key, job in active.items() if job["worker_key"]}
    for lease in leases:
        key = (lease["job_id"] or workers.get(lease["worker_key"]) or lease["worker_key"]
               or "lease:" + str(lease["path"].relative_to(state_dir())))
        lease["key"] = key
        if not lease["job_id"] and not lease["worker"]:
            unidentified[key] = {"kind": "occupied_lease", "job_id": None,
                                 "runtime": lease["runtime"], "account": None}
            continue
        if key not in active:
            status = read_json(jobs_dir() / key / "status.json", {}) if lease["job_id"] else {}
            status = status if isinstance(status, dict) else {}
            active[key] = {"job_id": lease["job_id"], "runtime": status.get("runtime") or lease["runtime"],
                           "account": status.get("account") or status.get("assigned_account"),
                           "state": "running", "surviving_worker": lease["worker"]}
    providers = []
    known = {}
    for name in RUNTIMES:
        runtime = _runtime(config.runtime(name))
        listed = _listed(runtime)
        known[name] = (runtime, listed)
        for lease in leases:
            if lease["runtime"] != name or not lease["home_key"]:
                continue
            key = lease["key"]
            matched = next((a for a in listed if accounts._home_key(a.home) == lease["home_key"]), None)
            job = active.get(key, unidentified.get(key))
            if matched and job and not job["account"]:
                job["account"] = matched.name
        raw = _store(accounts._usage_path(name))
        usage, problems = {}, {}
        for account in listed:
            try:
                usage[account.name] = _usage(raw.get(account.name, {})) if isinstance(raw, dict) else _usage(raw)
                if runtime.runtime == "claude":
                    account.login = profiles.account_info(account.home)["login"] or usage[account.name].get("login")
                    for other in runtime.all_accounts():
                        if profiles.same_folder(other.home, account.home):
                            other.login = account.login
            except ValueError as exc:
                problems[account.name] = str(exc)
        # A primary folder may now have a discovered canonical profile name.
        primary = runtime.primary_account()
        if (isinstance(raw, dict) and "primary" in raw and primary.name != "primary"
                and primary.name not in raw):
            try:
                usage[primary.name] = _usage(raw["primary"])
                for account in listed:
                    if account.name == primary.name:
                        account.login = account.login or usage[primary.name].get("login")
            except ValueError as exc:
                problems[primary.name] = str(exc)
        providers.append({"runtime": name, "label": "Claude Code" if name == "claude" else NAMES[name],
                          "accounts": [_account(runtime, a, usage, problems, leases, active, now) for a in listed]})

    blockers = {key: {"kind": "surviving_worker" if job["surviving_worker"] else "active_job",
                      "job_id": job["job_id"], "runtime": job["runtime"], "account": job["account"]}
                for key, job in active.items()}
    blockers.update(unidentified)

    def removed(job):
        runtime, listed = known.get(job["runtime"], (None, []))
        return bool(job["account"] and not any(runtime.named(a, str(job["account"])) for a in listed))

    totals = {"active": len(active), **{state: sum(job["state"] == state for job in active.values())
                                       for state in ACTIVE_STATES},
              "unassigned": sum(not job["account"] for job in active.values()),
              "removed_accounts": sum(removed(job) for job in active.values()),
              "surviving_workers": sum(b["kind"] == "surviving_worker" for b in blockers.values())}
    return {"schema_version": 1, "agt_version": __version__, "generated_at": float(now),
            "install": bundle.installation(), "providers": providers, "jobs": totals,
            "uninstall_blockers": list(blockers.values())}


def lines(report: dict) -> list[str]:
    result = []
    for provider in report["providers"]:
        for account in provider["accounts"]:
            state = account["availability"].replace("_", " ")
            text = f"{provider['label']} {account['name']} {state}"
            if account["until"] is not None:
                text += " · back " + time.strftime("%H:%M", time.localtime(account["until"]))
            for window in account["windows"]:
                detail = f"{window['label']} {window['used_percent']}% used"
                if window["resets_at"] is not None:
                    detail += " · resets " + time.strftime("%H:%M", time.localtime(window["resets_at"]))
                detail += " · reset" if window["reset"] else " · stale" if window["stale"] else ""
                text += " · " + detail
            result.append(text)
    return result


def command(args) -> int:
    import json
    from agents_router.config import load_config

    config = load_config()
    if args.refresh:
        usage_view.refresh_codex(config)
    report = snapshot(config)
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) if args.json else "\n".join(lines(report)))
    return 0
