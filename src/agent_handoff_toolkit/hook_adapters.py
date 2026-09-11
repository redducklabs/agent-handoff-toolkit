"""Bounded host input and output translation for lifecycle enforcement.

Raw host IDs and content remain transient. The storage boundary replaces the
normalizer's one-way session identifier with the repository-secret HMAC.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata

from .hooks import HookExecution
from .lifecycle import (
    AffirmationResult,
    AuthorityCategory,
    DecisionKind,
    EnforcementMode,
    EventName,
    LifecycleDecision,
    LifecycleIssue,
    LifecycleMutation,
    NormalizedEvent,
    TerminalCandidate,
    _blocked_stop,
    classify_affirmation,
    evaluate_pre_tool,
    evaluate_stop,
    evaluate_user_prompt,
)
from .lifecycle_operations import (
    LifecycleService,
    canonical_record_path,
    parse_bootstrap_command,
)
from .lifecycle_storage import (
    LocalLifecycleStorage,
    StaleLifecycleState,
    _check_path,
    _directory_guard,
    _open_directory,
)
from .lineage import record_digest, validate_identifier
from .records import parse_markdown, render_terminal_response

MAX_INPUT_BYTES = 128 * 1024
MAX_REASON_BYTES = 1200
_READ_ONLY = {
    "claude": frozenset({"Read", "Glob", "Grep"}),
    "codex": frozenset({"view_image"}),
}
_SHELL = {"claude": frozenset({"Bash", "PowerShell"}), "codex": frozenset({"Bash"})}
_ALIASES = {
    "userpromptsubmit": EventName.USER_PROMPT_SUBMIT,
    "pretooluse": EventName.PRE_TOOL_USE,
    "stop": EventName.STOP,
}
_ACTIONS = {
    "AHK-PRE-ROOT": "Register, resume, join, or adopt using the current bootstrap challenge.",
    "AHK-STOP-WORK": "Perform the authorized action or render a valid successor record.",
    "AHK-STOP-ROOT": "Render the locked root and registered authorization evidence.",
    "AHK-STOP-SCOPE": "Restore the inherited ordered scope definitions.",
    "AHK-STOP-PREDECESSOR": "Render a successor of the live record with its exact source digest.",
    "AHK-STOP-RESPONSE": "Emit only render-terminal-response output for the candidate.",
    "AHK-STOP-DECISION": "Emit exactly the registered decision response.",
    "AHK-STOP-STALE": "Reload lifecycle inspect and rebuild against current revisions.",
    "AHK-HOOK-RUNTIME": "Repair lifecycle runtime or input; inspect state and retry.",
    "AHK-STOP-CIRCUIT": "A real external user turn is required before another attempt.",
}


def event_name(event: str) -> EventName:
    if not isinstance(event, str) or len(event) > 64:
        raise ValueError("invalid hook event")
    try:
        return _ALIASES[event.lower().replace("_", "").replace("-", "")]
    except KeyError as error:
        raise ValueError("invalid lifecycle event") from error


def _string(value, limit, *, multiline=False, empty=False, trimmed=True):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid hook text")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if (not value and not empty) or (trimmed and value != value.strip()):
        raise ValueError("hook text must be trimmed")
    if (
        any(
            not (multiline and char == "\n")
            and (
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
            )
            for char in value
        )
        or len(value.encode("utf-8")) > limit
    ):
        raise ValueError("unsafe or oversized hook text")
    return value


def _bounded_json(value, depth=0):
    if depth > 12:
        raise ValueError("hook object nesting exceeds bound")
    if isinstance(value, str):
        # Opaque host fields may contain surrounding whitespace, but never controls.
        _string(value, 16384, multiline=True, empty=True, trimmed=False)
        if len(value.encode("utf-8")) > 16384:
            raise ValueError("hook field exceeds bound")
    elif isinstance(value, Mapping):
        if len(value) > 128:
            raise ValueError("hook object exceeds bound")
        for key, item in value.items():
            _string(key, 128)
            _bounded_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise ValueError("hook array exceeds bound")
        for item in value:
            _bounded_json(item, depth + 1)
    elif value is not None and type(value) not in {bool, int, float}:
        raise ValueError("invalid hook value")
    if (
        depth == 0
        and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
        > MAX_INPUT_BYTES
    ):
        raise ValueError("hook input exceeds bound")


def decode_payload(raw: str) -> Mapping[str, object]:
    if (
        not isinstance(raw, str)
        or len(raw) > MAX_INPUT_BYTES
        or len(raw.encode("utf-8")) > MAX_INPUT_BYTES
    ):
        raise ValueError("hook input exceeds bound")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate hook key")
            result[key] = value
        return result

    payload = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be an object")
    _bounded_json(payload)
    return payload


def normalize_event(
    platform: str, event: str, payload: Mapping[str, object], repo_root: Path
) -> NormalizedEvent:
    if platform not in _READ_ONLY or not isinstance(payload, Mapping):
        raise ValueError("invalid host or payload")
    name = event_name(event)
    _bounded_json(payload)
    if (
        "hook_event_name" in payload
        and event_name(payload["hook_event_name"]) is not name
    ):
        raise ValueError("hook event mismatch")
    session_id = _string(payload.get("session_id"), 4096)
    cwd = Path(_string(payload.get("cwd"), 4096))
    root = Path(repo_root).resolve()
    if not cwd.is_absolute() or not cwd.resolve().is_relative_to(root):
        raise ValueError("hook cwd is outside repository")
    transcript = _string(payload.get("transcript_path"), 4096)
    if not Path(transcript).is_absolute():
        raise ValueError("transcript reference must be absolute")
    turn = payload.get("turn_id")
    if platform == "codex" or "turn_id" in payload:
        turn = validate_identifier(turn, label="turn reference")
    else:
        turn = "turn-" + secrets.token_hex(16)
    stop_active = payload.get("stop_hook_active", False)
    external = payload.get("external_user_turn", name is EventName.USER_PROMPT_SUBMIT)
    if type(stop_active) is not bool or type(external) is not bool:
        raise ValueError("hook flags must be boolean")
    message = user = tool = inputs = capability = None
    if name is EventName.STOP:
        if "stop_hook_active" not in payload:
            raise ValueError("Stop requires loop evidence")
        message = _string(payload.get("last_assistant_message"), 16384, multiline=True)
    elif name is EventName.USER_PROMPT_SUBMIT:
        user = _string(payload.get("prompt"), 16384, multiline=True)
        external = external and not stop_active
    else:
        tool = _string(payload.get("tool_name"), 128)
        inputs = payload.get("tool_input")
        if (
            not isinstance(inputs, Mapping)
            or len(json.dumps(inputs, ensure_ascii=False).encode()) > 4096
        ):
            raise ValueError("invalid tool input")
        capability = (
            "intrinsic-read-only"
            if tool in _READ_ONLY[platform]
            else "mutation-capable"
        )
    return NormalizedEvent(
        host=platform,
        event=name,
        session_key=hashlib.sha256(session_id.encode()).hexdigest(),
        turn_reference=turn,
        repository_root=root.as_posix(),
        transcript_reference=transcript,
        stop_hook_active=stop_active,
        latest_assistant_message=message,
        current_user_message=user,
        current_user_reference=turn if name is EventName.USER_PROMPT_SUBMIT else None,
        tool_name=tool,
        tool_input=inputs,
        tool_capability=capability,
        external_user_turn=external if name is EventName.USER_PROMPT_SUBMIT else False,
    )


def _reason(decision):
    parts = []
    for issue in decision.issues:
        code = issue.code if issue.code in _ACTIONS else "AHK-HOOK-RUNTIME"
        line = f"{code}: {_ACTIONS[code]}"
        if decision.reason.startswith("Same lifecycle issue"):
            line = f"{code}: Same issue; apply the prior corrective action."
        for label in ("expected", "actual"):
            value = getattr(issue, label)
            if value is not None and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value
            ):
                line += f" {label}={value}"
        path = issue.candidate_path
        if (
            path
            and len(path.encode()) <= 256
            and re.fullmatch(r"(?:[A-Z]:/|/)[A-Za-z0-9._/-]+\.md", path)
        ):
            line += f" candidate={path}"
        if len(("\n".join(parts + [line])).encode()) > MAX_REASON_BYTES:
            break
        parts.append(line)
    return "\n".join(parts) or "AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry."


def render_hook_execution(
    platform: str, event: EventName, decision: LifecycleDecision
) -> HookExecution:
    if platform not in _READ_ONLY or not isinstance(event, EventName):
        raise ValueError("invalid hook output target")
    if decision.kind is DecisionKind.ALLOW:
        return HookExecution()
    reason = _reason(decision)
    if decision.kind is DecisionKind.POLICY_FAILURE:
        warning = "AHK-STOP-CIRCUIT: Lifecycle policy failure; attempted completion is not compliant. A real external user turn is required."
        output = {"continue": False, "stopReason": warning, "systemMessage": warning}
    elif event is EventName.PRE_TOOL_USE:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    else:
        output = {"decision": "block", "reason": reason}
    return HookExecution(
        stdout=json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    )


def _issue(code):
    return LifecycleIssue(code, "Lifecycle check failed.", _ACTIONS[code])


def _commit(storage, raw_id, snapshot, mutation):
    return storage.compare_and_swap(
        raw_id,
        snapshot.chain.targeted_revision if snapshot.chain else 0,
        snapshot.session.targeted_revision,
        mutation,
    )


def _correction_hmac(secret, session, reason):
    binding = (
        f"correction\0{session.session_key}\0{session.correction_cycle_count}\0{reason}"
    )
    return hmac.new(secret, binding.encode(), hashlib.sha256).hexdigest()


def _read_record(path, root):
    """Open one contained regular source once; retain its exact original bytes."""
    canonical = canonical_record_path(path)
    if canonical != path:
        raise ValueError("record pointer must use forward slashes")
    target = Path(canonical)
    handoffs = root / "handoffs"
    if not target.is_relative_to(handoffs) or target.name.lower() == "readme.md":
        raise ValueError("candidate outside handoffs")
    # The same no-reparse directory guard used by private state pins Windows
    # ancestors; POSIX opens relative to a no-follow parent descriptor.
    with _directory_guard(target.parent):
        before = _check_path(target)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise ValueError("invalid record source")
        parent = None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            if os.name == "nt":
                descriptor = os.open(target, flags)
            else:
                parent = _open_directory(target.parent)
                descriptor = os.open(target.name, flags, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("record changed during open")
                raw = source.read(MAX_INPUT_BYTES + 1)
                after = _check_path(target)
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ):
                    raise ValueError("record changed during read")
        finally:
            if parent is not None:
                os.close(parent)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("record source exceeds bound")
    text = raw.decode("utf-8")
    return text, parse_markdown(text), record_digest(text)


def _candidate(event, snapshot, root):
    message = event.latest_assistant_message
    # Only renderer-owned labels and a sole final link can discover a record.
    matches = list(
        re.finditer(
            r"\[(Continuation handoff|Audit record \(not a handoff\))\]\(<([^<>\n]+)>\)",
            message,
        )
    )
    if not matches:
        if "Continue from handoff:" in message or "](" in message:
            raise ValueError("invalid candidate pointer")
        return None
    if (
        len(matches) != 1
        or matches[0].end() != len(message)
        or len(re.findall(r"\]\(", message)) != 1
    ):
        raise ValueError("ambiguous candidate pointer")
    path = matches[0].group(2)
    canonical_record_path(path)
    references = re.findall(r"^Continue from handoff: (.+)$", message, re.MULTILINE)
    if matches[0].group(1) == "Continuation handoff":
        if (
            references != [path]
            or "```text\nContinue from handoff: " + path + "\n" not in message
        ):
            raise ValueError("candidate block differs from final link")
    elif references:
        raise ValueError("audit cannot contain restart pointer")
    text, data, digest = _read_record(path, root)
    predecessor = predecessor_path = predecessor_digest = None
    reference = data.get("predecessor")
    if reference is not None:
        if not isinstance(reference, Mapping) or not isinstance(
            reference.get("path"), str
        ):
            raise ValueError("invalid predecessor pointer")
        predecessor_path = reference["path"]
        _, predecessor, predecessor_digest = _read_record(predecessor_path, root)
    proof = snapshot.chain.publication_evidence if snapshot.chain else None
    return TerminalCandidate(
        candidate=data,
        text=text,
        path=path,
        digest=digest,
        rendered_response=render_terminal_response(path, text),
        predecessor=predecessor,
        predecessor_path=predecessor_path,
        predecessor_source_digest=predecessor_digest,
        trusted_transition_hmac=proof.evidence_hmac if proof else None,
        expected_chain_revision=snapshot.chain.targeted_revision
        if snapshot.chain
        else None,
        expected_session_revision=snapshot.session.targeted_revision,
    )


def _pre_tool(event, snapshot, root):
    session = snapshot.session
    parsed = None
    if (
        session.mode is EnforcementMode.UNTRACKED
        and event.tool_name in _SHELL[event.host]
        and session.bootstrap_challenge
    ):
        runner = root / ".agent-handoff-toolkit" / "runner.py"
        command = event.tool_input.get("command")
        parsed = parse_bootstrap_command(command, runner, session.bootstrap_challenge)
        if parsed is not None:
            _check_path(runner)
            if (
                parsed.arguments["expected_session_revision"]
                != session.targeted_revision
            ):
                parsed = None
    decision = evaluate_pre_tool(event, snapshot, bootstrap_allowed=parsed is not None)
    if decision.kind is DecisionKind.BLOCK:
        decision = LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-PRE-ROOT"),))
    output = render_hook_execution(event.host, event.event, decision)
    if decision.kind is DecisionKind.BLOCK and session.bootstrap_challenge:
        prefix = "python .agent-handoff-toolkit/runner.py lifecycle "
        suffix = f" --expected-session-revision {session.targeted_revision}"
        operations = {
            "register-root": " --scope-id ROOT --scope-kind issue --scope-definition-b64 BASE64URL",
            "resume": " --record /ABSOLUTE/RECORD.md",
            "join": " --authorization-id AUTH --expected-chain-revision REV",
            "adopt-v1": " --record /ABSOLUTE/RECORD.md",
        }
        reason = (
            "AHK-PRE-ROOT: Fill the capitalized placeholders for one command; run from the repository root.\n"
            + "\n".join(
                prefix
                + operation
                + " --challenge "
                + session.bootstrap_challenge
                + arguments
                + suffix
                for operation, arguments in operations.items()
            )
        )
        if len(reason.encode()) > MAX_REASON_BYTES:
            raise ValueError("bootstrap corrective data exceeds bound")
        value = json.loads(output.stdout)
        value["hookSpecificOutput"]["permissionDecisionReason"] = reason
        output = HookExecution(stdout=json.dumps(value, separators=(",", ":")))
    return output


def _user_prompt(event, snapshot, storage, raw_id, root):
    session = snapshot.session
    pending = session.pending_correction_hmac
    if (
        event.external_user_turn
        and pending
        and session.correction_cycle_count > 0
        and hmac.compare_digest(
            pending,
            _correction_hmac(storage.secret, session, event.current_user_message),
        )
    ):
        _commit(
            storage,
            raw_id,
            snapshot,
            LifecycleMutation(
                replace(
                    session,
                    targeted_revision=session.targeted_revision + 1,
                    pending_correction_hmac=None,
                )
            ),
        )
        return HookExecution()
    if not event.external_user_turn:
        return HookExecution()
    if event.current_user_reference == session.current_external_user_turn_reference:
        return HookExecution()
    proposal = session.pending_transition_reference
    preceding = None
    if (
        proposal
        and proposal.status == "pending"
        and proposal.source_user_turn_reference
        == session.current_external_user_turn_reference
        and proposal.source_user_turn_reference is not None
        and event.current_user_reference != proposal.assistant_turn_reference
    ):
        preceding = proposal.assistant_turn_reference
    service = LifecycleService(storage, raw_id)
    updated = service.observe_user_turn(
        event,
        preceding_assistant_turn_reference=preceding,
        expected_chain_revision=snapshot.chain.targeted_revision
        if snapshot.chain
        else 0,
        expected_session_revision=session.targeted_revision,
    )
    if updated.session.pending_correction_hmac is not None:
        updated = _commit(
            storage,
            raw_id,
            updated,
            LifecycleMutation(
                replace(
                    updated.session,
                    targeted_revision=updated.session.targeted_revision + 1,
                    pending_correction_hmac=None,
                )
            ),
        )
    request = updated.session.pending_decision_reference
    if request is not None and _decision_resolved(event.current_user_message, request):
        decision = evaluate_user_prompt(event, updated, decision_resolved=True)
        updated = _commit(storage, raw_id, updated, decision.mutation)
    match = re.fullmatch(r"Continue from handoff: ([^\n]+)", event.current_user_message)
    if match and updated.session.mode is EnforcementMode.UNTRACKED:
        path = match.group(1)
        text, data, digest = _read_record(path, root)
        updated = service.resume(
            challenge=updated.session.bootstrap_challenge,
            record_path=path,
            record_text=text,
            record_metadata=data,
            record_digest=digest,
            expected_session_revision=updated.session.targeted_revision,
        )
    remaining_proposal = updated.session.pending_transition_reference
    if updated.session.pending_decision_reference or (
        remaining_proposal and remaining_proposal.status == "pending"
    ):
        context = "AHK-USER-CLARIFY: Clarify the pending decision or reissue the exact scope proposal for an adjacent response. No new authorization was granted."
        return HookExecution(
            stdout=json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context,
                    }
                },
                separators=(",", ":"),
            )
        )
    return HookExecution()


def _decision_resolved(message, request):
    classification = classify_affirmation(message)
    if request.category in {
        AuthorityCategory.EXTERNAL_EFFECT,
        AuthorityCategory.DESTRUCTIVE_OPERATION,
        AuthorityCategory.REPOSITORY_APPROVAL,
    }:
        return classification in {AffirmationResult.APPROVE, AffirmationResult.REJECT}
    # Free-form input is only mechanically proven when the response explicitly
    # names the blocked field and supplies a bounded concrete token. Unknown
    # natural-language answers remain pending for a clearer follow-up.
    match = re.fullmatch(
        re.escape(request.blocked_action_field)
        + r": ([A-Za-z0-9][A-Za-z0-9._/-]{0,127})",
        message,
    )
    return match is not None and match.group(1).casefold() not in {
        "unknown",
        "unsure",
        "maybe",
        "none",
        "null",
        "tbd",
    }


def run_lifecycle_hook(platform, name, raw, repo_root, storage=None):
    event_kind = event_name(name)
    snapshot = None
    raw_id = None
    try:
        payload = decode_payload(raw)
        root = Path(repo_root).resolve()
        raw_id = _string(payload.get("session_id"), 4096)
        if storage is None:
            state_root = os.environ.get("AHK_STATE_ROOT")
            storage = LocalLifecycleStorage(
                root, state_root=Path(state_root) if state_root else None
            )
        snapshot = storage.load_snapshot(raw_id)
        event = normalize_event(platform, name, payload, repo_root)
        event = replace(event, session_key=snapshot.session.session_key)
        if event_kind is EventName.PRE_TOOL_USE:
            return _pre_tool(event, snapshot, root)
        if event_kind is EventName.USER_PROMPT_SUBMIT:
            return _user_prompt(event, snapshot, storage, raw_id, root)
        if snapshot.session.correction_cycle_count >= 3:
            decision = _blocked_stop(snapshot, (_issue("AHK-STOP-CIRCUIT"),))
        else:
            candidate = None
            if snapshot.session.mode is EnforcementMode.TRACKED:
                candidate = _candidate(event, snapshot, root)
            decision = evaluate_stop(
                event, snapshot, candidate=candidate, decision_secret=storage.secret
            )
    except StaleLifecycleState:
        decision = LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-STOP-STALE"),))
    except Exception:
        issue = _issue("AHK-HOOK-RUNTIME")
        if (
            snapshot
            and snapshot.session.authorization_id
            and event_kind is EventName.STOP
        ):
            decision = _blocked_stop(snapshot, (issue,))
        else:
            decision = LifecycleDecision(DecisionKind.BLOCK, (issue,))
    return _publish_decision(platform, event_kind, decision, storage, raw_id, snapshot)


def _publish_decision(
    platform, event_kind, decision, storage, raw_id, snapshot, *, retry=True
):
    output = render_hook_execution(platform, event_kind, decision)
    if decision.mutation is not None:
        mutation = decision.mutation
        if decision.kind is not DecisionKind.ALLOW and snapshot.chain is not None:
            # Refresh only the observed revision for failure bookkeeping. The
            # chain, record pointer and authority remain untouched, under CAS.
            mutation = replace(
                mutation,
                session=replace(
                    mutation.session, chain_revision=snapshot.chain.targeted_revision
                ),
            )
        if decision.kind is DecisionKind.BLOCK and event_kind is EventName.STOP:
            reason = json.loads(output.stdout)["reason"]
            mutation = replace(
                mutation,
                session=replace(
                    mutation.session,
                    pending_correction_hmac=_correction_hmac(
                        storage.secret, mutation.session, reason
                    ),
                ),
            )
        else:
            mutation = replace(
                mutation,
                session=replace(mutation.session, pending_correction_hmac=None),
            )
        try:
            _commit(storage, raw_id, snapshot, mutation)
        except StaleLifecycleState:
            if retry and event_kind is EventName.STOP:
                try:
                    current = storage.load_snapshot(raw_id)
                    if current.session.authorization_id is not None:
                        blocked = _blocked_stop(current, (_issue("AHK-STOP-STALE"),))
                        return _publish_decision(
                            platform,
                            event_kind,
                            blocked,
                            storage,
                            raw_id,
                            current,
                            retry=False,
                        )
                except Exception:
                    return render_hook_execution(
                        platform,
                        event_kind,
                        LifecycleDecision(
                            DecisionKind.BLOCK, (_issue("AHK-HOOK-RUNTIME"),)
                        ),
                    )
            return render_hook_execution(
                platform,
                event_kind,
                LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-STOP-STALE"),)),
            )
        except Exception:
            return render_hook_execution(
                platform,
                event_kind,
                LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-HOOK-RUNTIME"),)),
            )
    return output
