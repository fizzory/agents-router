"""Explicit, quota-free login checks. Usage polling reads the cache and never checks."""

from __future__ import annotations

import math
import os
from pathlib import Path
import time
from typing import Any

from agents_router import config, fsutil, profiles
from agents_router.runtimes import get_runtime

CHECK_TIMEOUT = 10


def _path() -> Path:
    return config.state_dir() / "account-checks.json"


def _key(runtime: config.RuntimeConfig, account: config.Account) -> str:
    return runtime.runtime + ":" + os.path.normcase(str(profiles.canonical(account.home)))


def cached(runtime: config.RuntimeConfig, account: config.Account) -> bool | None:
    if runtime.runtime == "claude":
        return profiles.logged_in(account.home, None, cached_only=True)
    data = fsutil.read_json(_path(), {})
    row = data.get(_key(runtime, account)) if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    checked = row.get("checked_at")
    if (type(checked) not in (int, float) or not math.isfinite(checked)
            or not 0 <= time.time() - checked < profiles.AUTH_TTL_SECONDS):
        return None
    value = row.get("logged_in")
    return value if type(value) is bool else None


def check(runtime: config.RuntimeConfig, account: config.Account, *, timeout: float = CHECK_TIMEOUT) -> dict[str, Any]:
    """Measure once with the runtime's bounded non-model probe, then cache that result."""

    result = None
    try:
        status = get_runtime(runtime.runtime).auth_status(runtime, account, timeout=timeout) if timeout > 0 else {}
        detail = str(status.get("error") or status.get("detail") or "")
        if not any(word in detail for word in ("TimeoutExpired", "binary not found", "kiro-cli not found", "OSError")):
            value = status.get("loggedIn")
            result = value if type(value) is bool else None
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        pass
    now = time.time()
    report = {"account": account.name, "logged_in": result, "checked_at": now,
              "error": "login check failed" if result is None else None}
    path = _path()
    with fsutil.file_lock(path.with_suffix(".lock"), timeout=10):
        data = fsutil.read_json(path, {})
        data = data if isinstance(data, dict) else {}
        data[_key(runtime, account)] = report
        fsutil.write_json(path, data)
    if runtime.runtime == "claude":
        profiles.forget(account.home)
        profiles.logged_in(account.home, lambda folder: result)
    account.logged_in = result
    return report


def listed(runtime: config.RuntimeConfig) -> list[config.Account]:
    rows = [runtime.primary_account(), *runtime.all_accounts()]
    return [row for index, row in enumerate(rows)
            if not any(profiles.same_folder(row.home, earlier.home) for earlier in rows[:index])]
