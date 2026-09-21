"""Bounded host input and output translation for lifecycle enforcement.

Raw host IDs and content remain transient. The storage boundary replaces the
normalizer's one-way session identifier with the repository-secret HMAC.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
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
    _UNGATED,
    classify_affirmation,
    evaluate_stop,
    evaluate_user_prompt,
)
from .lifecycle_operations import (
    SCOPE_KINDS,
    LifecycleService,
    _charset_code,
    canonical_record_path,
    decode_scope_definition,
    parse_bootstrap_command,
)
from .lifecycle_storage import (
    LocalLifecycleStorage,
    StaleLifecycleState,
    _check_path,
    _directory_guard,
    _open_directory,
)
from .lineage import (
    canonical_json_bytes,
    record_digest,
    scope_definition_digest,
    validate_identifier,
    validate_scope_definition,
)
from .records import parse_markdown, render_terminal_response
from .repository_state import worktree_digest, worktree_state

# One bound, on the whole hook input. A tool call carries the work's payload -
# a written file, a pasted log - and that size belongs to the work, not to the
# lifecycle. The structural bounds below exist only to stop pathological
# parsing; they are set where pathological begins, not where ordinary usage is.
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_REASON_BYTES = 1200
_MAX_DEPTH = 64
_MAX_ITEMS = 4096
_MAX_KEYS = 1024
_READ_ONLY = {
    "claude": frozenset({"Read", "Glob", "Grep"}),
    "codex": frozenset({"view_image"}),
}
_SHELL = {"claude": frozenset({"Bash", "PowerShell"}), "codex": frozenset({"Bash"})}
# Lifecycle subcommands that carry no session key, challenge or revision, and
# so have nothing the control interception could bind into them.
_UNBOUND_CONTROL = frozenset({"doctor"})
# Tools that write repository files. Shell is deliberately absent: classifying
# a command as read-only or not is fragile, and making `git status` an advisory
# trigger would defeat the purpose. A missed trigger costs an advisory, not a
# guarantee - the Stop note is the backstop and reads the worktree directly.
_WRITE_TOOLS = {
    "claude": frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"}),
    "codex": frozenset({"apply_patch"}),
}
_ALIASES = {
    "userpromptsubmit": EventName.USER_PROMPT_SUBMIT,
    "pretooluse": EventName.PRE_TOOL_USE,
    "stop": EventName.STOP,
}
_ACTIONS = {
    "AHK-PRE-ROOT": "Register, resume, join, or adopt using the current bootstrap challenge.",
    "AHK-CONTROL-BINDING": "Use the current derived session capability and revision.",
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
    """Guard the shape of hook input, never its content.

    Strings pass through untouched. Host content is opaque here: the fields the
    lifecycle parses are each validated where they are read, and no host content
    ever reaches hook output, so inspecting the rest rejects real work and
    proves nothing.
    """

    if depth > _MAX_DEPTH:
        raise ValueError("hook object nesting exceeds bound")
    if isinstance(value, str):
        pass
    elif isinstance(value, Mapping):
        if len(value) > _MAX_KEYS:
            raise ValueError("hook object exceeds bound")
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 256:
                raise ValueError("invalid hook key")
            _bounded_json(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise ValueError("hook array exceeds bound")
        for item in value:
            _bounded_json(item, depth + 1)
    elif value is not None and type(value) not in {bool, int, float}:
        raise ValueError("invalid hook value")


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
    return payload


def _turn_content(value):
    """Read host-reported turn content without failing the hook.

    The host reports what the turn produced: null when a turn ended on a tool
    call, empty when it emitted no text, otherwise prose or a pasted log with
    whatever spacing and characters it carries. None of that is a runtime
    fault. Treating it as one blocks the only hook that can end a turn, and on
    UserPromptSubmit it rejects the user's own message. Content that says
    nothing reads as absent, which every downstream check already handles.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid hook text")
    # Trailing newlines are normalized away with the line endings: the CLI
    # prints the rendered response followed by one, so a host reporting what
    # it printed differs from the renderer by that byte alone. Nothing else is
    # trimmed, so the body stays byte-exact.
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return normalized if normalized.strip() else None


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
    # The host reports where the transcript is. Absent, null or blank means it
    # has none to report - a turn that produced no transcript, or a host that
    # does not carry one - and that is not a runtime fault. The reference is
    # recorded metadata that no lifecycle check consumes, so an empty report
    # reads as absent. Only a wrong type or a relative path remains a fault.
    transcript = payload.get("transcript_path")
    if transcript is not None:
        transcript = _string(transcript, 4096, empty=True, trimmed=False)
        transcript = transcript if transcript.strip() else None
    if transcript is not None and not Path(transcript).is_absolute():
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
        message = _turn_content(payload.get("last_assistant_message"))
    elif name is EventName.USER_PROMPT_SUBMIT:
        user = _turn_content(payload.get("prompt"))
        external = external and not stop_active
    else:
        tool = _string(payload.get("tool_name"), 128)
        inputs = payload.get("tool_input")
        # The payload the host is acting on. Only the shell command is read,
        # and it is bounded where it is parsed; the rest stays opaque, under
        # the single bound applied to the whole hook input.
        if not isinstance(inputs, Mapping):
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
        # Closed-vocabulary detail: the validator's own codes name which checks
        # failed. Like expected/actual, each is echoed only when it matches the
        # identifier pattern, so no free text can reach the host through here.
        # It is appended only when the whole line still fits, so naming the
        # failed checks can never cost another issue its code or its action.
        details = ",".join(
            [
                detail
                for detail in getattr(issue, "detail_codes", ())
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", str(detail))
            ][:4]
        )
        detailed = f"{line} failed={details}" if details else line
        for candidate_line in (detailed, line):
            if len(("\n".join(parts + [candidate_line])).encode()) <= MAX_REASON_BYTES:
                parts.append(candidate_line)
                break
        else:
            break
    return "\n".join(parts) or "AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry."


def render_hook_execution(
    platform: str, event: EventName, decision: LifecycleDecision
) -> HookExecution:
    if platform not in _READ_ONLY or not isinstance(event, EventName):
        raise ValueError("invalid hook output target")
    if decision.kind is DecisionKind.ALLOW:
        return HookExecution()
    reason = _reason(decision)
    if event is EventName.USER_PROMPT_SUBMIT:
        return _prompt_notice(decision, reason)
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


def _runtime_issue(stage, error=None):
    """Name the failing stage and exception class on an opaque runtime fault.

    Every other denial names the checks that failed after `failed=`; this one
    discarded the exception, leaving a consumer no way to tell a malformed
    payload from unreachable state. Both values are bounded identifiers fixed
    in source - a stage label and a Python class name - so no host content can
    reach the feedback channel through here.
    """

    details = ["stage:" + stage]
    name = type(error).__name__ if error is not None else ""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        details.append("error:" + name.replace("_", "-"))
    number = getattr(error, "errno", None)
    if type(number) is int:
        details.append("errno:" + str(number))
    return LifecycleIssue(
        "AHK-HOOK-RUNTIME",
        "Lifecycle check failed.",
        _ACTIONS["AHK-HOOK-RUNTIME"],
        detail_codes=tuple(details),
    )


def _prompt_notice(decision, reason):
    """Deliver the user's message and say what failed, deciding nothing.

    A hook may decide only where the party it blocks can act on the feedback.
    `PreToolUse` denies a tool call and `Stop` refuses a turn ending, and in
    both the agent is still running and can correct. Blocking
    `UserPromptSubmit` erases the user's message and starts no turn, so nobody
    remains who could act on the reason string - and the session could not
    recover, because the state bookkeeping that would clear the condition runs
    only on a decision carrying a mutation, and no turn ever began to produce
    one. A peer session publishing a record was enough to silence every other
    session on its chain, permanently.

    Nothing is given up by refusing to decide here. `evaluate_user_prompt`
    returns `ALLOW` on every path; this event has no policy denial to lose.
    The agent-addressed detail goes to `additionalContext` where the model can
    act on it, and one sentence goes to `systemMessage` so the user knows the
    toolkit reported something and their turn continued anyway.
    """

    codes = ", ".join(
        dict.fromkeys(
            issue.code if issue.code in _ACTIONS else "AHK-HOOK-RUNTIME"
            for issue in decision.issues
        )
    )
    sentence = (
        f"Agent handoff toolkit reported {codes}; this prompt was delivered "
        "and the session continues."
        if codes
        else "Agent handoff toolkit reported an issue; this prompt was "
        "delivered and the session continues."
    )
    return HookExecution(
        stdout=json.dumps(
            {
                "systemMessage": sentence,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": reason,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _runtime_advisory(issue):
    """Report a malfunction without deciding anything.

    A runtime fault is not a policy decision. Emitting a bare top-level
    `systemMessage` returns no permission decision and no stop decision at all,
    so the host behaves exactly as it would with no hook installed while the
    fault stays visible and diagnosable.
    """

    return HookExecution(
        stdout=json.dumps(
            {"systemMessage": _reason(LifecycleDecision(DecisionKind.BLOCK, (issue,)))},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _declared_tracked(snapshot):
    """Report whether the session opted in to enforcement.

    An unknown mode is not tracked: state that could not be read cannot show
    that a session declared anything, and a session that declared nothing is
    ungated by construction.
    """

    return snapshot is not None and snapshot.session.mode not in _UNGATED


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


def _read_record(path, root, *, expected_digest=None):
    """Open one contained regular source once; retain its exact original bytes.

    A chain crosses checkouts. The same repository is a worktree here, a
    different worktree there and `/mnt/d/...` under WSL, so a path recorded in
    one of them names a file that does not exist in another, and the record it
    names is sitting in this checkout's `handoffs/` under the same name. When
    the caller already knows the digest it expects, a path outside this
    checkout is retried against that basename and accepted only when the file
    there hashes to exactly what was expected. The digest is the evidence; the
    path is a locator.
    """

    canonical = canonical_record_path(path)
    if canonical != path:
        raise ValueError("record pointer must use forward slashes")
    target = Path(canonical)
    handoffs = root / "handoffs"
    if expected_digest is not None and not target.is_relative_to(handoffs):
        # Read the record of that name in this checkout and report its real
        # digest. Whether it is the right record is decided by the digest
        # comparisons the caller already makes, which is where the evidence
        # belongs: a mismatch is a policy failure to correct, not a
        # malfunction of the toolkit.
        return _read_record((handoffs / target.name).as_posix(), root)
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
    if message is None:
        # The turn produced no usable final text, so it offers no candidate.
        # The caller blocks on the absent terminal record, not on this.
        return None
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
    # Trailing whitespace does not make a pointer ambiguous. Raising here
    # reports a malfunction, and a response that differs from the renderer by
    # whitespace is a formatting mismatch the model can correct - which is
    # what AHK-STOP-RESPONSE tells it, once the candidate is discovered.
    if (
        len(matches) != 1
        or matches[0].end() != len(message.rstrip())
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
        declared_digest = reference.get("sha256")
        _, predecessor, predecessor_digest = _read_record(
            predecessor_path,
            root,
            expected_digest=declared_digest
            if isinstance(declared_digest, str)
            else None,
        )
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


def _control_command(runner, session, capability, operation, fields=()):
    prefix = f"python {runner.as_posix()} lifecycle {operation} --session-key {session.session_key} --challenge {capability}"
    return (
        prefix
        + "".join(f" --{key} {value}" for key, value in fields)
        + f" --expected-session-revision {session.targeted_revision}"
    )


def _repair_bootstrap(command, runner, session, capability, reasons=None):
    def reject(code):
        if reasons is not None:
            reasons.append(code)
        return None

    if not isinstance(command, str):
        return reject("command-type")
    if len(command) > 8192:
        return reject("command-length")
    if re.fullmatch(r"[A-Za-z0-9._:/ -]+", command) is None:
        return reject(_charset_code(command))
    tokens = command.split(" ")
    if len(tokens) < 4 or tokens[0] != "python" or tokens[2] != "lifecycle":
        return reject("command-shape")
    tokens[1] = runner.as_posix()
    if "--session-key" not in tokens:
        tokens[4:4] = ["--session-key", session.session_key]
    for flag, value in (
        ("--session-key", session.session_key),
        ("--challenge", capability),
        ("--expected-session-revision", str(session.targeted_revision)),
    ):
        if tokens.count(flag) != 1 or tokens.index(flag) + 1 >= len(tokens):
            return reject("flag-order")
        tokens[tokens.index(flag) + 1] = value
    corrected = " ".join(tokens)
    parsed = parse_bootstrap_command(corrected, runner, capability, reasons)
    return (corrected, parsed) if parsed is not None else None


def _form_register_root(command, runner, session, capability, reasons=None):
    """Form the bound command from semantic slots the author can actually write.

    The bound command's scope definition is canonical JSON in unpadded
    base64url. That is machine-computable only, and a session that has not yet
    registered a root has no machine: every shell tool is denied until the root
    exists. Reading the slots here as plain text leaves the semantics with the
    author and gives the encoding to the only machine in reach.

    This widens nothing. The command emitted still has to satisfy the strict
    fixed-token parser, which is verified before it is offered, so the boundary
    on what can execute is exactly where it was.
    """

    def reject(code):
        if reasons is not None:
            reasons.append(code)
        return None

    if not isinstance(command, str) or len(command) > 8192:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return reject("command-quoting")
    if (
        len(tokens) < 4
        or tokens[0] != "python"
        or tokens[2] != "lifecycle"
        or tokens[3] != "register-root"
    ):
        return None
    values = {}
    for index in range(4, len(tokens) - 1, 2):
        flag = tokens[index]
        if not flag.startswith("--"):
            return reject("flag-order")
        values[flag[2:]] = tokens[index + 1]
    required = ("scope-id", "scope-kind", "scope-title", "scope-outcome")
    if not all(key in values for key in required):
        # Not an attempt at the plain-slot form; leave the caller its template.
        return None
    try:
        validate_identifier(values["scope-id"], label="scope-id")
    except ValueError:
        return reject("scope-id")
    if values["scope-kind"] not in SCOPE_KINDS:
        return reject("scope-kind")
    try:
        definition = validate_scope_definition(
            {"title": values["scope-title"], "outcome": values["scope-outcome"]}
        )
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(definition))
            .decode()
            .rstrip("=")
        )
    except ValueError:
        return reject("definition-fields")
    formed = _control_command(
        runner,
        session,
        capability,
        "register-root",
        (
            ("scope-id", values["scope-id"]),
            ("scope-kind", values["scope-kind"]),
            ("scope-definition-b64", encoded),
        ),
    )
    # The feedback channel is bounded, so a definition whose encoding will not
    # fit has to be named rather than silently dropped back to the template.
    if len(formed.encode()) > MAX_REASON_BYTES - 250:
        return reject("definition-too-long")
    if parse_bootstrap_command(formed, runner, capability) is None:
        return reject("formed-command")
    return formed


def _advisory_runner(root):
    """The runner as the notice names it: relative, so its length is fixed."""

    return Path(".agent-handoff-toolkit/runner.py")


def _write_advisory(event, snapshot, root, storage, raw_id):
    """Say, once, that nothing is tracking this session, and decide nothing.

    Never blocks and never errors: an advisory that can fail the tool call is
    worse than no advisory. Any failure here leaves the call untouched.

    It carries no `permissionDecision`. An earlier form paired the notice with
    `permissionDecision: "allow"`, which does not merely decline to block - on
    a real host it also satisfies the permission gate, so the first repository
    write of every untracked session proceeded without the approval the user
    would otherwise have been asked for. An advisory must not grant an
    approval nobody gave it, so the host's own permission flow runs untouched.

    It is delivered as `additionalContext` because it is addressed to the
    model: it hands the session two commands to choose between, and a message
    the model never sees cannot be acted on. `systemMessage` surfaces text to
    the user, which is what `AHK-NO-HANDOFF` needs and this does not.

    It names neither the session key, the challenge, nor an absolute path. The
    control interception binds any plain `lifecycle` attempt into its complete
    form, so repeating two 64-hex values and a repository path here bought
    nothing and cost about 300 tokens in every untracked session.
    """

    try:
        runner = _advisory_runner(root).as_posix()
        mutated_session = replace(
            snapshot.session,
            write_advisory_emitted=True,
            targeted_revision=snapshot.session.targeted_revision + 1,
        )
        message = (
            "AHK-DECLARE: First repository edit with no registered root. If "
            "this work will continue in another session, run: python "
            f"{runner} lifecycle register-root --scope-id <id> --scope-kind "
            '<kind> --scope-title "<title>" --scope-outcome "<outcome>" '
            f"(the hook returns the bound form). If it will not, run: python "
            f"{runner} lifecycle one-off. Shown once."
        )
        _commit(storage, raw_id, snapshot, LifecycleMutation(mutated_session))
        return _context("PreToolUse", message)
    except Exception:
        # Advisory only. A failure here must not disturb the tool call.
        return HookExecution()


def _pre_tool(event, snapshot, root, storage, raw_id):
    session = snapshot.session
    command = (
        event.tool_input.get("command")
        if event.tool_name in _SHELL[event.host]
        else None
    )
    invocation = (
        re.match(r"^python \S+ lifecycle(?: (\S+))?(?: |$)", command)
        if isinstance(command, str)
        else None
    )
    # `doctor` is the one lifecycle command that takes no session binding, so
    # there is nothing for the interception to carry into it. It is read only,
    # and denying it would reproduce the deadlock it exists to break: the only
    # path to a challenge runs through the hook flow that is failing.
    control = invocation is not None and invocation.group(1) not in _UNBOUND_CONTROL
    # Work is never gated. The toolkit intercepts only its own control
    # commands, which is how a session discovers its session key, challenge and
    # revision: UserPromptSubmit returns silently, so this denial is the sole
    # carrier of those credentials.
    if not control:
        if (
            session.mode is EnforcementMode.OPEN
            and not session.write_advisory_emitted
            and event.tool_name in _WRITE_TOOLS[event.host]
            and session.current_external_user_turn_reference is not None
        ):
            return _write_advisory(event, snapshot, root, storage, raw_id)
        return HookExecution()
    if session.current_external_user_turn_reference is None:
        return render_hook_execution(
            event.host,
            event.event,
            LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-PRE-ROOT"),)),
        )
    runner = root / ".agent-handoff-toolkit" / "runner.py"
    info = _check_path(runner)
    if (
        not stat.S_ISREG(info.st_mode)
        or re.fullmatch(r"(?:[A-Z]:/|/)[A-Za-z0-9._/-]+", runner.as_posix()) is None
    ):
        raise ValueError("unsafe owned runner")
    capability = storage.control_capability(session)
    reasons = []
    repaired = _repair_bootstrap(command, runner, session, capability, reasons)
    code = "AHK-PRE-ROOT"
    note = "Use the current bound control command."
    if session.mode not in _UNGATED:
        code = "AHK-CONTROL-BINDING"
        tokens = command.split(" ")
        flags = {
            "--session-key": session.session_key,
            "--challenge": capability,
            "--expected-session-revision": str(session.targeted_revision),
        }
        if (
            len(tokens) > 3
            and tokens[1] == runner.as_posix()
            and all(
                tokens.count(flag) == 1
                and tokens.index(flag) + 1 < len(tokens)
                and tokens[tokens.index(flag) + 1] == value
                for flag, value in flags.items()
            )
        ):
            return HookExecution()
        corrected = _control_command(runner, session, capability, "inspect")
    elif repaired is not None:
        corrected, parsed = repaired
        if parsed.operation == "register-root":
            arguments = parsed.arguments
            digest = scope_definition_digest(
                {
                    "scope_id": arguments["scope_id"],
                    "scope_kind": arguments["scope_kind"],
                    "parent_scope_id": None,
                    "scope_definition": decode_scope_definition(
                        arguments["scope_definition_b64"]
                    ),
                }
            )
            duplicate = next(
                (
                    chain
                    for chain in storage.load_registry().chains.values()
                    if chain.status == "active"
                    and chain.locked_root_id == arguments["scope_id"]
                    and chain.scope_digests[0] == digest
                ),
                None,
            )
            if duplicate is not None:
                corrected = _control_command(
                    runner,
                    session,
                    capability,
                    "join",
                    (
                        ("authorization-id", duplicate.authorization_id),
                        ("expected-chain-revision", duplicate.targeted_revision),
                    ),
                )
        if corrected == command:
            return HookExecution()
    else:
        formed = _form_register_root(command, runner, session, capability, reasons)
        if formed is not None:
            corrected = formed
            note = "Semantic slots accepted and encoded; run this command."
            # The strict parse of the plain-slot attempt failed by design.
            # Reporting those checks beside a successful encoding would name a
            # failure that did not happen.
            reasons.clear()
        else:
            corrected = _control_command(
                runner,
                session,
                capability,
                "register-root",
                (
                    ("scope-id", "{scope_id}"),
                    ("scope-kind", "{scope_kind}"),
                    ("scope-definition-b64", "{scope_definition_b64}"),
                ),
            )
            note = 'Derive the three semantic slots from the initiating user request; tooling does not supply them. Plain --scope-id --scope-kind --scope-title "..." --scope-outcome "..." is encoded for you.'
    reason = f"{code}: {note}"
    # Detail is additive: naming the failed check never costs the code, the
    # corrective action, or the command the author is being handed. It is
    # reported only for an actual attempt at a control command; a denial of an
    # ordinary tool call has no bootstrap check to have failed.
    detail = ",".join(dict.fromkeys(reasons))[:128] if control else ""
    if detail:
        detailed = f"{reason} failed={detail}\nCommand: {corrected}"
        if len(detailed.encode()) <= MAX_REASON_BYTES:
            reason = detailed
        else:
            reason = f"{reason}\nCommand: {corrected}"
    else:
        reason = f"{reason}\nCommand: {corrected}"
    if len(reason.encode()) > MAX_REASON_BYTES:
        raise ValueError("control feedback exceeds bound")
    return HookExecution(
        stdout=json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            separators=(",", ":"),
        )
    )


def _reconcile_chain(storage, raw_id, snapshot):
    """Bring a session's view of its own chain up to date, best effort.

    Every worktree of a repository shares one lifecycle state root, so a peer
    joined to the same authorization leaves this session one revision behind
    the moment it publishes, and a peer that publishes a completion audit
    leaves this session holding a lease on a finished chain. Both are ordinary
    consequences of a supported configuration - `join` exists for it - and
    neither is this session's fault, so neither may cost it anything more than
    a refresh.

    `_apply` does not reject every mutation from a session in this state; it
    rejects one whose resulting `chain_revision` still differs from the live
    chain. That is exactly why this runs first: the other prompt-path
    mutations all preserve the stale value and would be refused, while this
    one carries the live value and is accepted.

    Releasing a completed lease preserves the previous external turn
    reference, because the reentry that follows requires the new reference to
    differ from the recorded one.
    """

    chain, session = snapshot.chain, snapshot.session
    if chain is None or session.mode in _UNGATED:
        return snapshot, None
    completed = (
        chain.status == "complete" and session.mode is not EnforcementMode.COMPLETE
    )
    if not completed and session.chain_revision == chain.targeted_revision:
        return snapshot, None
    mutated = replace(
        session,
        targeted_revision=session.targeted_revision + 1,
        chain_revision=chain.targeted_revision,
    )
    if completed:
        mutated = replace(mutated, mode=EnforcementMode.COMPLETE)
    try:
        updated = _commit(storage, raw_id, snapshot, LifecycleMutation(mutated))
    except Exception as error:
        # A failed reconciliation is survivable: the turn proceeds with stale
        # bookkeeping rather than the session losing its only input channel.
        return snapshot, _step_note("reconcile-chain", error)
    if completed:
        return updated, (
            "AHK-CHAIN-COMPLETED: another session completed this authorization; "
            "this session no longer holds it. Any further work needs a new "
            "declaration."
        )
    return updated, (
        "AHK-CHAIN-ADVANCED: another session advanced this authorization to "
        f"revision {chain.targeted_revision}. Rebuild any successor in "
        "progress against the current record before ending the turn."
    )


def _step_note(step, error):
    """Name a best-effort step that failed, in the closed vocabulary only."""

    name = type(error).__name__
    detail = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"
    return (
        f"AHK-STEP-SKIPPED failed=step:{step},error:{detail.replace('_', '-')}: "
        "lifecycle bookkeeping for this turn was skipped; the turn continues."
    )


def _merge_context(execution, notices):
    """Fold best-effort notices into whatever context the turn already emits.

    Both channels are preserved. Rebuilding through the context helper alone
    dropped any `systemMessage` the inner output carried, which is the only
    channel the user sees: the agent would be told what failed and the person
    waiting on the turn would not.
    """

    if not notices:
        return execution
    message = "\n".join(notices)
    sentence = None
    if execution.stdout:
        try:
            data = json.loads(execution.stdout)
            existing = data.get("hookSpecificOutput", {}).get("additionalContext")
            if existing:
                message = message + "\n" + existing
            sentence = data.get("systemMessage")
        except Exception:
            pass
    if sentence is None and any("AHK-STEP-SKIPPED" in note for note in notices):
        sentence = (
            "Agent handoff toolkit skipped some bookkeeping for this turn; "
            "the prompt was delivered and the session continues."
        )
    if sentence is None:
        # Not every notice is a malfunction. A reconciliation that worked is
        # the common one, and announcing it to the user as skipped bookkeeping
        # reported a fault every time a peer advanced the chain and everything
        # went right. The agent is still told, on its own channel.
        return _context("UserPromptSubmit", message)
    output = {
        "systemMessage": sentence,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": message,
        },
    }
    return HookExecution(
        stdout=json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    )


def _user_prompt(event, snapshot, storage, raw_id, root):
    """Observe the turn without ever deciding against it.

    Each step is attempted; a step that fails is named and the next one is
    still attempted. The user's message is delivered either way, which is the
    whole point: a state nobody enumerated degrades to a skipped step and a
    notice, never to a session that cannot be spoken to.
    """

    notices = []
    if event.external_user_turn:
        # Wrapped as well as guarded internally: the internal guard covers the
        # write, and this covers everything else in the step, so a fault
        # deciding *whether* to reconcile is named as this step rather than
        # escaping to the backstop and skipping the rest of the turn.
        snapshot, notice = _attempt(
            "reconcile-chain",
            notices,
            lambda: _reconcile_chain(storage, raw_id, snapshot),
            (snapshot, None),
        )
        if notice is not None:
            notices.append(notice)
    # The individual steps name themselves; this is the backstop for anything
    # raised between them, including by the notice machinery itself.
    output = _attempt(
        "prompt-path",
        notices,
        lambda: _user_prompt_steps(event, snapshot, storage, raw_id, root, notices),
        HookExecution(),
    )
    return _merge_context(output, notices)


def _declaration_kind(message):
    """Name the enrollment a prompt asked for, if it asked for one."""

    first_line = (message or "").split("\n", 1)[0]
    if re.fullmatch(r"Continue from handoff: (.+)", first_line):
        return "RESUME"
    if re.fullmatch(r"Track: (.{3,200})", first_line):
        return "TRACK"
    return None


def _attempt(step, notices, action, fallback):
    """Run one best-effort step; name it and carry on if it fails."""

    try:
        return action()
    except Exception as error:
        notices.append(_step_note(step, error))
        return fallback


def _user_prompt_steps(event, snapshot, storage, raw_id, root, notices):
    session = snapshot.session
    pending = session.pending_correction_hmac

    def echoed():
        # Classifying is part of this step. Deriving the challenge encodes the
        # user's own message, which can raise on input the host accepted and
        # UTF-8 does not - an unpaired surrogate is enough - and that fault
        # belongs to this step rather than to the whole turn.
        if not (
            event.external_user_turn and pending and session.correction_cycle_count > 0
        ):
            return False
        return hmac.compare_digest(
            pending,
            _correction_hmac(storage.secret, session, event.current_user_message),
        )

    if _attempt("correction-echo", notices, echoed, False):
        cleared = _attempt(
            "correction-echo",
            notices,
            lambda: _commit(
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
            ),
            None,
        )
        # Returning here is right when the echo was recorded: that turn is the
        # correction, not new work. When it was not recorded, returning would
        # also skip observing a real user turn on account of an unrelated
        # storage failure, so the turn continues down the ordinary path.
        if cleared is not None:
            return HookExecution()
        # The commit was refused, so the snapshot in hand is still the live
        # one and the ordinary path can use it unchanged.
    if not event.external_user_turn:
        return HookExecution()
    if session.worktree_baseline is None:
        # Purely advisory: the Stop backstop reads it, and losing it costs one
        # turn of that backstop rather than the turn itself.
        def baseline(current=snapshot):
            digest = worktree_digest(root)
            if digest is None:
                return current
            return _commit(
                storage,
                raw_id,
                current,
                LifecycleMutation(
                    replace(
                        current.session,
                        worktree_baseline=digest,
                        targeted_revision=current.session.targeted_revision + 1,
                    )
                ),
            )

        snapshot = _attempt("worktree-baseline", notices, baseline, snapshot)
        session = snapshot.session
    if event.current_user_reference == session.current_external_user_turn_reference:
        return HookExecution()
    if session.mode is EnforcementMode.COMPLETE:

        def reenter(current=snapshot):
            reset = evaluate_user_prompt(event, current)
            # evaluate_user_prompt carries the advisory baseline and the
            # once-per-session flag across the reentry; only the fresh
            # bootstrap challenge is added here.
            mutation = replace(
                reset.mutation,
                session=replace(
                    reset.mutation.session,
                    bootstrap_challenge="challenge-" + secrets.token_hex(16),
                ),
            )
            return _commit(storage, raw_id, current, mutation)

        snapshot = _attempt("chain-reentry", notices, reenter, snapshot)
        session = snapshot.session
        if session.mode is EnforcementMode.COMPLETE:
            # Reentry did not take. Observing the turn against a released
            # chain is not meaningful, and the next turn reenters cleanly.
            # The turn is not silent about it, though: reentry is what would
            # have made this session eligible to enroll, so a declaration
            # arriving on it goes unmet, and `Stop` allows a COMPLETE session
            # unconditionally. Saying nothing would let declared work proceed
            # with no handoff obligation and no one told.
            declaration = _declaration_kind(event.current_user_message)
            if declaration is not None:
                notices.append(
                    f"AHK-{declaration}-FAILED failed=step:chain-reentry: this "
                    "session is untracked. Tell the user."
                )
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

    def observe():
        return service.observe_user_turn(
            event,
            preceding_assistant_turn_reference=preceding,
            expected_chain_revision=snapshot.chain.targeted_revision
            if snapshot.chain
            else 0,
            expected_session_revision=session.targeted_revision,
        )

    # This step records more than the turn reference: it classifies a
    # transition approval and can install a successor chain. Losing it costs
    # the turn's authorization-bearing bookkeeping, so the steps that depend
    # on it do not run, and a user's approval may have to be reissued rather
    # than merely waited out.
    updated = _attempt("observe-turn", notices, observe, None)
    if updated is None:
        # Enrollment is deliberately *not* independent of observation.
        # `register_root` binds the new authorization to whatever turn
        # reference is already stored, so enrolling after a failed
        # observation would name the previous turn as the authority for this
        # one - worse than not enrolling. It is skipped, and an explicit
        # declaration that was eligible and went unmet is said out loud, in
        # the words the existing enrollment failures already use. A session
        # that was never eligible to enroll is not told it is untracked,
        # because it is not.
        if session.mode in _UNGATED:
            declaration = _declaration_kind(event.current_user_message)
            if declaration is not None:
                notices.append(
                    f"AHK-{declaration}-FAILED failed=step:observe-turn: this "
                    "session is untracked. Tell the user."
                )
        return HookExecution()
    if updated.session.pending_correction_hmac is not None:

        def clear(current=updated):
            return _commit(
                storage,
                raw_id,
                current,
                LifecycleMutation(
                    replace(
                        current.session,
                        targeted_revision=current.session.targeted_revision + 1,
                        pending_correction_hmac=None,
                    )
                ),
            )

        # Its own step: clearing a spent challenge is unrelated to recording
        # the answer to a question, and a failure here used to discard that
        # answer as collateral.
        updated = _attempt("correction-clear", notices, clear, updated)
    request = updated.session.pending_decision_reference
    if request is not None:

        def resolve(current=updated):
            # Classifying the answer belongs inside the step: deciding whether
            # the prompt resolves the question can fail just as recording it
            # can, and a fault there is this step's, not the whole turn's.
            if not _decision_resolved(event.current_user_message, request):
                return current
            decision = evaluate_user_prompt(event, current, decision_resolved=True)
            return _commit(storage, raw_id, current, decision.mutation)

        # Recording the answer is its own step: losing it must not cost the
        # turn, and the session re-asks rather than proceeding as if answered.
        updated = _attempt("decision-answer", notices, resolve, updated)
    # The pointer line is the whole resume decision. The prompt body was never
    # the evidence - the record's digest and the live chain are - and requiring
    # the paste to match the renderer byte for byte meant one stray space left
    # the session untracked with nothing said to anyone.
    eligible = updated.session.mode in _UNGATED
    declared = _declaration_kind(event.current_user_message) if eligible else None
    if declared is not None:
        # Enrollment is its own step. `_resume_chain` and `_track_root` name
        # their own expected failures, but an unexpected one here reached the
        # generic backstop and reported `step:prompt-path`, leaving a session
        # the user explicitly asked to track with no word that it is not.
        enrolled = _attempt(
            "enroll",
            notices,
            lambda: _enroll(event, updated, service, storage, root),
            None,
        )
        if enrolled is not None:
            return enrolled
        notices.append(
            f"AHK-{declared}-FAILED failed=step:enroll: this session is "
            "untracked. Tell the user."
        )
        return HookExecution()
    return _pending_clarification(updated)


def _enroll(event, updated, service, storage, root):
    """Register the enrollment the prompt declared, or say why it did not."""

    first_line = (event.current_user_message or "").split("\n", 1)[0]
    match = re.fullmatch(r"Continue from handoff: (.+)", first_line)
    if match and updated.session.mode in _UNGATED:
        resumed, failure = _resume_chain(
            service, storage, updated, match.group(1), root
        )
        if failure is not None:
            return _context(
                "UserPromptSubmit",
                f"AHK-RESUME-FAILED failed={failure}: the pasted handoff did not "
                "resume a tracked chain; this session is untracked. Tell the user.",
            )
        updated = resumed
        return _context(
            "UserPromptSubmit",
            f"AHK-RESUMED: tracked root {updated.chain.locked_root_id} from "
            f"{match.group(1)}. This session must end with a continuation or a "
            "completion audit. Any text after the first line of the prompt is "
            "the user's instruction for this session."
            + _credentials(root, storage, updated),
        )
    # The user knows when a request is an epic. Naming it in their own turn is
    # both the plainest way to say so and the strongest authorization evidence
    # the design ever wanted: the root is bound to the turn that asked for it.
    declaration = re.fullmatch(r"Track: (.{3,200})", first_line)
    if declaration and updated.session.mode in _UNGATED:
        tracked, failure = _track_root(service, storage, updated, declaration.group(1))
        if failure is not None:
            return _context(
                "UserPromptSubmit",
                f"AHK-TRACK-FAILED failed={failure}: the declared goal did not "
                "register a root; this session is untracked. Tell the user.",
            )
        updated = tracked
        return _context(
            "UserPromptSubmit",
            f'AHK-TRACKED: root "{declaration.group(1)}" registered for this and '
            "later sessions. This session must end with a continuation or a "
            "completion audit; the rest of the prompt is the work."
            + _credentials(root, storage, updated),
        )
    return HookExecution()


def _pending_clarification(updated):
    """Ask for the outstanding answer, whatever else the turn did."""

    remaining_proposal = updated.session.pending_transition_reference
    if updated.session.pending_decision_reference or (
        remaining_proposal and remaining_proposal.status == "pending"
    ):
        return _context(
            "UserPromptSubmit",
            "AHK-USER-CLARIFY: Clarify the pending decision or reissue the exact scope proposal for an adjacent response. No new authorization was granted.",
        )
    return HookExecution()


def _root_scope_id(title):
    """Slug the user's own words, with a short digest tail for distinctness.

    The tail is derived from the title rather than drawn at random so that the
    same declared goal always names the same root: that is what lets a second
    session declaring it join the chain the first one started instead of
    silently forking a parallel epic.
    """

    slug = "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:64].strip("-")
    tail = hashlib.sha256(title.encode()).hexdigest()[:4]
    return (slug + "-" + tail) if slug else "goal-" + tail


def _track_root(service, storage, snapshot, title):
    """Register the user's declared goal, or join the chain that already holds it."""

    try:
        definition = {"title": title, "outcome": title}
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(definition))
            .decode()
            .rstrip("=")
        )
        scope_id = _root_scope_id(title)
        digest = scope_definition_digest(
            {
                "scope_id": scope_id,
                "scope_kind": "epic",
                "parent_scope_id": None,
                "scope_definition": definition,
            }
        )
    except Exception:
        return snapshot, "scope-definition"
    try:
        return (
            service.register_root(
                challenge=snapshot.session.bootstrap_challenge,
                scope_id=scope_id,
                scope_kind="epic",
                scope_definition_b64=encoded,
                expected_session_revision=snapshot.session.targeted_revision,
            ),
            None,
        )
    except StaleLifecycleState:
        return snapshot, "chain-stale"
    except Exception:
        pass
    # The same goal, declared again. `register_root` refuses to fork a second
    # chain over an identical active root, so this session joins that one.
    try:
        existing = next(
            (
                chain
                for chain in storage.load_registry().chains.values()
                if chain.status == "active"
                and chain.locked_root_id == scope_id
                and chain.scope_digests[0] == digest
            ),
            None,
        )
        if existing is None:
            return snapshot, "root-registration"
        return (
            service.join(
                challenge=snapshot.session.bootstrap_challenge,
                authorization_id=existing.authorization_id,
                expected_chain_revision=existing.targeted_revision,
                expected_session_revision=snapshot.session.targeted_revision,
            ),
            None,
        )
    except StaleLifecycleState:
        return snapshot, "chain-stale"
    except Exception:
        return snapshot, "root-registration"


def _credentials(root, storage, snapshot):
    """Hand a newly tracked session the bound command it would otherwise buy.

    Without this the only way to learn the session key, challenge and expected
    revision is to attempt a control command and read the denial: one wasted
    tool call and two messages before any work starts.
    """

    try:
        runner = root / ".agent-handoff-toolkit" / "runner.py"
        capability = storage.control_capability(snapshot.session)
        command = _control_command(runner, snapshot.session, capability, "inspect")
        return "\nCommand: " + command
    except Exception:
        # The context is worth sending without the command; the denial path
        # still issues credentials exactly as it did before.
        return ""


def _context(event_name, message):
    """Return model-visible context and no decision of any kind."""

    return HookExecution(
        stdout=json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": message,
                }
            },
            separators=(",", ":"),
        )
    )


def _resume_chain(service, storage, snapshot, pointer, root):
    """Resume the chain the pointed-at record heads, or name why it did not.

    The failure codes are the same closed vocabulary every other denial uses.
    None of them carries an exception message, a path the caller did not
    already supply, or any part of the prompt.
    """

    try:
        canonical = canonical_record_path(pointer)
        contained = Path(canonical).is_relative_to(root / "handoffs")
    except Exception:
        return snapshot, "candidate-outside-handoffs"
    if not contained:
        return snapshot, "candidate-outside-handoffs"
    try:
        text, data, digest = _read_record(pointer, root)
    except Exception:
        return snapshot, "record-invalid"
    failure = _resume_precheck(storage, data, canonical, digest)
    if failure is not None:
        return snapshot, failure
    try:
        return (
            service.resume(
                challenge=snapshot.session.bootstrap_challenge,
                record_path=pointer,
                record_text=text,
                record_metadata=data,
                record_digest=digest,
                expected_session_revision=snapshot.session.targeted_revision,
            ),
            None,
        )
    except StaleLifecycleState:
        return snapshot, "chain-stale"
    except Exception:
        return snapshot, "record-invalid"


def _record_name(path):
    """The record's basename, which is stable across checkouts."""

    return PurePosixPath(str(path)).name


def _resume_precheck(storage, data, path, digest):
    """Classify a resume that cannot succeed, before anything is committed."""

    if (
        not isinstance(data, Mapping)
        or data.get("schema_version") != 2
        or data.get("record_type") != "continuation"
        or not isinstance(data.get("authorization_id"), str)
    ):
        return "record-invalid"
    try:
        chain = storage.load_chain(data["authorization_id"])
    except Exception:
        return "chain-inactive"
    if chain is None or chain.status != "active":
        return "chain-inactive"
    current = chain.current_record_reference
    # The path is a locator: the chain may have recorded it in another
    # worktree or under WSL. The record id and the digest are the identity.
    if current is None or (
        current.record_id,
        _record_name(current.path),
        current.sha256,
    ) != (data.get("record_id"), _record_name(path), digest):
        return "record-digest"
    if chain.locked_root_id != data.get("authorized_root_scope_id"):
        return "record-invalid"
    return None


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
    stage = "decode-input"
    try:
        payload = decode_payload(raw)
        # Nothing is gated at PreToolUse but the toolkit's own control
        # commands and the first-write advisory, and the advisory fires only
        # for a known writing tool. Every other call - every shell command,
        # every MCP call - used to open state, take its lock and run a git
        # subprocess before returning the empty decision it always returns.
        # Two fields are read defensively here; anything unexpected falls
        # through to the unchanged path below.
        if event_kind is EventName.PRE_TOOL_USE and platform in _WRITE_TOOLS:
            tool = payload.get("tool_name")
            inputs = payload.get("tool_input")
            command = inputs.get("command") if isinstance(inputs, Mapping) else None
            control = (
                isinstance(command, str)
                and re.match(r"^python \S+ lifecycle(?: |$)", command) is not None
            )
            if not control and tool not in _WRITE_TOOLS[platform]:
                return HookExecution()
        root = Path(repo_root).resolve()
        raw_id = _string(payload.get("session_id"), 4096)
        if storage is None:
            stage = "open-state"
            state_root = os.environ.get("AHK_STATE_ROOT")
            storage = LocalLifecycleStorage(
                root, state_root=Path(state_root) if state_root else None
            )
        stage = "load-state"
        snapshot = storage.load_snapshot(raw_id)
        stage = "normalize-event"
        event = normalize_event(platform, name, payload, repo_root)
        event = replace(event, session_key=snapshot.session.session_key)
        stage = "evaluate"
        if event_kind is EventName.PRE_TOOL_USE:
            return _pre_tool(event, snapshot, root, storage, raw_id)
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
    except Exception as error:
        issue = _runtime_issue(stage, error)
        # A runtime fault is a malfunction, not a policy decision. A session
        # that declared no tracked work is ungated by construction, so there is
        # nothing here to enforce and blocking only removes the session's way
        # out - it was denied the very tools needed to repair the fault. Report
        # it on the advisory channel instead, which carries no decision at all.
        if not _declared_tracked(snapshot):
            return _runtime_advisory(issue)
        # Declared tracked work keeps the documented fail-closed guarantee. A
        # counted block is the only thing that arms the correction circuit, and
        # this is the last-resort handler, so a failure forming the counted
        # block still has to yield a rendered decision rather than a crash.
        try:
            decision = (
                _blocked_stop(snapshot, (issue,), allow_session_identity=True)
                if event_kind is EventName.STOP
                else LifecycleDecision(DecisionKind.BLOCK, (issue,))
            )
        except Exception:
            decision = LifecycleDecision(DecisionKind.BLOCK, (issue,))
    output = _publish_decision(
        platform, event_kind, decision, storage, raw_id, snapshot
    )
    if (
        event_kind is EventName.STOP
        and output == HookExecution()
        and snapshot is not None
        and snapshot.session.mode in _UNGATED
    ):
        note = _no_handoff_note(snapshot, Path(repo_root).resolve(), storage, raw_id)
        if note is not None:
            return note
    return output


def _no_handoff_note(snapshot, root, storage, raw_id):
    """Report unfinished work once, at the end of an untracked session.

    Both conditions must hold: the tree is dirty now, and it differs from the
    baseline taken at the session's first user turn. The first alone fires on
    work the user left in place beforehand; the second alone fires on a session
    that cleaned the tree by committing pre-existing changes.

    It says this once. `Stop` runs at every turn end, not at the end of a
    session, so an unchanged repeat cost the user the same notice every time
    the agent stopped talking and told them nothing they had not read. The
    flag is committed with the notice, which is also what lets a later `Stop`
    skip the `git status` subprocess entirely.
    """

    try:
        if snapshot.session.no_handoff_note_emitted:
            return None
        baseline = snapshot.session.worktree_baseline
        if baseline is None:
            return None
        # One `git status` per Stop: the digest and the dirtiness are two
        # readings of the same porcelain output, not two subprocesses.
        state = worktree_state(root)
        if state is None:
            return None
        current, dirty = state
        if current == baseline or not dirty:
            return None
        # The flag is committed before the notice is returned, so a failure to
        # record it leaves the notice unsent rather than repeating forever.
        _commit(
            storage,
            raw_id,
            snapshot,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    no_handoff_note_emitted=True,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                )
            ),
        )
        # Plain English, addressed to the person reading it. The mechanism
        # sees a changed tree, not who changed it, so the notice still does
        # not claim the session made the change.
        return HookExecution(
            stdout=json.dumps(
                {
                    "systemMessage": (
                        "AHK-NO-HANDOFF: The repository changed during this "
                        "session and is ending with uncommitted work and no "
                        "handoff record. If this work continues later, ask me "
                        "to create a handoff before you close the session."
                    )
                },
                separators=(",", ":"),
            )
        )
    except Exception:
        # Advisory only; never disturb the stop.
        return None


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
                except Exception as error:
                    issue = _runtime_issue("republish-stale", error)
                    if not _declared_tracked(snapshot):
                        return _runtime_advisory(issue)
                    return render_hook_execution(
                        platform,
                        event_kind,
                        LifecycleDecision(DecisionKind.BLOCK, (issue,)),
                    )
            return render_hook_execution(
                platform,
                event_kind,
                LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-STOP-STALE"),)),
            )
        except Exception as error:
            issue = _runtime_issue("commit-state", error)
            if not _declared_tracked(snapshot):
                return _runtime_advisory(issue)
            return render_hook_execution(
                platform,
                event_kind,
                LifecycleDecision(DecisionKind.BLOCK, (issue,)),
            )
    return output
