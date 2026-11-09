"""Health fields and wording shared by account reports and the Room line."""

from __future__ import annotations

import time
from typing import Any

from agents_router import health
from agents_router.config import Account, RuntimeConfig


def _open_circuits(runtime: RuntimeConfig, account: Account, now: float) -> list[tuple[str, dict[str, Any]]]:
    try:
        record = health.read_record(runtime, account, now=now)
    except OSError:
        return []  # an unreadable health store must not break a report
    return [(scope, circuit) for scope, circuit in sorted(record["circuits"].items()) if circuit["opened_at"] is not None]


def _fields(circuit: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "unhealthy_until": time.strftime("%Y-%m-%d %H:%M", time.localtime(circuit["open_until"])),
        "unhealthy_reason": circuit["reason"],
        "recovering": circuit["open_until"] <= now,
    }


def account_health(runtime: RuntimeConfig, account: Account, now: float) -> dict[str, Any]:
    """Only report open circuits; a healthy account gains no JSON fields."""

    fields: dict[str, Any] = {}
    models = []
    for scope, circuit in _open_circuits(runtime, account, now):
        entry = _fields(circuit, now)
        if scope == "account":
            fields.update(entry)
        else:
            models.append({"model": scope.removeprefix("model:"), **entry})
    if models:
        fields["unhealthy_models"] = models
    return fields


def _text(label: str, circuit: dict[str, Any], *, reason: bool = False) -> str:
    until = circuit["unhealthy_until"]
    text = f"{label} recovering" if circuit["recovering"] else f"{label} unhealthy until {until.rsplit(' ', 1)[-1]}"
    return text + (f" · last error: {circuit['unhealthy_reason']}" if reason else "")


def account_detail(entry: dict[str, Any]) -> str:
    """One clause per affected circuit, with the approved account-detail copy."""

    clauses = [_text(entry["name"], entry, reason=True)] if "unhealthy_until" in entry else []
    clauses += [_text(f"{entry['name']}/{model['model']}", model, reason=True) for model in entry.get("unhealthy_models") or []]
    return "; ".join(clauses)


def summary(runtime: RuntimeConfig, listed: list[Account], now: float) -> str:
    """The soonest-clearing circuit, plus the number of other affected circuits."""

    clauses = []
    for account in listed:
        for scope, circuit in _open_circuits(runtime, account, now):
            label = account.name + ("/" + scope.removeprefix("model:") if scope.startswith("model:") else "")
            clauses.append((circuit["open_until"], _text(label, _fields(circuit, now))))
    clauses.sort()
    if not clauses:
        return ""
    return clauses[0][1] + (f" (+{len(clauses) - 1} more)" if len(clauses) > 1 else "")
