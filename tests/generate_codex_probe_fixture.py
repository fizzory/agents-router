"""Producer contract: PYTHONPATH=src:tests python3 tests/generate_codex_probe_fixture.py."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import helpers  # isolate real homes and CLIs before importing agt

from agents_router import accounts, cli
from agents_router.config import Account, Config, RuntimeConfig

FIXTURE = Path(__file__).parent / "fixtures/app/usage-probe-codex.json"
NOW = 1800000000.0


def fixture_text():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime = RuntimeConfig("codex", root / "codex", include_primary=True, _found=[])
        runtime.accounts = [Account("fresh", root / "fresh")]
        for account in runtime.listed_accounts():
            account.home.mkdir()
        env = {"AGT_HOME": str(root / "agt"),
               "AGT_CODEX_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_codex.py"))]),
               "FAKE_CODEX_APP_SERVER": json.dumps({"rateLimits": {"rateLimitReachedType": None, "primary": {
                   "usedPercent": 12, "windowDurationMins": 300, "resetsAt": NOW + 3600}}})}
        with patch.dict(os.environ, env), patch("time.time", return_value=NOW), \
                patch.object(cli, "load_config", return_value=Config({"codex": runtime})):
            accounts.update_usage("codex", "fresh", windows={
                "five_hour": {"utilization": .1, "resets_at": NOW + 3600}}, observed_at=NOW - 599)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert cli.main(["usage", "--probe-codex", "--json"]) == 0
            return output.getvalue()


if __name__ == "__main__":
    FIXTURE.write_text(fixture_text())
    print("Wrote tests/fixtures/app/usage-probe-codex.json")
