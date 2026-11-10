#!/usr/bin/env python3
"""Debug app fixture server. No imports from agt and no access to the user's setup.

Use an absolute path in AGT_APP_TEST_LAUNCHER (debug builds only), with HOME and
AGT_HOME pointing at a scratch folder. AGT_APP_TEST_CALLS optionally records argv
outside watched state/jobs/settings paths. No real agent program is ever started.
"""
import json
import os
from pathlib import Path
import sys

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/app"
args = sys.argv[1:]
if os.environ.get("AGT_APP_TEST_CALLS"):
    with Path(os.environ["AGT_APP_TEST_CALLS"]).open("a") as stream:
        stream.write(json.dumps(args) + "\n")
if args[:1] == ["usage"] and "--json" in args:
    name = os.environ.get("AGT_APP_TEST_USAGE_FIXTURE", "usage.json")
    if name not in ("usage.json", "usage-popover.json", "usage-nothing.json"):
        raise SystemExit("unsupported usage fixture")
    print((FIXTURES / name).read_text())
elif args[:2] == ["accounts", "check"] and "--json" in args:
    print((FIXTURES / "accounts-check.json").read_text())
elif args[:2] == ["config", "show"] and "--json" in args:
    print((FIXTURES / "config-show.json").read_text())
elif "--json" in args:
    fixtures = {
        ("roles", "show"): "roles-show-panes.json",
        ("roles", "config"): "roles-config.json",
        ("accounts", "list"): "accounts-list-locked.json",
        ("accounts", "add"): "accounts-add-check.json",
        ("accounts", "enable"): "accounts-enable.json",
        ("accounts", "disable"): "accounts-disable.json",
        ("share", "list"): "share-list-kinds.json",
        ("share", "status"): "share-status-waiting.json",
        ("share", "pick"): "share-pick-in.json",
        ("share", "undo"): "share-undo.json",
        ("config", "set"): "config-set.json",
        ("config", "unset"): "config-unset-absent.json",
        ("config", "revert"): "config-revert.json",
    }
    name = "activity-log.json" if args[:1] == ["log"] else fixtures.get(tuple(args[:2]))
    if args[:3] in (["share", "kinds", "add"], ["share", "kinds", "remove"]):
        name = "share-kinds-add.json" if args[2] == "add" else "share-kinds-remove.json"
    if not name or (args[:2] == ["accounts", "add"] and "--check" not in args):
        raise SystemExit("fake app agt: unsupported request")
    text = (FIXTURES / name).read_text().replace("<ROOT>", str(Path(os.environ["HOME"])))
    print(text)
else:
    print("fake app agt: unsupported request", file=sys.stderr)
    sys.exit(2)
