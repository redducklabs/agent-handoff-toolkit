"""Schema version 1 parsing, validation, and deterministic rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

SCHEMA_VERSION = 1
METADATA_OPEN = "<!-- agent-handoff-metadata"
METADATA_CLOSE = "-->"
AUDIT_SENTINEL = "> Audit record — not a handoff. Do not use this file to start or continue a session."

CONTINUATION_SECTIONS = (
    "Objective",
    "Authoritative references",
    "User decisions",
    "Repository state",
    "Completed work",
    "Verification evidence",
    "Incomplete work and risks",
    "Exact next action",
    "External effects",
    "Remaining code by active scope",
    "Next-session prompt",
)

AUDIT_SECTIONS = (
    "Completed objective",
    "Authoritative references",
    "User decisions",
    "Final repository state",
    "Completed work",
    "Verification evidence",
    "Known risks or separately tracked follow-ups",
    "External effects",
)

SCOPE_KINDS = frozenset({"unit", "issue", "phase", "epic", "rollout", "standalone"})
VERIFICATION_RESULTS = frozenset({"pass", "fail", "not-run"})
METADATA_RE = re.compile(
    r"<!-- agent-handoff-metadata\n(?P<json>.*?)\n-->",
    re.DOTALL,
)
HEADING_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class ValidationIssue:
    """A deterministic structural validation diagnostic."""

    code: str
    message: str


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message)


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_content(value: object) -> bool:
    if _is_nonempty_string(value):
        return True
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return bool(value) and all(_is_nonempty_string(item) for item in value)
    return False


def _expected_sections(record_type: object) -> tuple[str, ...] | None:
    if record_type == "continuation":
        return CONTINUATION_SECTIONS
    if record_type == "completion-audit":
        return AUDIT_SECTIONS
    return None


def _validate_timestamp(value: object) -> bool:
    if not _is_nonempty_string(value):
        return False
    candidate = str(value)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_scopes(data: Mapping[str, object]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    raw_scopes = data.get("active_scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        return [_issue("scope-list", "active_scopes must be a non-empty list")]

    scopes: list[Mapping[str, object]] = []
    required = (
        "scope_id",
        "scope_kind",
        "parent_scope_id",
        "highest_authorized",
        "remaining_work",
        "remaining_code",
        "remaining_code_detail",
        "status",
    )
    for index, raw_scope in enumerate(raw_scopes):
        if not isinstance(raw_scope, Mapping):
            issues.append(
                _issue("scope-field", f"active_scopes[{index}] must be an object")
            )
            continue
        scopes.append(raw_scope)
        missing = [field for field in required if field not in raw_scope]
        if missing:
            issues.append(
                _issue(
                    "scope-field",
                    f"active_scopes[{index}] is missing: {', '.join(missing)}",
                )
            )
            continue
        if not _is_nonempty_string(raw_scope.get("scope_id")):
            issues.append(
                _issue(
                    "scope-field", f"active_scopes[{index}].scope_id must be non-empty"
                )
            )
        if raw_scope.get("scope_kind") not in SCOPE_KINDS:
            issues.append(
                _issue(
                    "scope-kind",
                    f"active_scopes[{index}].scope_kind must be one of {sorted(SCOPE_KINDS)}",
                )
            )
        parent = raw_scope.get("parent_scope_id")
        if parent is not None and not _is_nonempty_string(parent):
            issues.append(
                _issue(
                    "scope-field",
                    f"active_scopes[{index}].parent_scope_id must be null or non-empty",
                )
            )
        for field in ("highest_authorized", "remaining_work", "remaining_code"):
            if not isinstance(raw_scope.get(field), bool):
                issues.append(
                    _issue(
                        "scope-field", f"active_scopes[{index}].{field} must be boolean"
                    )
                )
        for field in ("remaining_code_detail", "status"):
            if not _is_nonempty_string(raw_scope.get(field)):
                issues.append(
                    _issue(
                        "scope-field",
                        f"active_scopes[{index}].{field} must be non-empty",
                    )
                )

    valid_ids = [
        scope.get("scope_id")
        for scope in scopes
        if _is_nonempty_string(scope.get("scope_id"))
    ]
    if len(valid_ids) != len(set(valid_ids)):
        issues.append(_issue("scope-id", "scope_id values must be unique"))
    scope_by_id = {
        str(scope_id): scope
        for scope_id, scope in zip(
            valid_ids, [s for s in scopes if _is_nonempty_string(s.get("scope_id"))]
        )
    }

    highest = [scope for scope in scopes if scope.get("highest_authorized") is True]
    if len(highest) != 1:
        issues.append(
            _issue("scope-highest", "exactly one scope must be highest_authorized")
        )

    roots = [scope for scope in scopes if scope.get("parent_scope_id") is None]
    if len(roots) != 1 or (highest and roots and roots[0] is not highest[0]):
        issues.append(
            _issue(
                "scope-root",
                "the highest-authorized scope must be the only root and have no parent",
            )
        )

    for scope in scopes:
        scope_id = scope.get("scope_id")
        parent = scope.get("parent_scope_id")
        if parent is not None and parent not in scope_by_id:
            issues.append(
                _issue(
                    "scope-parent",
                    f"scope {scope_id!r} references unknown parent {parent!r}",
                )
            )

    for start in valid_ids:
        seen: set[str] = set()
        current: str | None = str(start)
        while current is not None and current in scope_by_id:
            if current in seen:
                issues.append(
                    _issue("scope-cycle", f"scope chain containing {start!r} is cyclic")
                )
                break
            seen.add(current)
            parent = scope_by_id[current].get("parent_scope_id")
            current = str(parent) if parent is not None else None

    if len(highest) == 1:
        expected_remaining = data.get("record_type") == "continuation"
        if highest[0].get("remaining_work") is not expected_remaining:
            state = "true" if expected_remaining else "false"
            issues.append(
                _issue(
                    "scope-state",
                    f"highest-authorized remaining_work must be {state} for this record type",
                )
            )

    return issues


def _validate_verification(data: Mapping[str, object]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    entries = data.get("verification")
    if not isinstance(entries, list):
        return [_issue("verification-list", "verification must be a list")]
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            issues.append(
                _issue("verification-entry", f"verification[{index}] must be an object")
            )
            continue
        if not _is_nonempty_string(entry.get("check")):
            issues.append(
                _issue(
                    "verification-check",
                    f"verification[{index}].check must be non-empty",
                )
            )
        result = entry.get("result")
        if result not in VERIFICATION_RESULTS:
            issues.append(
                _issue(
                    "verification-result",
                    f"verification[{index}].result must be pass, fail, or not-run",
                )
            )
        elif result == "not-run":
            if not _is_nonempty_string(entry.get("reason")):
                issues.append(
                    _issue(
                        "verification-reason",
                        f"verification[{index}] with not-run requires a reason",
                    )
                )
        elif not _is_nonempty_string(entry.get("evidence")):
            issues.append(
                _issue(
                    "verification-evidence",
                    f"verification[{index}] with {result} requires evidence",
                )
            )
    return issues


def _validate_type_fields(data: Mapping[str, object]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    record_type = data.get("record_type")
    if record_type == "continuation":
        forbidden = [
            field
            for field in ("authorization_basis", "completed_scope_id")
            if field in data
        ]
        if forbidden:
            issues.append(
                _issue(
                    "field-exclusion",
                    f"continuations must not contain: {', '.join(forbidden)}",
                )
            )
        gates = data.get("next_session_gates")
        if not isinstance(gates, list) or gates:
            issues.append(
                _issue("question-gate", "next_session_gates must be an empty list")
            )
        action = data.get("exact_action")
        if not isinstance(action, Mapping):
            issues.append(_issue("exact-action", "exact_action must be an object"))
        else:
            for field in ("action", "target", "constraints", "completion_condition"):
                if not _has_content(action.get(field)):
                    issues.append(
                        _issue(
                            "exact-action", f"exact_action.{field} must be non-empty"
                        )
                    )
        if not _is_nonempty_string(data.get("next_session_prompt")):
            issues.append(
                _issue("next-prompt", "next_session_prompt must be non-empty")
            )
    elif record_type == "completion-audit":
        forbidden = (
            "next_session_gates",
            "exact_action",
            "next_session_prompt",
        )
        present = [field for field in forbidden if field in data]
        if present:
            issues.append(
                _issue(
                    "field-exclusion",
                    f"completion audits must not contain: {', '.join(present)}",
                )
            )
        if not _is_nonempty_string(data.get("authorization_basis")):
            issues.append(
                _issue("authorization-basis", "authorization_basis must be non-empty")
            )
        completed_scope_id = data.get("completed_scope_id")
        highest_scope_ids = (
            [
                scope.get("scope_id")
                for scope in data.get("active_scopes", [])
                if isinstance(scope, Mapping)
                and scope.get("highest_authorized") is True
            ]
            if isinstance(data.get("active_scopes"), list)
            else []
        )
        if (
            not _is_nonempty_string(completed_scope_id)
            or len(highest_scope_ids) != 1
            or completed_scope_id != highest_scope_ids[0]
        ):
            issues.append(
                _issue(
                    "completed-scope",
                    "completed_scope_id must match the highest-authorized scope",
                )
            )
    return issues


def _validate_data(data: object) -> list[ValidationIssue]:
    if not isinstance(data, Mapping):
        return [_issue("metadata-type", "metadata must be a JSON object")]

    issues: list[ValidationIssue] = []
    if data.get("schema_version") != SCHEMA_VERSION or isinstance(
        data.get("schema_version"), bool
    ):
        issues.append(_issue("schema-version", "schema_version must be 1"))
    record_type = data.get("record_type")
    expected_sections = _expected_sections(record_type)
    if expected_sections is None:
        issues.append(
            _issue(
                "record-type",
                "record_type must be continuation or completion-audit",
            )
        )
    if not _validate_timestamp(data.get("timestamp")):
        issues.append(
            _issue("timestamp", "timestamp must be an ISO 8601 value with a timezone")
        )

    issues.extend(_validate_scopes(data))
    issues.extend(_validate_verification(data))
    issues.extend(_validate_type_fields(data))

    sections = data.get("sections")
    if expected_sections is not None:
        if (
            not isinstance(sections, Mapping)
            or len(sections) != len(expected_sections)
            or set(sections.keys()) != set(expected_sections)
        ):
            issues.append(
                _issue(
                    "section-shape",
                    f"sections must contain exactly: {', '.join(expected_sections)}",
                )
            )
        elif any(
            not _is_nonempty_string(sections.get(name)) for name in expected_sections
        ):
            issues.append(
                _issue("section-content", "every required section must be non-empty")
            )
    return issues


def _format_issues(issues: Sequence[ValidationIssue]) -> str:
    return "; ".join(f"{issue.code}: {issue.message}" for issue in issues)


def _extract_markdown(text: str) -> tuple[dict[str, Any] | None, list[ValidationIssue]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = METADATA_RE.search(normalized)
    if match is None:
        return None, [
            _issue("metadata-missing", "agent-handoff metadata comment is missing")
        ]

    issues: list[ValidationIssue] = []
    try:
        metadata = json.loads(match.group("json"))
    except json.JSONDecodeError as error:
        return None, [
            _issue(
                "metadata-json",
                f"metadata JSON is invalid at line {error.lineno}, column {error.colno}",
            )
        ]
    if not isinstance(metadata, dict):
        return None, [_issue("metadata-type", "metadata must be a JSON object")]

    record_type = metadata.get("record_type")
    if record_type == "continuation":
        if match.start() != 0:
            issues.append(
                _issue(
                    "record-preamble",
                    "a continuation must begin with its metadata comment",
                )
            )
        expected_title = "# Session continuation"
    elif record_type == "completion-audit":
        if normalized[: match.start()] != AUDIT_SENTINEL + "\n\n":
            issues.append(
                _issue(
                    "audit-sentinel",
                    "a completion audit must begin with the exact sentinel",
                )
            )
        expected_title = "# Completion audit"
    else:
        expected_title = None

    heading_matches = list(HEADING_RE.finditer(normalized))
    actual_headings = tuple(item.group("title") for item in heading_matches)
    expected_headings = _expected_sections(record_type)
    if expected_headings is not None and actual_headings != expected_headings:
        issues.append(
            _issue(
                "section-shape",
                f"level-two sections must be exactly, in order: {', '.join(expected_headings)}",
            )
        )

    if heading_matches and expected_title is not None:
        title_area = normalized[match.end() : heading_matches[0].start()].strip()
        if title_area != expected_title:
            issues.append(
                _issue("record-title", f"record title must be {expected_title!r}")
            )

    sections: dict[str, str] = {}
    for index, heading in enumerate(heading_matches):
        start = heading.end()
        end = (
            heading_matches[index + 1].start()
            if index + 1 < len(heading_matches)
            else len(normalized)
        )
        sections[heading.group("title")] = normalized[start:end].strip()

    data = dict(metadata)
    data["sections"] = sections
    issues.extend(_validate_data(data))
    return data, issues


def validate_markdown(text: str) -> list[ValidationIssue]:
    """Validate structural conformance without claiming factual correctness."""

    _, issues = _extract_markdown(text)
    return issues


def parse_markdown(text: str) -> dict[str, Any]:
    """Parse a valid record into its metadata and narrative sections."""

    data, issues = _extract_markdown(text)
    if issues:
        raise ValueError(_format_issues(issues))
    if data is None:  # Defensive: every no-data path above supplies an issue.
        raise ValueError("metadata-missing: agent-handoff metadata comment is missing")
    return data


def render_record(data: Mapping[str, object]) -> str:
    """Render schema data into canonical Markdown, or fail on invalid input."""

    issues = _validate_data(data)
    if issues:
        raise ValueError(_format_issues(issues))

    record_type = str(data["record_type"])
    sections = data["sections"]
    assert isinstance(sections, Mapping)
    expected_sections = _expected_sections(record_type)
    assert expected_sections is not None

    metadata = {key: value for key, value in data.items() if key != "sections"}
    metadata_json = json.dumps(
        metadata,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    metadata_block = f"{METADATA_OPEN}\n{metadata_json}\n{METADATA_CLOSE}"
    title = (
        "# Session continuation"
        if record_type == "continuation"
        else "# Completion audit"
    )
    preamble = metadata_block
    if record_type == "completion-audit":
        preamble = f"{AUDIT_SENTINEL}\n\n{metadata_block}"

    rendered_sections = "\n\n".join(
        f"## {name}\n\n{str(sections[name]).strip()}" for name in expected_sections
    )
    return f"{preamble}\n\n{title}\n\n{rendered_sections}\n"


def _absolute_markdown_path(record_path: str | os.PathLike[str]) -> str:
    raw = os.fspath(record_path)
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw)
    if windows_path.is_absolute():
        return windows_path.as_posix()
    if posix_path.is_absolute():
        return str(posix_path)
    return Path(raw).resolve().as_posix()


def render_tail(record_path: str | os.PathLike[str], text: str) -> str:
    """Render the exact response tail for a structurally valid record."""

    data = parse_markdown(text)
    link_path = _absolute_markdown_path(record_path)
    if data["record_type"] == "continuation":
        prompt = str(data["next_session_prompt"]).strip()
        longest_run = max((len(run) for run in re.findall(r"`+", prompt)), default=0)
        fence = "`" * max(3, longest_run + 1)
        return (
            f"{fence}text\n{prompt}\n{fence}\n\n"
            f"[Continuation handoff](<{link_path}>)"
        )
    return f"[Audit record (not a handoff)](<{link_path}>)"
