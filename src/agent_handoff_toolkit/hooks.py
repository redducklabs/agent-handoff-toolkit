"""Advisory context hooks and fail-closed lifecycle dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING
import unicodedata
from urllib.parse import quote

if TYPE_CHECKING:
    from .lifecycle_storage import LocalLifecycleStorage

MAX_HOOK_INPUT_BYTES = 128 * 1024
MAX_PATCH_BYTES = 64 * 1024

_PATCH_FILE_DIRECTIVE = re.compile(
    r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$",
    re.MULTILINE,
)

# Every session in a consumer repository pays for this reminder, so it points at
# the `agent-handoff` skill instead of directing sessions that never touch a
# record to read the whole contract.
_SESSION_START_REMINDER = (
    "Use the `agent-handoff` skill for any handoff record; do not search or "
    "inspect deprecated legacy handoffs."
)


def hook_runtime_reason(stage: str, error: BaseException | None = None) -> str:
    """Render an opaque runtime fault with its failing stage and exception.

    The detail matches the `failed=` convention every other denial already
    uses. Both values are bounded identifiers fixed in source - a stage label
    and a Python class name - so no host content reaches the host through here.
    """

    details = ["stage:" + stage]
    name = type(error).__name__ if error is not None else ""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        details.append("error:" + name.replace("_", "-"))
    return "AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry. failed=" + ",".join(
        details
    )


@dataclass(frozen=True)
class HookExecution:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass(frozen=True)
class _RecordChange:
    operation: str
    path: str


def _is_handoff_path(path: str) -> bool:
    if not path or any(unicodedata.category(char).startswith("C") for char in path):
        return False
    normalized = path.replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/") if part]
    if not parts or not parts[-1].endswith(".md") or "handoffs" not in parts:
        return False
    return not (parts[-1] == "readme.md" and len(parts) > 1 and parts[-2] == "handoffs")


def _claude_changes(payload: dict[str, Any]) -> tuple[_RecordChange, ...]:
    if payload.get("tool_name") not in {
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
    }:
        return ()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ()
    # NotebookEdit names its target `notebook_path`; the others use
    # `file_path`. `_is_handoff_path` still decides what counts as a record.
    candidate = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
    )
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
            {"Add": "Added", "Update": "Updated", "Delete": "Deleted"}[operation],
            path,
        )
        if _is_handoff_path(path) and change not in changes:
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
    authored = tuple(change for change in changes if change.operation != "Deleted")
    deleted = tuple(change for change in changes if change.operation == "Deleted")
    paragraphs = ["Record change reminder:\n" + rendered_paths]

    if authored:
        # The renderer is the only source of the terminal response, so this
        # reminder names the record-type decision and leaves the generated tail,
        # the contract, and the authoring gates to the `agent-handoff` skill.
        paragraphs.append(
            "Finish the record through the `agent-handoff` skill. A continuation "
            "needs remaining authorized work and an executable first action; a "
            "completion audit is evidence and must not contain a restart action, "
            "exact next action, or next-session prompt."
        )
    if deleted:
        paragraphs.append(
            "Ensure each deletion is intentional and that any still-authorized "
            "work retains a valid current continuation."
        )

    if authored:
        validation = (
            "Run the explicit `validate` subcommand separately for every added, "
            "updated, or edited record listed above."
        )
        if deleted:
            validation += " Do not validate deleted paths."
        paragraphs.append(validation)
    paragraphs.append(
        "The listed paths are percent-encoded for safe display; use the actual "
        "tool-call path with quoting appropriate to the current shell."
    )
    return "\n\n".join(paragraphs)


def _raw_input_is_bounded(raw: str) -> bool:
    if not isinstance(raw, str) or len(raw) > MAX_HOOK_INPUT_BYTES:
        return False
    return len(raw.encode("utf-8")) <= MAX_HOOK_INPUT_BYTES


def _session_end_notice(
    payload: dict[str, Any], repo_root: Path, storage: object = None
) -> str:
    """Tell the user when tracked work closed on a progress line, not a handoff.

    A tracked session may end a turn on the canonical progress line instead of
    authoring a record. That is a pause, not a conclusion, so a session that
    closes on one leaves the epic with no record of where it stopped. This is
    the report that says so. It is informational and fails open: a missing or
    unreadable state file, or any other fault, produces no message at all.
    """

    try:
        import os

        from .lifecycle import EnforcementMode
        from .lifecycle_storage import LocalLifecycleStorage

        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return ""
        if storage is None:
            state_root = os.environ.get("AHK_STATE_ROOT")
            storage = LocalLifecycleStorage(
                Path(repo_root), state_root=Path(state_root) if state_root else None
            )
        snapshot = storage.load_snapshot(session_id)
        session, chain = snapshot.session, snapshot.chain
        if (
            session.mode is not EnforcementMode.TRACKED
            or not session.last_stop_was_progress
            or chain is None
        ):
            return ""
        reference = chain.current_record_reference
        title = chain.root_title or chain.locked_root_id
        if reference is None:
            resume = "by declaring the goal again with: Track: " + title
        else:
            resume = (
                "by starting a new session with: Continue from handoff: "
                + reference.path
            )
        return json.dumps(
            {
                "systemMessage": (
                    f'Tracked work "{title}" ended without a final handoff. '
                    f"Resume it {resume}"
                )
            },
            ensure_ascii=False,
        )
    except Exception:
        # Informational only; a fault here must never disturb the session end.
        return ""


def _run_advisory_hook(
    platform: str, event: str, raw: str, repo_root: Path, storage: object = None
) -> str:
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

        if normalized_event == "sessionend":
            normalized_event = "session-end"

        if normalized_event == "session-start":
            return _hook_output("SessionStart", _SESSION_START_REMINDER)
        if normalized_event == "session-end":
            return _session_end_notice(payload, repo_root, storage)
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


def run_hook(
    platform: str,
    event: str,
    raw: str,
    repo_root: Path,
    storage: LocalLifecycleStorage | None = None,
) -> HookExecution:
    """Keep informational errors open and lifecycle errors visibly blocking."""
    name = (
        event.lower().replace("-", "").replace("_", "")
        if isinstance(event, str)
        else ""
    )
    if name not in {"userpromptsubmit", "pretooluse", "stop"}:
        return HookExecution(
            stdout=_run_advisory_hook(platform, event, raw, repo_root, storage)
        )
    try:
        from .hook_adapters import run_lifecycle_hook

        return run_lifecycle_hook(platform, event, raw, repo_root, storage)
    except Exception as error:
        # Reached only when the owned adapter or lifecycle modules cannot load,
        # which is install corruption rather than a transient fault: the module
        # that would report whether this session declared tracked work is the
        # one failing, so this boundary keeps failing closed. It still names the
        # stage and the exception class, both fixed identifiers - never the
        # message, which could carry host content.
        reason = hook_runtime_reason("load-adapter", error)
        if name == "pretooluse":
            value = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        else:
            value = {"decision": "block", "reason": reason}
        return HookExecution(stdout=json.dumps(value, separators=(",", ":")))
