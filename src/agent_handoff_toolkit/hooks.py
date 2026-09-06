"""Context-health policy and fail-open Claude Code/Codex hook adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
import uuid

MILESTONES = (50, 60, 70)
MAX_PATCH_BYTES = 64 * 1024

_PATCH_FILE_DIRECTIVE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$",
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
    normalized = path.strip().replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/") if part]
    if not parts or not parts[-1].endswith(".md") or "handoffs" not in parts:
        return False
    return not (parts[-1] == "readme.md" and len(parts) > 1 and parts[-2] == "handoffs")


def _claude_paths(payload: dict[str, Any]) -> tuple[str, ...]:
    if payload.get("tool_name") not in {"Write", "Edit", "MultiEdit"}:
        return ()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ()
    candidate = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(candidate, str) or not _is_handoff_path(candidate):
        return ()
    return (candidate,)


def _codex_paths(payload: dict[str, Any]) -> tuple[str, ...]:
    if payload.get("tool_name") != "apply_patch":
        return ()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ()
    patch = tool_input.get("command")
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return ()

    paths: list[str] = []
    for match in _PATCH_FILE_DIRECTIVE.finditer(patch):
        path = match.group(1).strip()
        if _is_handoff_path(path) and path not in paths:
            paths.append(path)
    return tuple(paths)


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


def _authoring_reminder(paths: tuple[str, ...], repo_root: Path) -> str:
    normalized = [path.replace("\\", "/") for path in paths]
    rendered_paths = "\n".join(f"- `{path}`" for path in normalized)
    validate_target = normalized[0]
    return (
        "Handoff authoring reminder:\n"
        f"{rendered_paths}\n\n"
        "Re-read `docs/contract.md` now. Resolve any question that could change "
        "the next session's first action, record remaining code at every active "
        "scope, reconcile live state, and preserve verification failures or "
        "not-run checks accurately. Validate the record explicitly with "
        f"`python -m agent_handoff_toolkit validate {validate_target}` before "
        "rendering the final response tail. "
        f"Repository root: `{Path(repo_root)}`."
    )


def run_hook(platform: str, event: str, raw: str, repo_root: Path) -> str:
    """Normalize a host event and return hook JSON, failing open on all errors."""
    try:
        if platform not in {"claude", "codex"}:
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

        paths = (
            _claude_paths(payload) if platform == "claude" else _codex_paths(payload)
        )
        if not paths:
            return ""
        return _hook_output(
            "PostToolUse",
            _authoring_reminder(paths, Path(repo_root)),
        )
    except Exception:
        return ""
