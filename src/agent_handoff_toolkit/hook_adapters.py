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
from pathlib import Path
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
from .records import parse_markdown, render_resume_prompt, render_terminal_response
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


def _write_advisory(event, snapshot, root, storage, raw_id):
    """Say, once, that nothing is tracking this session, and decide nothing.

    Never blocks and never errors: an advisory that can fail the tool call is
    worse than no advisory. Any failure here leaves the call untouched.

    The payload carries a `systemMessage` and nothing else. An earlier form
    paired it with `permissionDecision: "allow"`, which does not merely
    decline to block - on a real host it also satisfies the permission gate,
    so the first repository write of every untracked session proceeded without
    the approval the user would otherwise have been asked for. An advisory
    must not grant an approval nobody gave it, so it now returns no decision
    at all and the host's own permission flow runs untouched.
    """

    try:
        runner = root / ".agent-handoff-toolkit" / "runner.py"
        # Recording the advisory is itself a session commit, and every commit
        # advances targeted_revision. Building the bound commands from the
        # pre-commit session would hand the author a revision the commit
        # itself has already invalidated, so the capability and commands are
        # derived from the post-commit session the mutation below will
        # produce, and the commit uses that identical, already-built session.
        mutated_session = replace(
            snapshot.session,
            write_advisory_emitted=True,
            targeted_revision=snapshot.session.targeted_revision + 1,
        )
        capability = storage.control_capability(mutated_session)
        message = (
            "AHK-DECLARE: This session is changing the repository with no "
            "registered root, so nothing will carry the work to a next session. "
            "If the work ends here, run:\n"
            + _control_command(runner, mutated_session, capability, "one-off")
            + "\nIf it continues past this session, run register-root with the "
            "scope title and outcome as plain text:\n"
            + _control_command(
                runner,
                mutated_session,
                capability,
                "register-root",
                (
                    ("scope-id", "{scope_id}"),
                    ("scope-kind", "{scope_kind}"),
                    ("scope-title", '"{title}"'),
                    ("scope-outcome", '"{outcome}"'),
                ),
            )
            + "\nNeither is required; this notice appears once."
        )
        # MAX_REASON_BYTES bounds hook *feedback* - a denial reason fed back
        # to the model, where an oversize string costs a correction cycle. A
        # one-shot notice on an undecided path is neither fed back nor looped,
        # and its length is a deterministic function of the runner path, so
        # applying that bound here only produced a silent cliff: past a repo
        # root of roughly 145 characters no advisory ever fired, and because
        # the flag was committed after the check, the capability and message
        # were rebuilt on every subsequent write call. The notice is exempt.
        _commit(storage, raw_id, snapshot, LifecycleMutation(mutated_session))
        return HookExecution(
            stdout=json.dumps({"systemMessage": message}, separators=(",", ":"))
        )
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
        pending = session.pending_transition_reference
        formed = _form_register_root(command, runner, session, capability, reasons)
        if pending and pending.kind == "v1-adoption" and pending.status == "approved":
            corrected = _control_command(
                runner,
                session,
                capability,
                "adopt-v1",
                (("record", pending.selected_record.path),),
            )
        elif formed is not None:
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
    if session.worktree_baseline is None:
        digest = worktree_digest(root)
        if digest is not None:
            snapshot = _commit(
                storage,
                raw_id,
                snapshot,
                LifecycleMutation(
                    replace(
                        snapshot.session,
                        worktree_baseline=digest,
                        targeted_revision=snapshot.session.targeted_revision + 1,
                    )
                ),
            )
            session = snapshot.session
    if event.current_user_reference == session.current_external_user_turn_reference:
        return HookExecution()
    if session.mode is EnforcementMode.COMPLETE:
        reset = evaluate_user_prompt(event, snapshot)
        # evaluate_user_prompt carries the advisory baseline and the
        # once-per-session flag across the reentry; only the fresh bootstrap
        # challenge is added here.
        mutation = replace(
            reset.mutation,
            session=replace(
                reset.mutation.session,
                bootstrap_challenge="challenge-" + secrets.token_hex(16),
            ),
        )
        snapshot = _commit(storage, raw_id, snapshot, mutation)
        session = snapshot.session
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
    match = re.match(r"Continue from handoff: ([^\n]+)\n", event.current_user_message)
    if match and updated.session.mode in _UNGATED:
        path = match.group(1)
        text, data, digest = _read_record(path, root)
        if event.current_user_message != render_resume_prompt(path, text):
            return HookExecution()
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
    stage = "decode-input"
    try:
        payload = decode_payload(raw)
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
