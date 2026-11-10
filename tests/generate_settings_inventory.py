"""Generate the UI coverage inventory from real `config show --all --json`.

Run: PYTHONPATH=src:tests python3 tests/generate_settings_inventory.py
No provider runs: helpers isolates the process before agt is imported.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import helpers  # noqa: F401
from agents_router import cli

INVENTORY = Path(__file__).resolve().parents[1] / "macos/ui/settings-inventory.json"
CONTROL_KINDS = {"int", "float", "bool", "choice", "text", "names", "auto_copy"}


def inventory_data():
    with tempfile.TemporaryDirectory(prefix="agt-settings-inventory-") as folder:
        home = Path(folder)
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(home)
            with patch.dict(os.environ, HOME=str(home), AGT_HOME=str(home / ".agents-router"),
                            AGT_CONFIG_DIR=str(home / ".agt"), CLAUDE_CONFIG_DIR=str(home / ".claude"),
                            CODEX_HOME=str(home / ".codex")), contextlib.redirect_stdout(output):
                code = cli.main(["config", "show", "--all", "--json"])
        finally:
            os.chdir(previous)
        if code != 0:
            raise AssertionError(f"config show exited {code}")
        settings = {}
        for row in json.loads(output.getvalue())["settings"]:
            if row["kind"] in CONTROL_KINDS and row["editable"]:
                settings[row["key"]] = "control:" + row["kind"]
            elif row["changed_by"]:
                settings[row["key"]] = "command:" + row["changed_by"]
            elif not row["editable"]:
                settings[row["key"]] = "read-only"
            else:
                raise AssertionError(f"No UI mapping for {row['key']} ({row['kind']})")
        return {"schema_version": 1, "producer": "tests/generate_settings_inventory.py", "settings": settings}


if __name__ == "__main__":
    data = inventory_data()
    INVENTORY.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote macos/ui/settings-inventory.json ({len(data['settings'])} keys)")
