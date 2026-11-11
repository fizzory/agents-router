"""Account checks and settings writers used by the CLI and the app."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from agents_router import account_check, config, profiles, settings_write, setup, synclog, ui


class AccountError(ValueError):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def _name(args: Any) -> None:
    if not args.name or not config.NAME_RE.fullmatch(args.name) or args.name == "primary":
        raise AccountError("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).", 2)


def _add(args: Any, loaded: config.Config) -> config.Account:
    """Validate without saving, creating folders or probing auth; the config is a fresh private copy."""

    _name(args)
    runtime = loaded.runtime(args.runtime)
    if runtime.find(args.name):
        raise AccountError(f"{args.runtime} account '{args.name}' already exists.")
    home = Path(args.home).expanduser().resolve() if args.home else config.homes_dir() / args.runtime / args.name
    if home.exists() and not home.is_dir():
        raise AccountError(f"{home} isn't a folder.")
    try:
        status, account = setup.register_account(loaded, args.runtime, args.name, home)
    except ValueError as exc:
        raise AccountError(str(exc)) from None
    if status == "exists":
        raise AccountError(f"That folder already belongs to {args.runtime} account '{account.name}'.")
    return account


def _toggle(args: Any, loaded: config.Config) -> config.Account:
    if args.name == "primary":
        raise AccountError(f"'primary' is {args.runtime}'s own login; agt can't switch it on or off. "
                           "Choose a named extra account instead. See: agt accounts list", 2)
    _name(args)
    runtime = loaded.runtime(args.runtime)
    account = runtime.find(args.name)
    if account is None:
        raise AccountError(f"No {args.runtime} account named '{args.name}'. See: agt accounts list")
    if account.is_primary or profiles.same_folder(account.home, runtime.primary):
        raise AccountError(f"'{args.name}' is the primary {args.runtime} account; agt can't switch it on or off.")
    if account.found:
        raise AccountError(f"'{args.name}' is one of your own Claude profiles ({ui.short_path(account.home)}): agt runs it "
                           "with its own setup and keeps nothing to sync or switch. `agt accounts remove "
                           f"{args.name}` makes agt leave it alone.")
    return account


def command(args: Any) -> int:
    from agents_router import cli
    from agents_router.fsutil import make_private_dir
    from agents_router.runtimes import get_runtime

    args.runtime = args.runtime or "claude"
    try:
        if args.check and args.action != "add":
            raise AccountError("--check goes with agt accounts add.", 2)
        loaded = config.load_config(cached_only=True)
        runtime = loaded.runtime(args.runtime)
        if args.action == "check":
            rows = account_check.listed(runtime) if not args.name else [runtime.find(args.name)]
            if any(row is None for row in rows):
                raise AccountError(f"No {args.runtime} account named '{args.name}'. See: agt accounts list")
            deadline = time.monotonic() + account_check.CHECK_TIMEOUT
            reports = [account_check.check(runtime, row, timeout=max(0, deadline - time.monotonic())) for row in rows]
            report = {"schema_version": 1, "runtime": args.runtime, "accounts": reports}
            if args.json:
                cli._print(report)
            else:
                for row in reports:
                    state = "logged in" if row["logged_in"] is True else "logged out" if row["logged_in"] is False else "unknown"
                    print(f"{row['account']}: {state}")
            return 0
        validate = _add if args.action == "add" else _toggle
        account = validate(args, loaded)  # refusals and --check create nothing, including no lock file
        if args.check:
            report = {"ok": True, "account": args.name, "runtime": args.runtime, "home": str(account.home)}
            cli._print(report if args.json else f"{args.runtime} account '{args.name}' can be added with folder {account.home}")
            return 0
        with settings_write.locked():
            loaded = config.load_config(cached_only=True)
            runtime = loaded.runtime(args.runtime)
            account = validate(args, loaded)  # a check reserves nothing; re-read under the shared lock
            location = config.user_settings_path()
            backup = settings_write.backup(location, "user")
            old = account.enabled
            if args.action != "add":
                account.enabled = args.action == "enable"
            config.save_accounts(loaded)
            key = f"{args.runtime}.accounts.{args.name}"
            synclog.append({"type": "setting", **synclog.where(None), "file": str(location), "level": "user",
                            "key": key if args.action == "add" else key + ".enabled",
                            "old": None if args.action == "add" else old,
                            "new": str(account.home) if args.action == "add" else account.enabled,
                            "old_present": args.action != "add", "new_present": True,
                            "backup": str(backup) if backup else None,
                            "command": f"agt accounts {args.action} {args.name} --runtime {args.runtime}",
                            **settings_write.who(None)})
        if args.action != "add":
            cli._print({"ok": True, "account": account.name, "enabled": account.enabled} if args.json else
                       f"{account.name}: {'enabled' if account.enabled else 'disabled'}")
            return 0
        make_private_dir(account.home)
        print(f"Added {args.runtime} account '{args.name}' with folder {account.home}")
        adapter = get_runtime(args.runtime)
        if getattr(adapter, "add_note", None):
            print(adapter.add_note)
        cli._print_setup(adapter.prepare(runtime, account, cli._probe_spec(args.runtime)))
        if args.no_login:
            account_check.check(runtime, account)
            print(f"Log in later with: agt accounts login {args.name} --runtime {args.runtime}")
            return 0
        return cli._login(runtime, adapter, account)
    except (ValueError, RuntimeError, OSError) as exc:
        code = getattr(exc, "code", 2)
        if args.json:
            print(json.dumps({"ok": False, "error": {"account": args.name, "runtime": args.runtime,
                                                      "message": str(exc), "exit_code": code}}, indent=2))
        else:
            print(exc)
        return code
