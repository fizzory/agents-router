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
else:
    print("fake app agt: unsupported request", file=sys.stderr)
    sys.exit(2)
