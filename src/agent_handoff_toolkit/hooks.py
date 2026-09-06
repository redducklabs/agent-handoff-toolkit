"""Context-health policy and fail-open Claude Code/Codex hook adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
import unicodedata
from urllib.parse import quote
import uuid

MILESTONES = (50, 60, 70)
MAX_HOOK_INPUT_BYTES = 128 * 1024
MAX_PATCH_BYTES = 64 * 1024

_PATCH_FILE_DIRECTIVE = re.compile(
    r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$",
    re.MULTILINE,
)

_MILESTONE_MESSAGES = {
    50: (
        "Context is 50% used. Identify the next natural stopping point and "
        "keep the current workstream bounded."
    ),
    60: (
        "Context is 60% used. Finish and verify the current coherent unit, "
        "resolve every question that gates the next action, then create or "
        "update the continuation."
    ),
    70: (
        "Context is 70% used. Do not begin another substantial unit before "
        "creating or updating the continuation and rendering its required "
        "copy/paste response tail."
    ),
}

_SESSION_START_REMINDER = (
    "Read `docs/contract.md` before continuing. Inspect the repository's current "
    "continuation, if one exists, reconcile it with live state, and do not repeat "
    "completed work."
)


@dataclass(frozen=True)
class _RecordChange:
    operation: str
    path: str


def select_milestone(percentage: float) -> int | None:
    """Return the highest crossed context milestone."""
    if (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not math.isfinite(percentage)
        or percentage < 0
        or percentage > 100
    ):
        raise ValueError("Context percentage must be between 0 and 100.")
    for milestone in reversed(MILESTONES):
        if percentage >= milestone:
            return milestone
    return None


def _state_path(state_dir: Path, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return state_dir / f"{key}.json"


def _read_milestone(path: Path) -> int | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("milestone") not in MILESTONES:
        return None
    return int(value["milestone"])


def _write_milestone(path: Path, milestone: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({"milestone": milestone}, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def observe_context(
    session_id: str,
    percentage: float,
    state_dir: Path,
) -> str | None:
    """Record an explicit observation and return a newly due reminder.

    A drop below the prior milestone is treated as compaction. Observing a value
    below 50 clears the state completely, so all milestones can fire again.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("A non-empty session identifier is required.")
    milestone = select_milestone(percentage)
    path = _state_path(Path(state_dir), session_id)
    prior = _read_milestone(path)

    if milestone is None:
        path.unlink(missing_ok=True)
        return None
    if prior == milestone:
        return None

    _write_milestone(path, milestone)
    return _MILESTONE_MESSAGES[milestone]


def _is_handoff_path(path: str) -> bool:
    if not path or any(unicodedata.category(char).startswith("C") for char in path):
        return False
    normalized = path.replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/") if part]
    if not parts or not parts[-1].endswith(".md") or "handoffs" not in parts:
        return False
    return not (parts[-1] == "readme.md" and len(parts) > 1 and parts[-2] == "handoffs")


def _claude_changes(payload: dict[str, Any]) -> tuple[_RecordChange, ...]:
    if payload.get("tool_name") not in {"Write", "Edit", "MultiEdit"}:
        return ()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ()
    candidate = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(candidate, str) or not _is_handoff_path(candidate):
        return ()
    return (_RecordChange("Edited", candidate),)


def _codex_changes(payload: dict[str, Any]) -> tuple[_RecordChange, ...]:
    if payload.get("tool_name") != "apply_patch":
        return ()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ()
    patch = tool_input.get("command")
    if (
        not isinstance(patch, str)
        or len(patch) > MAX_PATCH_BYTES
        or len(patch.encode("utf-8")) > MAX_PATCH_BYTES
    ):
        return ()

    changes: list[_RecordChange] = []
    for match in _PATCH_FILE_DIRECTIVE.finditer(patch):
        operation = match.group(1)
        path = match.group(2).strip()
        change = _RecordChange(
            "Added" if operation == "Add" else "Updated",
            path,
        )
        if operation != "Delete" and _is_handoff_path(path) and change not in changes:
            changes.append(change)
    return tuple(changes)


def _hook_output(event_name: str, context: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        },
        ensure_ascii=False,
    )


def _display_path(path: str) -> str:
    """Return a reversible display form without Markdown or shell delimiters."""
    return quote(path.replace("\\", "/"), safe="/:._-")


def _authoring_reminder(changes: tuple[_RecordChange, ...]) -> str:
    rendered_paths = "\n".join(
        f"- {change.operation}: {_display_path(change.path)}" for change in changes
    )
    return (
        "Record authoring reminder:\n"
        f"{rendered_paths}\n\n"
        "Determine the record type before finishing it. Continuation: resolve "
        "every question that gates the next session's first action, record "
        "remaining code at every active scope, and render the required response "
        "tail. Completion audit: this is not a handoff and must not contain a "
        "restart action, exact next action, or next-session prompt.\n\n"
        "Re-read `docs/contract.md`, reconcile live state, and preserve failed or "
        "not-run verification accurately. Run the explicit `validate` subcommand "
        "separately for every added, updated, or edited record listed above; "
        "the listed paths are percent-encoded for safe display, so use the actual "
        "tool-call path with quoting appropriate to the current shell."
    )


def _raw_input_is_bounded(raw: str) -> bool:
    if not isinstance(raw, str) or len(raw) > MAX_HOOK_INPUT_BYTES:
        return False
    return len(raw.encode("utf-8")) <= MAX_HOOK_INPUT_BYTES


def run_hook(platform: str, event: str, raw: str, repo_root: Path) -> str:
    """Normalize a host event and return hook JSON, failing open on all errors.

    ``repo_root`` is retained at the adapter boundary for future local contract
    discovery; untrusted payload paths are never resolved or opened here.
    """
    try:
        if platform not in {"claude", "codex"}:
            return ""
        if not _raw_input_is_bounded(raw):
            return ""
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            return ""

        normalized_event = event.strip().lower().replace("_", "-")
        if normalized_event == "sessionstart":
            normalized_event = "session-start"
        if normalized_event == "posttooluse":
            normalized_event = "post-tool-use"

        if normalized_event == "session-start":
            return _hook_output("SessionStart", _SESSION_START_REMINDER)
        if normalized_event != "post-tool-use":
            return ""

        changes = (
            _claude_changes(payload)
            if platform == "claude"
            else _codex_changes(payload)
        )
        if not changes:
            return ""
        return _hook_output(
            "PostToolUse",
            _authoring_reminder(changes),
        )
    except Exception:
        return ""
