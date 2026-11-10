"""Regenerate the app contract from the producer and deterministic fake state.

Run from the repository root: PYTHONPATH=src:tests python3 tests/generate_usage_fixture.py
"""

import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import helpers  # isolate every real CLI, home and background service before agt imports

from agents_router import accounts, usage_snapshot
from agents_router.config import Account, Config, RUNTIMES, RuntimeConfig
from agents_router.fsutil import write_json

NOW = 1800000000.0
FIXTURE = Path(__file__).parent / "fixtures" / "app" / "usage.json"


def build_fixture() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.dict(os.environ, {"AGT_HOME": str(root / "agt")}), patch("time.time", return_value=NOW), \
                patch("agents_router.usage_snapshot.same_process", side_effect=lambda pid, started: pid == 111):
            runtimes = {name: RuntimeConfig(name, root / name, _found=[]) for name in RUNTIMES}
            runtime = runtimes["claude"]
            names = ["claude-work", "claude-cooling", "claude-logged-out", "claude-unknown", "claude-busy",
                     "claude-off", "claude-stale", "claude-reset", "claude-unhealthy", "claude-recovering"]
            runtime.accounts = [Account(name, root / name, max_jobs=2) for name in names]
            runtime.primary = runtime.accounts[0].home
            runtime.accounts[5].enabled = False
            cache = {str(account.home.resolve()): {"logged_in": account.name != "claude-logged-out",
                                                   "stamp": None, "checked": NOW}
                     for account in runtime.accounts if account.name != "claude-unknown"}
            state = root / "agt" / "state"
            write_json(state / "claude-profiles.json", cache)
            usage = {"claude-work": {"observed_at": NOW - 60, "windows": {
                "five_hour": {"utilization": .17, "resets_at": NOW + 3600},
                "seven_day": {"utilization": .42, "resets_at": NOW + 86400}}},
                "claude-cooling": {"cooldown_until": NOW + 600, "cooldown_reason": "usage limit"},
                "claude-stale": {"observed_at": NOW - 4000, "windows": {
                    "five_hour": {"utilization": .7, "resets_at": NOW + 3600}}},
                "claude-reset": {"observed_at": NOW - 4000, "windows": {
                    "five_hour": {"utilization": 1, "resets_at": NOW - 1}}}}
            write_json(state / "claude-usage.json", usage)
            health = {}
            for account, scope, until in ((runtime.accounts[0], "model:opus", NOW + 60),
                                          (runtime.accounts[-2], "account", NOW + 120),
                                          (runtime.accounts[-1], "account", NOW - 1)):
                health[accounts._home_key(account.home)] = {"generation": 0, "circuits": {scope: {
                    "opened_at": NOW - 61, "open_until": until, "reason": "model at capacity"}}}
            write_json(state / "claude-health.json", health)
            busy = runtime.accounts[4]
            for index in range(2):
                slot = accounts._slot_path(runtime, busy, index)
                slot.parent.mkdir(parents=True, exist_ok=True)
                slot.write_bytes(b"\0")
                job_id = f"fixture-busy-{index}"
                write_json(accounts._owner_path(slot), {"pid": 111, "started": 1, "job_id": job_id})
                write_json(root / "agt" / "jobs" / job_id / "status.json", {
                    "runtime": "claude", "account": busy.name, "state": "running", "updated_at": NOW,
                    "runner_pid": 111})
            return usage_snapshot.snapshot(Config(runtimes), now=NOW)


def fixture_text() -> str:
    return json.dumps(build_fixture(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(fixture_text(), encoding="utf-8")
    print(f"Wrote {FIXTURE.relative_to(Path(__file__).resolve().parents[1])}")
