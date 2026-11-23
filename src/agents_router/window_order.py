"""The one order that merges usage windows from every source, and how old a window's figure is.

The standalone tap owns the numeric checks and ordering key; importing them loads no agt configuration.
"""

from __future__ import annotations

from typing import Any

from agents_router.usage_tap import _finite, window_key


def observed_at(window: dict[str, Any], record: dict[str, Any] | None = None) -> float | None:
    """When a window's figure was read: its own `observed_at`, else the record's `observed_at`, else its `updated`."""

    for value in (window.get("observed_at"), *((record.get("observed_at"), record.get("updated")) if record else ())):
        number = _finite(value)
        if number is not None:
            return number
    return None


def merge_windows(*sources: dict[str, Any] | None, now: float) -> dict[str, dict[str, Any]]:
    """One window per name across `sources`, each the greatest under `window_key`. A name is never dropped:
    an expired window stays until a newer window of that name replaces it, so its age can still be shown."""

    merged: dict[str, dict[str, Any]] = {}
    for windows in sources:
        if not isinstance(windows, dict):
            continue
        for name, window in windows.items():
            if not isinstance(window, dict):
                continue
            current = merged.get(name)
            if current is None or window_key(window, now) > window_key(current, now):
                merged[name] = window
    return merged
