"""One live Claude fixture for fold, window-order and CLI tests, under throwaway homes only."""

import json
import os

from helpers import TempHome

from agents_router import accounts, profiles, usage_tap

LIVE_SOURCE = usage_tap.OBSERVED_AT_SOURCE


class ClaudeLiveFixture(TempHome):
    @staticmethod
    def login_hash(uuid):
        return profiles._hash(uuid)

    def claude_json(self, account, uuid):
        account.home.mkdir(parents=True, exist_ok=True)
        (account.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}),
                                                   encoding="utf-8")
        return self.login_hash(uuid)

    def live_path(self, account):
        return self.home / "state" / "claude-live" / f"{accounts._home_key(account.home)}.json"

    def put_live(self, account, content):
        """Write the folder's live file as the tap would, each write with its own mtime."""

        path = self.live_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
        self._live_ticks = getattr(self, "_live_ticks", 0) + 1
        os.utime(path, ns=(1_700_000_000_000_000_000 + self._live_ticks,) * 2)
        return path

    def live(self, account, windows, login=None):
        marked = {name: {**window, "observed_at_source": LIVE_SOURCE} for name, window in windows.items()}
        return self.put_live(account, {"login": self.login if login is None else login, "limits": True,
                                       "windows": marked, "superseded": {}})
