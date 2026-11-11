"""Read-only app contract from the sharing planner and the pick grid's decisions.

Cells contain no provider configuration, hook commands or MCP secret values.
An id is the same normalized, kind-qualified key the writer and state use.
"""

from __future__ import annotations

from typing import Any

from agents_router.share import engine, picker, plan as planner, state


def pick_cell(item: planner.Item, label: str, made: set[str]) -> picker.Cell:
    """The pick grid and listing share one editability decision."""
    cell = item.cells.get(label, planner.Cell(planner.BLOCKED, "not a folder agt shares"))
    status, note = cell.state, cell.note
    if status == planner.HAS:
        if note:
            return picker.Cell(picker.FIXED, note)
        if label not in made:
            return picker.Cell(picker.FIXED, "your original: agt never removes it")
        return picker.Cell(picker.ON, "agt's copy; untick to remove it, with a backup", there=True)
    if status in (planner.COPY, planner.UPDATE, planner.CONFLICTED):
        return picker.Cell(picker.ON, note or "agt copies it there", there=status != planner.COPY and label in made)
    if status == planner.WAITING:
        if note.startswith(planner.NEW_WAITING):
            return picker.Cell(picker.OFF, "new: tick where it should go")
        if note == "its folder is missing or frozen this run":
            return picker.Cell(picker.FIXED, note)
        return picker.Cell(picker.ON, note, there=label in made)
    if status == planner.BLOCKED:
        return picker.Cell(picker.BLOCKED, note)
    return picker.Cell(picker.OFF, note or "left out")


def snapshot(plan: planner.Plan, current: state.State, *, enabled: bool) -> dict[str, Any]:
    labels = [provider.label for provider in plan.included]
    items = []

    def cell(status, note="", reason=None):
        reason = reason or ("sharing is off" if not enabled else None)
        return {"state": status, "note": note, "editable": reason is None, "reason": reason}

    def add(key, kind, name, cells):
        items.append({"id": key, "kind": kind, "name": name, "cells": cells})

    for item in plan.items:
        made = set((current.items.get(item.key) or {}).get("made") or [])
        cells = {}
        for label in labels:
            raw = item.cells.get(label, planner.Cell(planner.BLOCKED, "not a folder agt shares"))
            choice = pick_cell(item, label, made)
            cells[label] = cell(raw.state, raw.note, choice.note if choice.state in (picker.FIXED, picker.BLOCKED) else None)
        add(item.key, item.kind, item.name, cells)
    for excluded in plan.excluded:
        key = f"{excluded.kind}/{planner.names.identity(excluded.name)}"
        if not any(item["id"] == key for item in items):
            add(key, excluded.kind, excluded.name,
                {label: cell(planner.BLOCKED, excluded.reason, excluded.reason) for label in labels})
    if plan.mcp is not None:
        from agents_router.share import mcp

        for server in plan.mcp.servers:
            made = mcp.agt_made(server, current)
            cells = {}
            for label in labels:
                reason = (server.held if server.held != "you turned it off" else None) or server.blocked.get(label)
                if label in server.copies and label not in made:
                    reason = reason or "your original: agt never removes it"
                decision = server.decision
                status = planner.HAS if label in server.copies else planner.COPY if label in server.writes else planner.SKIPPED
                if reason:
                    status = planner.BLOCKED if label not in server.copies else planner.HAS
                elif decision and decision.action == engine.CONFLICT:
                    status = planner.CONFLICTED
                cells[label] = cell(status, reason or (decision.reason if decision else ""), reason)
            add(server.key, "mcp", server.name, cells)
    if plan.hooks is not None:
        for hook in plan.hooks.hooks:
            cells = {}
            for label in labels:
                original = label in hook.labels
                copy = next((copy for copy in plan.hooks.copies if copy.hook is hook and copy.label == label), None)
                reason = "your original: agt never removes it" if original else hook.verdicts.get(label)
                status = planner.HAS if original or (copy and not copy.waiting) else planner.WAITING
                cells[label] = cell(planner.BLOCKED if reason and not original else status, reason or "", reason)
            add(f"hooks/{hook.name}", "hooks", hook.name, cells)
    if plan.plugins is not None:
        from agents_router.share import plugins

        for entry in plan.plugins.entries:
            made = plugins.agt_made(entry, current)
            cells = {}
            for label in labels:
                reason = entry.held if entry.held != "you turned it off" else None
                reason = reason or entry.stays.get(label)
                if label not in plan.plugins.by_label:
                    reason = reason or "this CLI does not install plugins"
                if label in entry.copies and label not in made:
                    reason = reason or "your original: agt never removes it"
                status = planner.HAS if label in entry.copies else planner.WAITING if label in entry.asks else \
                    planner.COPY if label in entry.writes else planner.SKIPPED
                cells[label] = cell(status, reason or "; ".join(entry.asks.get(label, [])), reason)
            add(entry.key, "plugins", entry.name, cells)
    if plan.instructions is not None:
        shared = plan.instructions
        copies = {copy.label: copy for copy in shared.copies}
        from agents_router.share.instructions import KEY
        add(KEY, "instructions", "instructions", {
            label: cell(copies[label].state if label in copies else planner.BLOCKED,
                        copies[label].problem or "" if label in copies else "",
                        "instruction files use share adopt and resolve, not per-item picks") for label in labels})
    return {"schema_version": 1, "enabled": enabled,
            "folders": [{"label": provider.label, "cli": provider.cli, "path": str(provider.folder)} for provider in plan.included],
            "items": sorted(items, key=lambda item: item["id"])}
