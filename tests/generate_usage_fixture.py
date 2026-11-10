"""Regenerate the app contract from the producer and deterministic fake state.

Run from the repository root: PYTHONPATH=src:tests python3 tests/generate_usage_fixture.py
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import helpers  # isolate every real CLI, home and background service before agt imports

from agents_router import account_check, accounts, usage_snapshot
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig
from agents_router.fsutil import write_json

NOW = 1800000000.0
FIXTURE = Path(__file__).parent / "fixtures" / "app" / "usage.json"


POPOVER_NOW = datetime(2026, 11, 10, 19, tzinfo=timezone.utc).timestamp()
POPOVER_FOLDER = FIXTURE.parent.parent / "popover"


def build_fixture(scenario="base") -> dict:
    now = NOW if scenario == "base" else POPOVER_NOW
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.dict(os.environ, {"AGT_HOME": str(root / "agt")}), patch("time.time", return_value=now), \
                patch("agents_router.usage_snapshot.same_process", side_effect=lambda pid, started: pid == 111):
            runtimes = {name: RuntimeConfig(name, root / name, _found=[]) for name in RUNTIMES}
            runtime = runtimes["claude"]
            names = ["claude-work", "claude-cooling", "claude-logged-out", "claude-unknown", "claude-busy",
                     "claude-off", "claude-stale", "claude-reset", "claude-unhealthy", "claude-recovering"]
            runtime.accounts = [Account(name, root / name, max_jobs=2) for name in names]
            runtime.primary = runtime.accounts[0].home
            runtime.accounts[5].enabled = False
            cache = {str(account.home.resolve()): {"logged_in": account.name != "claude-logged-out",
                                                   "stamp": None, "checked": now}
                     for account in runtime.accounts if account.name != "claude-unknown"}
            state = root / "agt" / "state"
            write_json(state / "claude-profiles.json", cache)
            usage = {"claude-work": {"observed_at": now - 60, "windows": {
                "five_hour": {"utilization": .17, "resets_at": now + 3600},
                "seven_day": {"utilization": .42, "resets_at": now + 86400}}},
                "claude-cooling": {"cooldown_until": now + 600, "cooldown_reason": "usage limit"},
                "claude-stale": {"observed_at": now - 4000, "windows": {
                    "five_hour": {"utilization": .7, "resets_at": now + 3600}}},
                "claude-reset": {"observed_at": now - 4000, "windows": {
                    "five_hour": {"utilization": 1, "resets_at": now - 1}}}}
            if scenario != "base":
                usage["claude-work"]["windows"]["seven_day"].update(
                    utilization=.86, resets_at=now + 86400)
                usage["claude-cooling"]["cooldown_until"] = now + 9600  # back 21:40 UTC
                usage["claude-stale"]["windows"]["seven_day"] = {
                    "utilization": .86, "resets_at": now + 86400, "observed_at": now - 7200}
                usage["claude-reset"]["windows"]["seven_day"] = {
                    "utilization": .5, "resets_at": now + 86400, "observed_at": now - 60}
                usage["claude-malformed"] = {"windows": []}
                runtime.accounts.append(Account("claude-malformed", root / "claude-malformed"))
                runtime.accounts[4].max_jobs = 4
                # Two jobs can own the four occupied execution leases; the UI reports jobs, not leases.
                write_json(state / "account-checks.json", {
                    account_check._key(other, other.primary_account()): {"logged_in": True, "checked_at": now}
                    for other in (runtimes["kiro"], runtimes["antigravity"])})
            write_json(state / "claude-usage.json", usage)
            health = {}
            for account, scope, until in ((runtime.accounts[0], "model:opus", now + 60),
                                          (runtime.accounts[8], "account", now + 120),
                                          (runtime.accounts[9], "account", now - 1)):
                health[accounts._home_key(account.home)] = {"generation": 0, "circuits": {scope: {
                    "opened_at": now - 61, "open_until": until, "reason": "model at capacity"}}}
            write_json(state / "claude-health.json", health)
            busy = runtime.accounts[4]
            for index in range(0 if scenario == "idle" else 2 if scenario == "base" else 4):
                slot = accounts._slot_path(runtime, busy, index)
                slot.parent.mkdir(parents=True, exist_ok=True)
                slot.write_bytes(b"\0")
                job_id = f"fixture-busy-{index % 2}"
                write_json(accounts._owner_path(slot), {"pid": 111, "started": 1, "job_id": job_id})
                write_json(root / "agt" / "jobs" / job_id / "status.json", {
                    "runtime": "claude", "account": busy.name, "state": "running", "updated_at": now,
                    "runner_pid": 111})
            if scenario == "nothing":
                runtime.accounts = runtime.accounts[1:3]
                runtime.primary = runtime.accounts[0].home
                usage["claude-cooling"]["cooldown_until"] = now + 4500  # Back 20:15 UTC
                write_json(state / "claude-usage.json", usage)
                write_json(state / "account-checks.json", {
                    account_check._key(other, other.primary_account()): {"logged_in": False, "checked_at": now}
                    for other in (runtimes["kiro"], runtimes["antigravity"])})
            return usage_snapshot.snapshot(Config(runtimes), now=now)


def fixture_text() -> str:
    return json.dumps(build_fixture(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def fixture_texts() -> dict:
    return {"usage.json": fixture_text(), **{
        f"usage-{name}.json": json.dumps(build_fixture(name), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        for name in ("popover", "nothing", "idle")}}


def popover_states_text() -> str:
    # Native envelope inputs, generated separately from the agt Usage DTO fixtures.
    states = {
        "reading": {"fixture": "usage-popover.json", "reading": True},
        "first": {"reading": True},
        "empty": {"empty": True},
        "unreadable": {"error": "Can't read agt: timed out"},
        "missing": {"error": "agt not found", "update_version": "0.17.1"},
        "old": {"error": "agt 0.16 is too old", "update_version": "0.17.1"},
        "update": {"fixture": "usage-popover.json", "update_version": "0.17.1"},
        "nothing": {"fixture": "usage-nothing.json"},
    }
    return json.dumps(states, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    for name, text in fixture_texts().items():
        (FIXTURE.parent / name).write_text(text, encoding="utf-8")
        print(f"Wrote tests/fixtures/app/{name}")
    POPOVER_FOLDER.mkdir(parents=True, exist_ok=True)
    (POPOVER_FOLDER / "states.json").write_text(popover_states_text(), encoding="utf-8")
    print("Wrote tests/fixtures/popover/states.json")
