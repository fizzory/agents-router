"""The environment the app checks give the app and agt: scratch folders only.

A caller may run under the release app's launcher, whose AGT_* variables name
the production folders. These checks swap HOME for a scratch one. A test build
refuses production folders under that HOME and under the account's home from
the user database, so an inherited AGT_APP_SUPPORT would stop the launch, and
one from another account could reach its folder. Dropping every AGT_* variable,
AGT_EXCLUDE_HOMES included, keeps each check in its own scratch folders.
"""
import os

# The folders the app and agt take from the environment. A check sets all four.
FOLDERS = ("AGT_APP_SUPPORT", "AGT_BIN_DIR", "AGT_HOME", "AGT_CONFIG_DIR")
# Provider and Python folders a child would otherwise inherit from the caller.
DROPPED = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME", "CFFIXED_USER_HOME",
           "PYTHONPATH", "PYTHONHOME")


def without_agt(base=None):
    """`base` (default: this process's environment) without any AGT_* or provider variable."""
    base = os.environ if base is None else base
    return {key: value for key, value in base.items()
            if not key.startswith("AGT_") and key not in DROPPED}


def scratch_env(values, base=None):
    """`without_agt(base)` plus `values`, which must name all four agt folders."""
    missing = [name for name in FOLDERS if not values.get(name)]
    if missing:
        raise ValueError(f"scratch_env needs {', '.join(missing)}")
    env = without_agt(base)
    env.update({key: str(value) for key, value in values.items()})
    return env
