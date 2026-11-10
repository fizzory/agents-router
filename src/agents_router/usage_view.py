"""Usage that is already known, shown as one line, plus a free refresh for Codex.

Nothing here calls a model or starts a job. Codex usage is read from the newest rollout
file and re-checked live when an account is held back. Claude usage
comes from the rate-limit events of jobs that already ran. Antigravity exposes none.
"""

from __future__ import annotations

from threading import Thread
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agents_router import accounts, health_view, quota
from agents_router.config import Account, Config, RuntimeConfig, state_dir
from agents_router.fsutil import file_lock
from agents_router.limits import NAMES, utilization

if TYPE_CHECKING:
    from agents_router.runtimes.codex import RateLimits

STALE_AFTER_SECONDS = 1800
MAX_ROLLOUTS_TRIED = 5
CODEX_PROBE_INTERVAL_SECONDS = 600
CODEX_PROBE_TIMEOUT_SECONDS = 10
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "week", "primary": "5h", "secondary": "week"}


def newest_rollouts(home: Path, limit: int = MAX_ROLLOUTS_TRIED) -> list[Path]:
    """Newest rollout files first: <home>/sessions/YYYY/MM/DD/rollout-*.jsonl, newest day folders first."""

    found: list[Path] = []
    root = home / "sessions"
    try:
        for year in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            for month in sorted((p for p in year.iterdir() if p.is_dir()), reverse=True):
                for day in sorted((p for p in month.iterdir() if p.is_dir()), reverse=True):
                    files = sorted(day.glob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    found.extend(files)
                    if len(found) >= limit:
                        return found[:limit]
    except OSError:
        pass
    return found[:limit]


def refresh_codex(config: Config, *, live: bool = True, pinned: str | None = None) -> None:
    """Read rollouts; check due accounts together within one deadline, submitting the pin first."""

    from agents_router.runtimes.codex import last_rate_limits, usage_windows

    runtime = config.runtime("codex")
    for account in runtime.listed_accounts():
        try:
            for path in newest_rollouts(account.home):
                snapshot = last_rate_limits(path)
                windows = usage_windows(snapshot[0]) if snapshot else {}
                if not windows:
                    continue
                accounts.update_usage("codex", account.name, windows=windows, observed_at=snapshot[1])
                break
        except Exception:
            continue  # a convenience: never fail a start over an unreadable file
    if live:
        try:
            usage = accounts.load_usage("codex")
            ordered = sorted(runtime.listed_accounts(), key=lambda a: (
                a.name != pinned, float((usage.get(a.name) or {}).get("probed_at") or 0)))
            now = time.time()
            due = [account for account in ordered if _probe_due(runtime, usage.get(account.name) or {}, now)]
            if due:
                deadline = time.monotonic() + CODEX_PROBE_TIMEOUT_SECONDS
                probes = [Thread(target=probe_codex, args=(runtime, account),
                                 kwargs={"deadline": deadline}, daemon=True) for account in due]
                for probe in probes:
                    probe.start()
                for probe in probes:
                    # Cleanup must not extend admission's deadline or hold up interpreter exit.
                    probe.join(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            pass  # corrupt usage metadata cannot fail a start
    # TODO: Antigravity (agy) exposes no usage; see plans.md 3f.


def _usage_cooldown(record: dict[str, Any], now: float) -> bool:
    from agents_router.runtimes.codex import LIMIT_RE

    reason = record.get("cooldown_reason")
    return bool(accounts.cooldown_until(record, now) and isinstance(reason, str)
                and (reason == "usage_limit" or LIMIT_RE.search(reason)))


def _codex_probe_lock(account: Account) -> Path:
    return state_dir() / "codex-probes" / (accounts._home_key(account.home) + ".lock")


def _probe_due(runtime: RuntimeConfig, record: dict[str, Any], now: float) -> bool:
    used = utilization(record, now)
    return bool(((used is not None and used >= runtime.usage_ceiling) or _usage_cooldown(record, now))
                and now - float(record.get("probed_at") or 0) >= CODEX_PROBE_INTERVAL_SECONDS)


def probe_codex(runtime: RuntimeConfig, account: Account, *, force: bool = False,
                deadline: float | None = None) -> RateLimits | None:
    """Reserve a probe attempt before starting it, including failed attempts in the throttle."""

    from agents_router.runtimes.codex import probe_rate_limits

    try:
        if deadline is None:
            deadline = time.monotonic() + CODEX_PROBE_TIMEOUT_SECONDS
        lock = _codex_probe_lock(account)
        with file_lock(lock, timeout=max(0, deadline - time.monotonic())):
            now = time.time()
            record = accounts.load_usage("codex").get(account.name) or {}
            if not force and not _probe_due(runtime, record, now):
                return None
            if time.monotonic() >= deadline:
                return None
            record = accounts.update_usage("codex", account.name, probed_at=now, deadline=deadline)
            cooldown = record.get("cooldown_revision") if _usage_cooldown(record, now) else None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        result = probe_rate_limits(runtime, account, timeout=remaining)
        if result is None:
            return None
        used = utilization({"windows": result.windows}, time.time())
        clear = cooldown if (used is not None and used < runtime.usage_ceiling and result.reached_type is None) else None
        accounts.update_usage("codex", account.name, windows=result.windows, observed_at=result.observed_at,
                              clear_cooldown_if=clear, deadline=deadline)
        return result
    except Exception:
        return None


def held_back(runtime: RuntimeConfig, record: dict[str, Any], now: float) -> str | None:
    """The ceiling's admission hold, including the latest reset among blocking windows."""

    used = utilization(record, now)
    if used is None or used < runtime.usage_ceiling:
        return None
    text = f"held back: at the {runtime.usage_ceiling:.0%} usage ceiling"
    blocking = [window for window in (record.get("windows") or {}).values()
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
                and window["utilization"] >= runtime.usage_ceiling
                and not (isinstance(window.get("resets_at"), (int, float)) and window["resets_at"] <= now)]
    resets = [window.get("resets_at") for window in blocking]
    if resets and all(isinstance(value, (int, float)) for value in resets):
        text += " until " + reset_text(max(resets), now)
    return text


def _age(record: dict[str, Any], now: float) -> float:
    return now - float(record.get("observed_at") or record.get("updated") or now)


def _hours(seconds: float) -> str:
    return f"{round(seconds / 60)} min" if seconds < 5400 else f"{round(seconds / 3600)} h"


def reset_text(resets_at: float, now: float) -> str:
    """When a usage window resets, in local time, e.g. '2026-10-18 01:26 (in 4 h 15 min)'."""

    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets_at))
    if resets_at <= now:
        return f"{when} (passed; that window's figure is out of date)"
    days, rest = divmod(round((resets_at - now) / 60), 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{when} (in {days} d {hours} h)"
    return f"{when} (in {hours} h {minutes} min)" if hours else f"{when} (in {minutes} min)"


NO_USAGE = ("antigravity", "kiro")  # these CLIs print no usage figures; only their cooldowns are known
UNKNOWN = "unknown (no usage data)"


def account_quota(runtime: str, account: Any, record: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """What is known of one account's quota, and the words agt shows for it (the one wording source).

    `left_percent` is the share left in the busiest open window, `resets_at` when that window resets, and
    `observed_at` when the figure was read; all None when unknown. `text` starts with the account's name, e.g.
    "claude-work 8% left (5h, resets 14:20, as of 40 min ago)" or "primary unknown (no usage data)"."""

    record = record or {}
    tag = " fallback" if account.fallback else ""
    observed = record.get("observed_at") or record.get("updated")
    found: dict[str, Any] = {"runtime": runtime, "account": account.name, "known": False, "left_percent": None,
                             "window": None, "resets_at": None,
                             "observed_at": float(observed) if isinstance(observed, (int, float)) else None,
                             "cooling_until": accounts.cooldown_until(record, now)}
    if found["cooling_until"]:
        until = time.strftime("%H:%M", time.localtime(found["cooling_until"]))
        return {**found, "known": True, "text": f"{account.name}{tag} at its limit until {until}"}
    open_windows = {} if runtime in NO_USAGE else {
        name: window
        for name, window in (record.get("windows") or {}).items()
        if isinstance(window, dict)
        and isinstance(window.get("utilization"), (int, float))
        and not (isinstance(window.get("resets_at"), (int, float)) and window["resets_at"] <= now)
    }
    if not open_windows or utilization(record, now) is None:
        return {**found, "text": f"{account.name}{tag} {UNKNOWN}"}
    name, window = max(open_windows.items(), key=lambda item: item[1]["utilization"])
    left = max(0, round((1 - float(window["utilization"])) * 100))
    resets = window.get("resets_at") if isinstance(window.get("resets_at"), (int, float)) else None
    detail = [WINDOW_LABELS.get(name, name)]
    if resets is not None:
        detail.append(f"resets {time.strftime('%H:%M', time.localtime(resets))}")
    age = _age(record, now)
    if age > STALE_AFTER_SECONDS:
        detail.append(f"as of {_hours(age)} ago")
    return {**found, "known": True, "left_percent": left, "window": name, "resets_at": float(resets) if resets else None,
            "text": f"{account.name}{tag} {left}% left ({', '.join(detail)})"}


def _account_text(runtime: RuntimeConfig, account: Any, record: dict[str, Any], now: float) -> str | None:
    """The room line's words for an account; None when nothing is known (the line then counts accounts)."""

    quota = account_quota(runtime.runtime, account, record, now)
    if quota["known"]:
        return quota["text"]
    return f"{account.name} fallback" if account.fallback else None


def room_line(config: Config, runtimes: list[str] | None = None, now: float | None = None) -> str:
    """One line: the room each runtime's accounts have left, from what is known. Empty if nothing is."""

    from agents_router.limits import _available_runtime, count_active

    now = now or time.time()
    parts = []
    counts = count_active(now)
    for name in runtimes or list(NAMES):
        runtime = config.runtime(name)
        usage = accounts.load_usage(name)
        listed = runtime.worker_accounts()
        state = quota.pool_state(runtime, counts, now)
        texts = []
        for account in listed:
            record = usage.get(account.name) or {}
            text = _account_text(runtime, account, record, now)
            pools = quota.pool_report(runtime, account.name, state, record, now)
            if pools:
                text = (text or account.name) + " [" + "; ".join(pool["state"] for pool in pools) + "]"
            if text:
                texts.append(text)
        health_text = health_view.summary(runtime, listed, now)
        if health_text:
            texts.append(health_text)
        cap, left = quota.room_cap(runtime, now)
        if cap is not None:
            detail = f"{left:g}% left" if left is not None else "no usage data"
            effective = min(cap, runtime.max_jobs) if runtime.max_jobs is not None else cap
            texts.append(f"{cap} jobs at a time (max_jobs_by_room, {detail}; {counts.by_runtime.get(name, 0)} running; effective runtime cap {effective})")
        if health_text or runtime.pools or cap is not None or any("%" in text or "limit until" in text for text in texts):
            parts.append(f"{NAMES[name]} " + ", ".join(texts))
        elif _available_runtime(name):
            count = len(listed)
            noun = "account" if count == 1 else "accounts"
            parts.append(f"{NAMES[name]} {count} {noun} (no usage data)")
    return ("Room: " + " · ".join(parts)) if parts else ""
