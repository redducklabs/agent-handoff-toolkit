"""Pure lifecycle policy and immutable state transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import hmac
import re
from types import MappingProxyType
from typing import Any
import unicodedata

from .lineage import (
    LineageError,
    canonical_json_bytes,
    normalize_text,
    record_digest,
    scope_definition_digest,
    validate_hex_digest,
    validate_identifier,
)
from .records import (
    ValidationIssue,
    parse_markdown,
    render_terminal_response,
    validate_markdown,
    validate_successor,
)


class EventName(str, Enum):
    USER_PROMPT_SUBMIT = "user-prompt-submit"
    PRE_TOOL_USE = "pre-tool-use"
    STOP = "stop"


class EnforcementMode(str, Enum):
    UNTRACKED = "untracked"
    TRACKED = "tracked"
    AWAITING_DECISION = "awaiting-decision"
    COMPLETE = "complete"


class DecisionKind(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    POLICY_FAILURE = "policy-failure"


class AuthorityCategory(str, Enum):
    MISSING_INPUT = "missing-input"
    MUTUALLY_EXCLUSIVE_CHOICE = "mutually-exclusive-choice"
    EXTERNAL_EFFECT = "external-effect"
    DESTRUCTIVE_OPERATION = "destructive-operation"
    REPOSITORY_APPROVAL = "repository-approval"


class AffirmationResult(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"


_ACTION_FIELDS = {"action", "target", "constraints", "completion_condition"}
_CHAIN_STATUSES = {"active", "superseded", "complete"}
_TOOL_CAPABILITIES = {
    "intrinsic-read-only",
    "tool-free",
    "mutation-capable",
    "unknown",
}


def _enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{label} must be a {enum_type.__name__}")


def _identifier(value: object, label: str) -> str:
    try:
        return validate_identifier(value, label=label)
    except LineageError as error:
        raise ValueError(str(error)) from error


def _digest(value: object, label: str) -> str:
    try:
        return validate_hex_digest(value, label=label)
    except LineageError as error:
        raise ValueError(str(error)) from error


def _text(value: object, *, limit: int, label: str) -> str:
    try:
        return normalize_text(value, limit=limit, label=label)  # type: ignore[arg-type]
    except LineageError as error:
        raise ValueError(str(error)) from error


def _optional_text(value: object, *, limit: int, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, limit=limit, label=label)


def _content(value: object, *, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(
        character != "\n"
        and unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
        for character in normalized
    ):
        raise ValueError(f"{label} contains unsafe text")
    if len(normalized.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    return normalized


def _revision(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _has_unsafe_text(value: object) -> bool:
    if isinstance(value, str):
        return any(
            character != "\n"
            and unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
            for character in value
        )
    if isinstance(value, Mapping):
        return any(
            not isinstance(key, str) or _has_unsafe_text(key) or _has_unsafe_text(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_unsafe_text(item) for item in value)
    return False


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _frozen_mapping(value: object, *, limit: int, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a mapping with text keys")
    if _has_unsafe_text(value):
        raise ValueError(f"{label} contains unsafe text")
    plain_value = _thaw_json(value)
    try:
        encoded = canonical_json_bytes(plain_value)
    except LineageError as error:
        raise ValueError(f"{label} must contain canonical JSON values") from error
    if len(encoded) > limit:
        raise ValueError(f"{label} exceeds its UTF-8 byte limit")
    return _freeze_json(plain_value)


def _action_value(value: object) -> str | tuple[str, ...]:
    if isinstance(value, str):
        return _text(value, limit=1000, label="blocked_action_value")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = tuple(
            _text(item, limit=500, label="blocked_action_value item") for item in value
        )
        if not items or len(items) > 16:
            raise ValueError("blocked_action_value must contain 1 to 16 items")
        return items
    raise ValueError("blocked_action_value must be text or a sequence of text")


@dataclass(frozen=True)
class NormalizedEvent:
    host: str
    event: EventName
    session_key: str
    turn_reference: str
    repository_root: str
    transcript_reference: str
    stop_hook_active: bool
    latest_assistant_message: str | None = None
    current_user_message: str | None = None
    current_user_reference: str | None = None
    tool_name: str | None = None
    tool_input: Mapping[str, object] | None = None
    tool_capability: str | None = None
    external_user_turn: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", _identifier(self.host, "host"))
        _enum(self.event, EventName, "event")
        object.__setattr__(
            self, "session_key", _identifier(self.session_key, "session_key")
        )
        object.__setattr__(
            self,
            "turn_reference",
            _identifier(self.turn_reference, "turn_reference"),
        )
        object.__setattr__(
            self,
            "repository_root",
            _text(self.repository_root, limit=4096, label="repository_root"),
        )
        object.__setattr__(
            self,
            "transcript_reference",
            _text(
                self.transcript_reference,
                limit=4096,
                label="transcript_reference",
            ),
        )
        if not isinstance(self.stop_hook_active, bool):
            raise ValueError("stop_hook_active must be boolean")
        if not isinstance(self.external_user_turn, bool):
            raise ValueError("external_user_turn must be boolean")
        object.__setattr__(
            self,
            "latest_assistant_message",
            _optional_text(
                self.latest_assistant_message,
                limit=16384,
                label="latest_assistant_message",
            ),
        )
        object.__setattr__(
            self,
            "current_user_message",
            _optional_text(
                self.current_user_message,
                limit=16384,
                label="current_user_message",
            ),
        )
        if self.current_user_reference is not None:
            object.__setattr__(
                self,
                "current_user_reference",
                _identifier(self.current_user_reference, "current_user_reference"),
            )
        if self.tool_name is not None:
            object.__setattr__(
                self, "tool_name", _text(self.tool_name, limit=128, label="tool_name")
            )
        if self.tool_input is not None:
            object.__setattr__(
                self,
                "tool_input",
                _frozen_mapping(self.tool_input, limit=4096, label="tool_input"),
            )
        if (
            self.tool_capability is not None
            and self.tool_capability not in _TOOL_CAPABILITIES
        ):
            raise ValueError("tool_capability is not recognized")

    def to_state_dict(self) -> dict[str, object]:
        """Return only bounded non-content event metadata."""

        return {
            "host": self.host,
            "event": self.event.value,
            "session_key": self.session_key,
            "turn_reference": self.turn_reference,
            "repository_root": self.repository_root,
            "transcript_reference": self.transcript_reference,
            "stop_hook_active": self.stop_hook_active,
            "current_user_reference": self.current_user_reference,
            "tool_name": self.tool_name,
            "tool_capability": self.tool_capability,
            "external_user_turn": self.external_user_turn,
        }


@dataclass(frozen=True)
class LifecycleIssue:
    code: str
    summary: str
    corrective_action: str
    expected: str | None = None
    actual: str | None = None
    candidate_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, "issue code"))
        object.__setattr__(
            self, "summary", _text(self.summary, limit=400, label="issue summary")
        )
        object.__setattr__(
            self,
            "corrective_action",
            _text(
                self.corrective_action,
                limit=800,
                label="issue corrective action",
            ),
        )
        for field_name in ("expected", "actual", "candidate_path"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), limit=4096, label=field_name),
            )


@dataclass(frozen=True)
class DecisionRequest:
    request_id: str
    authorization_id: str
    category: AuthorityCategory
    blocked_action_field: str
    blocked_action_value: str | tuple[str, ...]
    question_hmac: str
    reason_hmac: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _identifier(self.request_id, "request_id")
        )
        object.__setattr__(
            self,
            "authorization_id",
            _identifier(self.authorization_id, "authorization_id"),
        )
        _enum(self.category, AuthorityCategory, "category")
        if self.blocked_action_field not in _ACTION_FIELDS:
            raise ValueError("blocked_action_field is not an exact-action field")
        object.__setattr__(
            self, "blocked_action_value", _action_value(self.blocked_action_value)
        )
        object.__setattr__(
            self,
            "question_hmac",
            _digest(self.question_hmac, "question_hmac"),
        )
        object.__setattr__(
            self, "reason_hmac", _digest(self.reason_hmac, "reason_hmac")
        )


@dataclass(frozen=True)
class RecordReference:
    record_id: str
    path: str
    sha256: str

    def __post_init__(self):
        _identifier(self.record_id, "record_id")
        _digest(self.sha256, "sha256")
        _text(self.path, limit=4096, label="record path")
        if (
            not re.fullmatch(r"(?:[A-Z]:/|/)[^\\\n<>]+\.md", self.path)
            or self.path.startswith("//")
            or any(part in {"", ".", ".."} for part in self.path.split("/")[1:])
        ):
            raise ValueError("record path must be canonical absolute Markdown")


@dataclass(frozen=True)
class AuthorizationProposal:
    proposal_id: str
    kind: str
    assistant_turn_reference: str
    from_authorization_id: str | None
    to_authorization_id: str
    old_scopes: tuple[Mapping[str, object], ...]
    new_scopes: tuple[Mapping[str, object], ...]
    selected_record: RecordReference | None = None
    status: str = "pending"
    approval_turn_reference: str | None = None
    evidence_hmac: str | None = None
    source_user_turn_reference: str | None = None

    def __post_init__(self):
        if self.source_user_turn_reference is not None:
            _identifier(self.source_user_turn_reference, "source_user_turn_reference")
        _identifier(self.proposal_id, "proposal_id")
        _identifier(self.assistant_turn_reference, "assistant_turn_reference")
        _identifier(self.to_authorization_id, "to_authorization_id")
        if self.to_authorization_id == self.from_authorization_id:
            raise ValueError("proposal must create a new authorization")
        if self.from_authorization_id is not None:
            _identifier(self.from_authorization_id, "from_authorization_id")
        if self.kind not in {"transition", "v1-adoption"} or self.status not in {
            "pending",
            "approved",
            "consumed",
        }:
            raise ValueError("invalid authorization proposal kind or status")
        for name in ("old_scopes", "new_scopes"):
            scopes = tuple(getattr(self, name))
            if len(scopes) > 64 or (name == "new_scopes" and not scopes):
                raise ValueError("invalid proposal scope count")
            frozen = []
            for scope in scopes:
                if set(scope) != {
                    "scope_id",
                    "scope_kind",
                    "parent_scope_id",
                    "scope_definition",
                }:
                    raise ValueError("proposal scope contains unexpected fields")
                scope_definition_digest(scope)
                frozen.append(
                    _frozen_mapping(scope, limit=2048, label="proposal scope")
                )
            object.__setattr__(self, name, tuple(frozen))
        if self.kind == "v1-adoption" and not isinstance(
            self.selected_record, RecordReference
        ):
            raise ValueError("adoption requires one selected record")
        if self.kind == "transition" and (
            self.from_authorization_id is None
            or not self.old_scopes
            or self.selected_record is not None
        ):
            raise ValueError("transition requires old authority and scopes")
        if self.status == "pending":
            if (
                self.approval_turn_reference is not None
                or self.evidence_hmac is not None
            ):
                raise ValueError("pending proposal cannot contain approval evidence")
        else:
            _identifier(self.approval_turn_reference, "approval_turn_reference")
            _digest(self.evidence_hmac, "evidence_hmac")


@dataclass(frozen=True)
class ChainState:
    authorization_id: str
    locked_root_id: str
    scope_digests: tuple[str, ...]
    targeted_revision: int
    status: str
    current_record_reference: RecordReference | None
    successor_authorization_id: str | None = None
    authorization_user_turn_reference: str | None = None
    authorization_evidence_hmac: str | None = None
    publication_evidence: AuthorizationProposal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authorization_id",
            _identifier(self.authorization_id, "authorization_id"),
        )
        object.__setattr__(
            self,
            "locked_root_id",
            _identifier(self.locked_root_id, "locked_root_id"),
        )
        digests = tuple(_digest(item, "scope digest") for item in self.scope_digests)
        if not digests:
            raise ValueError("scope_digests must not be empty")
        object.__setattr__(self, "scope_digests", digests)
        object.__setattr__(
            self,
            "targeted_revision",
            _revision(self.targeted_revision, "targeted_revision"),
        )
        if self.status not in _CHAIN_STATUSES:
            raise ValueError("chain status is not recognized")
        if self.current_record_reference is not None and not isinstance(
            self.current_record_reference, RecordReference
        ):
            raise ValueError("current_record_reference must be a RecordReference")
        if self.authorization_user_turn_reference is not None:
            _identifier(
                self.authorization_user_turn_reference,
                "authorization_user_turn_reference",
            )
        if self.authorization_evidence_hmac is not None:
            _digest(self.authorization_evidence_hmac, "authorization_evidence_hmac")
        proof = self.publication_evidence
        if proof is not None:
            if (
                not isinstance(proof, AuthorizationProposal)
                or proof.kind != "transition"
                or proof.status not in {"approved", "consumed"}
                or proof.to_authorization_id != self.authorization_id
                or self.current_record_reference is None
                or self.status != "active"
                or proof.new_scopes[0]["scope_id"] != self.locked_root_id
                or tuple(scope_definition_digest(scope) for scope in proof.new_scopes)
                != self.scope_digests
                or proof.evidence_hmac != self.authorization_evidence_hmac
                or proof.approval_turn_reference
                != self.authorization_user_turn_reference
            ):
                raise ValueError(
                    "publication evidence must bind this active successor chain"
                )
        if self.successor_authorization_id is not None:
            object.__setattr__(
                self,
                "successor_authorization_id",
                _identifier(
                    self.successor_authorization_id,
                    "successor_authorization_id",
                ),
            )


@dataclass(frozen=True)
class SessionState:
    session_key: str
    targeted_revision: int
    mode: EnforcementMode
    authorization_id: str | None = None
    chain_revision: int | None = None
    pending_decision_reference: DecisionRequest | None = None
    pending_transition_reference: AuthorizationProposal | None = None
    correction_cycle_count: int = 0
    last_issue_signature: str | None = None
    current_external_user_turn_reference: str | None = None
    bootstrap_challenge: str | None = None
    pending_correction_hmac: str | None = None

    def __post_init__(self) -> None:
        if self.pending_correction_hmac is not None:
            _digest(self.pending_correction_hmac, "pending_correction_hmac")
        object.__setattr__(
            self, "session_key", _identifier(self.session_key, "session_key")
        )
        object.__setattr__(
            self,
            "targeted_revision",
            _revision(self.targeted_revision, "targeted_revision"),
        )
        _enum(self.mode, EnforcementMode, "mode")
        if self.authorization_id is not None:
            object.__setattr__(
                self,
                "authorization_id",
                _identifier(self.authorization_id, "authorization_id"),
            )
        if self.chain_revision is not None:
            object.__setattr__(
                self,
                "chain_revision",
                _revision(self.chain_revision, "chain_revision"),
            )
        if self.pending_decision_reference is not None and not isinstance(
            self.pending_decision_reference, DecisionRequest
        ):
            raise ValueError("pending_decision_reference must be a DecisionRequest")
        if self.pending_transition_reference is not None and not isinstance(
            self.pending_transition_reference, AuthorizationProposal
        ):
            raise ValueError(
                "pending_transition_reference must be an AuthorizationProposal"
            )
        for field_name in (
            "current_external_user_turn_reference",
            "bootstrap_challenge",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _identifier(value, field_name))
        object.__setattr__(
            self,
            "correction_cycle_count",
            _revision(self.correction_cycle_count, "correction_cycle_count"),
        )
        if self.last_issue_signature is not None:
            object.__setattr__(
                self,
                "last_issue_signature",
                _digest(self.last_issue_signature, "last_issue_signature"),
            )
        if self.mode is EnforcementMode.UNTRACKED:
            if self.authorization_id is not None or self.chain_revision is not None:
                raise ValueError("untracked sessions cannot identify a chain")
        elif self.authorization_id is None or self.chain_revision is None:
            raise ValueError("tracked lifecycle modes require chain identity")
        if (
            self.mode is EnforcementMode.AWAITING_DECISION
            and self.pending_decision_reference is None
        ):
            raise ValueError("awaiting-decision mode requires a decision request")


@dataclass(frozen=True)
class LifecycleMutation:
    session: SessionState
    chain: ChainState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionState):
            raise ValueError("session must be a SessionState")
        if self.chain is not None and not isinstance(self.chain, ChainState):
            raise ValueError("chain must be a ChainState")


@dataclass(frozen=True)
class LifecycleDecision:
    kind: DecisionKind
    issues: tuple[LifecycleIssue, ...] = ()
    reason: str = ""
    mutation: LifecycleMutation | None = None

    def __post_init__(self) -> None:
        _enum(self.kind, DecisionKind, "kind")
        issues = tuple(self.issues)
        if not all(isinstance(issue, LifecycleIssue) for issue in issues):
            raise ValueError("issues must contain LifecycleIssue values")
        object.__setattr__(self, "issues", issues)
        if self.reason:
            object.__setattr__(
                self, "reason", _text(self.reason, limit=1600, label="reason")
            )
        elif not isinstance(self.reason, str):
            raise ValueError("reason must be text")
        if self.mutation is not None and not isinstance(
            self.mutation, LifecycleMutation
        ):
            raise ValueError("mutation must be a LifecycleMutation")
        if self.kind is not DecisionKind.ALLOW and not issues:
            raise ValueError("blocking decisions require at least one issue")


@dataclass(frozen=True)
class LifecycleSnapshot:
    chain: ChainState | None
    session: SessionState

    def __post_init__(self) -> None:
        if self.chain is not None and not isinstance(self.chain, ChainState):
            raise ValueError("chain must be a ChainState")
        if not isinstance(self.session, SessionState):
            raise ValueError("session must be a SessionState")


@dataclass(frozen=True)
class TerminalCandidate:
    candidate: Mapping[str, object]
    text: str
    path: str
    digest: str
    rendered_response: str
    predecessor: Mapping[str, object] | None = None
    predecessor_path: str | None = None
    predecessor_source_digest: str | None = None
    trusted_transition_hmac: str | None = None
    expected_chain_revision: int | None = None
    expected_session_revision: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate",
            _frozen_mapping(self.candidate, limit=131072, label="candidate"),
        )
        object.__setattr__(
            self, "text", _content(self.text, limit=131072, label="text")
        )
        object.__setattr__(self, "path", _text(self.path, limit=4096, label="path"))
        object.__setattr__(self, "digest", _digest(self.digest, "digest"))
        object.__setattr__(
            self,
            "rendered_response",
            _text(
                self.rendered_response,
                limit=16384,
                label="rendered_response",
            ),
        )
        if self.predecessor is not None:
            object.__setattr__(
                self,
                "predecessor",
                _frozen_mapping(self.predecessor, limit=131072, label="predecessor"),
            )
        object.__setattr__(
            self,
            "predecessor_path",
            _optional_text(self.predecessor_path, limit=4096, label="predecessor_path"),
        )
        for field_name in ("predecessor_source_digest", "trusted_transition_hmac"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _digest(value, field_name))
        for field_name in ("expected_chain_revision", "expected_session_revision"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _revision(value, field_name))


_APPROVALS = {
    "yes",
    "yeah",
    "yep",
    "approved",
    "looks right",
    "go ahead",
}
_REJECTIONS = {"no", "nope", "reject", "rejected"}
_SELECTION_APPROVAL_RE = re.compile(
    r"(?:(?:yes|yeah|yep)[, ]+)?go with [A-Za-z0-9][A-Za-z0-9._:-]{0,31}"
)
_DECISION_RESPONSE_RE = re.compile(
    r"Authorized work is paused for one required user decision\.\n\n"
    r"Decision needed: (?P<question>[^\n]+)\n"
    r"Blocked action field: (?P<field>[^\n]+)\n"
    r"Reason: (?P<reason>[^\n]+)"
)
_OPERATIONAL_PREFIX = (
    r"(?:(?:should|shall|can|may) (?:i|we)|"
    r"(?:would you like|do you want) me to)"
)
_OPERATIONAL_ACTION = r"(?:continue|stop|pause|proceed|carry on|keep (?:working|going))"
_OPERATION_ONLY_RE = re.compile(
    rf"{_OPERATIONAL_PREFIX} {_OPERATIONAL_ACTION}"
    r"(?: (?:with|on) (?:this|the|current|authorized) work)?"
    r"(?: now| here)?"
)
_PREFIX_FREE_OPERATION_ONLY_RE = re.compile(rf"{_OPERATIONAL_ACTION}(?: now| here)?")
_HANDOFF_ONLY_RE = re.compile(
    rf"{_OPERATIONAL_PREFIX} (?:create|write|make|generate) "
    r"(?:a |the )?handoff(?: .+)?"
)
_OPERATIONAL_MENU = (
    r"(?:continue|stop|pause)"
    r"(?: (?:or )?(?:continue|stop|pause|create (?:a |the )?handoff))+"
)
_OPERATIONAL_MENU_RE = re.compile(_OPERATIONAL_MENU)
_PREFIXED_OPERATIONAL_MENU_RE = re.compile(
    rf"{_OPERATIONAL_PREFIX} {_OPERATIONAL_MENU}"
)


def classify_affirmation(text: str) -> AffirmationResult:
    """Classify a bounded direct response without semantic inference."""

    if not isinstance(text, str):
        return AffirmationResult.AMBIGUOUS
    try:
        if len(text.encode("utf-8")) > 200:
            return AffirmationResult.AMBIGUOUS
    except UnicodeError:
        return AffirmationResult.AMBIGUOUS
    if any(
        unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
        for character in text
    ):
        return AffirmationResult.AMBIGUOUS
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = " ".join(normalized.split())
    normalized = normalized.rstrip(".!?…")
    normalized = normalized.strip()
    if normalized in _APPROVALS or _SELECTION_APPROVAL_RE.fullmatch(normalized):
        return AffirmationResult.APPROVE
    if normalized in _REJECTIONS:
        return AffirmationResult.REJECT
    return AffirmationResult.AMBIGUOUS


def _decision_text(value: object, label: str) -> str:
    text = _text(value, limit=400, label=label)
    if "\n" in text:
        raise ValueError(f"{label} must be a single line")
    return text


def _decision_text_hmac(secret: bytes, label: str, value: str) -> str:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    payload = f"agent-handoff-toolkit:{label}\0{value}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _is_operational_evasion(question: str) -> bool:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    normalized = re.sub(r"[,;:]+", " ", normalized)
    normalized = " ".join(normalized.rstrip(".!?…").split())
    return any(
        pattern.fullmatch(normalized) is not None
        for pattern in (
            _OPERATION_ONLY_RE,
            _PREFIX_FREE_OPERATION_ONLY_RE,
            _HANDOFF_ONLY_RE,
            _OPERATIONAL_MENU_RE,
            _PREFIXED_OPERATIONAL_MENU_RE,
        )
    )


def render_decision_response(
    question: str,
    reason: str,
    request: DecisionRequest,
    secret: bytes,
) -> str:
    """Render the only terminal response permitted for a decision request."""

    if not isinstance(request, DecisionRequest):
        raise ValueError("request must be a DecisionRequest")
    question = _decision_text(question, "question")
    reason = _decision_text(reason, "reason")
    if _is_operational_evasion(question):
        raise ValueError(
            "decision question cannot merely offer continue, stop, or handoff"
        )
    expected_question_hmac = _decision_text_hmac(secret, "question", question)
    expected_reason_hmac = _decision_text_hmac(secret, "reason", reason)
    if not hmac.compare_digest(request.question_hmac, expected_question_hmac):
        raise ValueError("question does not match its persisted HMAC")
    if not hmac.compare_digest(request.reason_hmac, expected_reason_hmac):
        raise ValueError("reason does not match its persisted HMAC")
    return (
        "Authorized work is paused for one required user decision.\n\n"
        f"Decision needed: {question}\n"
        f"Blocked action field: {request.blocked_action_field}\n"
        f"Reason: {reason}"
    )


def verify_decision_response(
    message: str,
    request: DecisionRequest,
    secret: bytes,
) -> bool:
    """Verify exact canonical decision-response lines and their keyed digests."""

    if not isinstance(message, str) or not isinstance(request, DecisionRequest):
        return False
    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    match = _DECISION_RESPONSE_RE.fullmatch(normalized)
    if match is None or match.group("field") != request.blocked_action_field:
        return False
    try:
        question = _decision_text(match.group("question"), "question")
        reason = _decision_text(match.group("reason"), "reason")
        if _is_operational_evasion(question):
            return False
        question_hmac = _decision_text_hmac(secret, "question", question)
        reason_hmac = _decision_text_hmac(secret, "reason", reason)
    except ValueError:
        return False
    return hmac.compare_digest(
        request.question_hmac, question_hmac
    ) and hmac.compare_digest(request.reason_hmac, reason_hmac)


def evaluate_pre_tool(
    event: NormalizedEvent,
    snapshot: LifecycleSnapshot,
    *,
    bootstrap_allowed: bool,
) -> LifecycleDecision:
    """Apply the pure capability gate before a tool invocation."""

    if event.event is not EventName.PRE_TOOL_USE:
        raise ValueError("evaluate_pre_tool requires a pre-tool-use event")
    if not isinstance(snapshot, LifecycleSnapshot):
        raise ValueError("snapshot must be a LifecycleSnapshot")
    if not isinstance(bootstrap_allowed, bool):
        raise ValueError("bootstrap_allowed must be boolean")
    if snapshot.session.mode is not EnforcementMode.UNTRACKED:
        return LifecycleDecision(DecisionKind.ALLOW)
    if event.tool_capability in {"intrinsic-read-only", "tool-free"}:
        return LifecycleDecision(DecisionKind.ALLOW)
    if bootstrap_allowed:
        return LifecycleDecision(DecisionKind.ALLOW)
    issue = LifecycleIssue(
        "AHK-PRETOOL-TRACKING",
        "Mutation-capable or unknown tools require an authorization root.",
        "Register, resume, join, or adopt an authorization root before using the tool.",
        actual=event.tool_capability or "missing",
    )
    return LifecycleDecision(
        DecisionKind.BLOCK,
        (issue,),
        f"{issue.code}: {issue.summary}\nCorrect by {issue.corrective_action}",
    )


def evaluate_user_prompt(
    event: NormalizedEvent,
    snapshot: LifecycleSnapshot,
    *,
    affirmation: AffirmationResult | None = None,
    decision_resolved: bool = False,
) -> LifecycleDecision:
    """Reset correction state only for a real external user turn."""

    if event.event is not EventName.USER_PROMPT_SUBMIT:
        raise ValueError("evaluate_user_prompt requires a user-prompt-submit event")
    if not isinstance(snapshot, LifecycleSnapshot):
        raise ValueError("snapshot must be a LifecycleSnapshot")
    if affirmation is not None:
        _enum(affirmation, AffirmationResult, "affirmation")
    if not isinstance(decision_resolved, bool):
        raise ValueError("decision_resolved must be boolean")
    if not event.external_user_turn:
        return LifecycleDecision(DecisionKind.ALLOW)
    next_mode = snapshot.session.mode
    pending_decision = snapshot.session.pending_decision_reference
    if next_mode is EnforcementMode.AWAITING_DECISION and decision_resolved:
        next_mode = EnforcementMode.TRACKED
        pending_decision = None
    next_session = replace(
        snapshot.session,
        targeted_revision=snapshot.session.targeted_revision + 1,
        mode=next_mode,
        pending_decision_reference=pending_decision,
        correction_cycle_count=0,
        last_issue_signature=None,
        current_external_user_turn_reference=(
            event.current_user_reference or event.turn_reference
        ),
    )
    return LifecycleDecision(
        DecisionKind.ALLOW,
        mutation=LifecycleMutation(session=next_session),
    )


def issue_signature(
    authorization_id: str,
    chain_revision: int,
    issues: Sequence[LifecycleIssue],
) -> str:
    """Identify an invariant-code set without incorporating cosmetic evidence."""

    authorization_id = _identifier(authorization_id, "authorization_id")
    chain_revision = _revision(chain_revision, "chain_revision")
    issue_values = tuple(issues)
    if not issue_values or not all(
        isinstance(issue, LifecycleIssue) for issue in issue_values
    ):
        raise ValueError("issues must contain at least one LifecycleIssue")
    payload = {
        "authorization_id": authorization_id,
        "chain_revision": chain_revision,
        "issue_codes": sorted({issue.code for issue in issue_values}),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _blocked_stop(
    snapshot: LifecycleSnapshot,
    issues: Sequence[LifecycleIssue],
) -> LifecycleDecision:
    issue_values = tuple(issues)
    authorization_id = snapshot.session.authorization_id
    chain_revision = snapshot.session.chain_revision
    if authorization_id is None or chain_revision is None:
        raise ValueError("blocking a tracked stop requires chain identity")
    signature = issue_signature(authorization_id, chain_revision, issue_values)
    next_count = snapshot.session.correction_cycle_count + 1
    repeated = signature == snapshot.session.last_issue_signature
    next_session = replace(
        snapshot.session,
        targeted_revision=snapshot.session.targeted_revision + 1,
        correction_cycle_count=next_count,
        last_issue_signature=signature,
    )
    mutation = LifecycleMutation(session=next_session)
    if next_count >= 3:
        circuit_issue = LifecycleIssue(
            "AHK-STOP-CIRCUIT",
            "Lifecycle enforcement reached the correction-cycle limit.",
            "A real external user turn is required before another terminal attempt.",
        )
        return LifecycleDecision(
            DecisionKind.POLICY_FAILURE,
            (*issue_values, circuit_issue),
            "AHK-STOP-CIRCUIT: Lifecycle enforcement stopped after three blocked attempts. "
            "A real external user turn is required.",
            mutation,
        )
    codes = ", ".join(issue.code for issue in issue_values)
    if repeated:
        reason = f"Same lifecycle issue(s): {codes}. Apply the prior corrective action."
    else:
        parts = [
            f"{issue.code}: {issue.summary}\nCorrect by {issue.corrective_action}"
            for issue in issue_values
        ]
        reason = "\n".join(parts)
    return LifecycleDecision(DecisionKind.BLOCK, issue_values, reason, mutation)


def _stop_issue(
    code: str,
    summary: str,
    corrective_action: str,
    *,
    expected: object = None,
    actual: object = None,
    candidate_path: str | None = None,
) -> LifecycleIssue:
    def evidence(value: object) -> str | None:
        if value is None:
            return None
        return str(value)[:4096]

    return LifecycleIssue(
        code,
        summary,
        corrective_action,
        expected=evidence(expected),
        actual=evidence(actual),
        candidate_path=candidate_path,
    )


def _stale_issues(
    snapshot: LifecycleSnapshot,
    candidate: TerminalCandidate | None,
) -> tuple[LifecycleIssue, ...]:
    chain = snapshot.chain
    session = snapshot.session
    mismatches: list[str] = []
    if chain is None:
        mismatches.append("tracked chain is missing")
    else:
        if session.authorization_id != chain.authorization_id:
            mismatches.append("session authorization differs from chain authorization")
        if session.chain_revision != chain.targeted_revision:
            mismatches.append("session chain revision differs from chain revision")
    if candidate is not None:
        if (
            candidate.expected_chain_revision is not None
            and candidate.expected_chain_revision
            != (chain.targeted_revision if chain is not None else None)
        ):
            mismatches.append("candidate chain revision is stale")
        if (
            candidate.expected_session_revision is not None
            and candidate.expected_session_revision != session.targeted_revision
        ):
            mismatches.append("candidate session revision is stale")
    if not mismatches:
        return ()
    return (
        _stop_issue(
            "AHK-STOP-STALE",
            "; ".join(mismatches),
            "Reload lifecycle state and rebuild the candidate against the targeted revisions.",
        ),
    )


def _mapped_successor_issues(
    validation_issues: Sequence[ValidationIssue],
    *,
    candidate_path: str,
) -> tuple[LifecycleIssue, ...]:
    categories: dict[str, list[str]] = {}
    for issue in validation_issues:
        if issue.code in {"lineage-root", "lineage-authorization"}:
            stop_code = "AHK-STOP-ROOT"
        elif issue.code in {
            "lineage-predecessor",
            "lineage-record",
            "lineage-schema",
        }:
            stop_code = "AHK-STOP-PREDECESSOR"
        elif issue.code in {
            "lineage-scope",
            "lineage-definition",
        } or issue.code.startswith("scope-"):
            stop_code = "AHK-STOP-SCOPE"
        else:
            stop_code = "AHK-STOP-WORK"
        categories.setdefault(stop_code, []).append(issue.message)
    corrective_actions = {
        "AHK-STOP-ROOT": "Render a successor for the locked authorized root or use a trusted approved transition.",
        "AHK-STOP-PREDECESSOR": "Render a direct successor of the current record using its exact path and source digest.",
        "AHK-STOP-SCOPE": "Restore the inherited ordered scope definitions before rendering the successor.",
        "AHK-STOP-WORK": "Correct the candidate record validation issues and render it again.",
    }
    return tuple(
        _stop_issue(
            code,
            "; ".join(messages)[:400],
            corrective_actions[code],
            candidate_path=candidate_path,
        )
        for code, messages in categories.items()
    )


def _allow_stop(
    snapshot: LifecycleSnapshot,
    *,
    chain: ChainState,
    mode: EnforcementMode,
) -> LifecycleDecision:
    next_session = replace(
        snapshot.session,
        targeted_revision=snapshot.session.targeted_revision + 1,
        mode=mode,
        authorization_id=chain.authorization_id,
        chain_revision=chain.targeted_revision,
        correction_cycle_count=0,
        last_issue_signature=None,
    )
    return LifecycleDecision(
        DecisionKind.ALLOW,
        mutation=LifecycleMutation(session=next_session, chain=chain),
    )


def evaluate_stop(
    event: NormalizedEvent,
    snapshot: LifecycleSnapshot,
    *,
    candidate: TerminalCandidate | None,
    decision_secret: bytes | None = None,
) -> LifecycleDecision:
    """Evaluate a terminal attempt without performing I/O or host operations."""

    if event.event is not EventName.STOP:
        raise ValueError("evaluate_stop requires a stop event")
    if not isinstance(snapshot, LifecycleSnapshot):
        raise ValueError("snapshot must be a LifecycleSnapshot")
    if candidate is not None and not isinstance(candidate, TerminalCandidate):
        raise ValueError("candidate must be a TerminalCandidate")
    session = snapshot.session
    if session.mode in {EnforcementMode.UNTRACKED, EnforcementMode.COMPLETE}:
        return LifecycleDecision(DecisionKind.ALLOW)

    stale = _stale_issues(snapshot, candidate)
    if stale:
        return _blocked_stop(snapshot, stale)
    chain = snapshot.chain
    assert chain is not None

    if session.mode is EnforcementMode.AWAITING_DECISION:
        request = session.pending_decision_reference
        verified = (
            request is not None
            and decision_secret is not None
            and event.latest_assistant_message is not None
            and verify_decision_response(
                event.latest_assistant_message, request, decision_secret
            )
        )
        if not verified:
            issue = _stop_issue(
                "AHK-STOP-DECISION",
                "The pending decision response is missing or noncanonical.",
                "Render exactly the registered decision response with no added text.",
            )
            return _blocked_stop(snapshot, (issue,))
        return _allow_stop(
            snapshot, chain=chain, mode=EnforcementMode.AWAITING_DECISION
        )

    if candidate is None:
        issue = _stop_issue(
            "AHK-STOP-WORK",
            "Authorized executable work remains without a valid terminal record.",
            "perform the existing authorized exact action or render its valid successor record.",
        )
        return _blocked_stop(snapshot, (issue,))

    message = event.latest_assistant_message
    normalized_message = (
        message.replace("\r\n", "\n").replace("\r", "\n")
        if message is not None
        else None
    )
    normalized_response = candidate.rendered_response.replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    if normalized_message != normalized_response:
        issue = _stop_issue(
            "AHK-STOP-RESPONSE",
            "The terminal assistant message differs from the rendered response.",
            "Emit only the complete canonical rendered response.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))

    candidate_data = _thaw_json(candidate.candidate)
    try:
        authoritative_candidate = parse_markdown(candidate.text)
    except ValueError:
        issue = _stop_issue(
            "AHK-STOP-WORK",
            "The candidate record text is not structurally valid.",
            "Correct and render the candidate record before stopping.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))
    if candidate_data != authoritative_candidate:
        issue = _stop_issue(
            "AHK-STOP-RESPONSE",
            "The supplied candidate mapping differs from the candidate record text.",
            "Parse the candidate text and evaluate that exact authoritative mapping.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))
    candidate_data = authoritative_candidate
    if chain.current_record_reference is None:
        return _evaluate_initial_record(snapshot, candidate, candidate_data)
    predecessor_data = (
        _thaw_json(candidate.predecessor) if candidate.predecessor is not None else None
    )
    if not isinstance(candidate_data, dict) or not isinstance(predecessor_data, dict):
        issue = _stop_issue(
            "AHK-STOP-PREDECESSOR",
            "The candidate does not contain a direct predecessor mapping.",
            "Render a direct successor of the current record.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))
    if (
        candidate.predecessor_path is None
        or candidate.predecessor_source_digest is None
    ):
        issue = _stop_issue(
            "AHK-STOP-PREDECESSOR",
            "The predecessor source path or digest is missing.",
            "Supply the exact current-record path and LF-normalized source digest.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))

    proposal = chain.publication_evidence
    trusted_transition = (
        proposal is not None
        and proposal.kind == "transition"
        and proposal.status in {"approved", "consumed"}
        and candidate.trusted_transition_hmac == proposal.evidence_hmac
        and predecessor_data.get("authorization_id") == proposal.from_authorization_id
        and candidate_data.get("authorization_id") == chain.authorization_id
        and candidate_data.get("transition")
        == {
            "from_authorization_id": proposal.from_authorization_id,
            "to_authorization_id": proposal.to_authorization_id,
            "old_root_scope_id": proposal.old_scopes[0]["scope_id"],
            "new_root_scope_id": proposal.new_scopes[0]["scope_id"],
            "proposal_turn_ref": proposal.assistant_turn_reference,
            "approval_turn_ref": proposal.approval_turn_reference,
            "evidence_hmac": proposal.evidence_hmac,
        }
    )
    validation_issues = validate_successor(
        candidate_data,
        predecessor_data,
        expected_predecessor_path=candidate.predecessor_path,
        predecessor_sha256=candidate.predecessor_source_digest,
        approved_transition_hmac=proposal.evidence_hmac if trusted_transition else None,
    )
    extra_issues: list[LifecycleIssue] = []
    if proposal is not None and not trusted_transition:
        extra_issues.append(
            _stop_issue(
                "AHK-STOP-ROOT",
                "The approved transition publication evidence does not match.",
                "Render the first successor using the chain's approved transition evidence.",
            )
        )
    predecessor_record_id = predecessor_data.get("record_id")
    if (
        predecessor_record_id != chain.current_record_reference.record_id
        or candidate.predecessor_path != chain.current_record_reference.path
        or candidate.predecessor_source_digest != chain.current_record_reference.sha256
    ):
        extra_issues.append(
            _stop_issue(
                "AHK-STOP-PREDECESSOR",
                "The predecessor is not the chain's current record.",
                "Rebuild the candidate from the current chain record.",
                expected=chain.current_record_reference,
                actual=predecessor_record_id,
                candidate_path=candidate.path,
            )
        )
    predecessor_authorization_id = predecessor_data.get("authorization_id")
    expected_predecessor_authorization = (
        proposal.from_authorization_id if trusted_transition else chain.authorization_id
    )
    if predecessor_authorization_id != expected_predecessor_authorization:
        extra_issues.append(
            _stop_issue(
                "AHK-STOP-PREDECESSOR",
                "The predecessor authorization does not match the live chain.",
                "Rebuild the candidate from the current live authorization chain.",
                expected=chain.authorization_id,
                actual=predecessor_authorization_id,
                candidate_path=candidate.path,
            )
        )
    candidate_root = candidate_data.get("authorized_root_scope_id")
    if (
        candidate_root != chain.locked_root_id
        or candidate_data.get("authorization_id") != chain.authorization_id
    ):
        extra_issues.append(
            _stop_issue(
                "AHK-STOP-ROOT",
                "The candidate does not retain the locked authorized root.",
                "Render a successor for the locked root.",
                expected=chain.locked_root_id,
                actual=candidate_root,
                candidate_path=candidate.path,
            )
        )
    predecessor_scopes = predecessor_data.get("active_scopes")
    if isinstance(predecessor_scopes, list):
        predecessor_scope_digests = tuple(
            scope.get("scope_definition_digest")
            for scope in predecessor_scopes
            if isinstance(scope, Mapping)
        )
        expected_scope_digests = (
            tuple(scope_definition_digest(scope) for scope in proposal.old_scopes)
            if trusted_transition
            else chain.scope_digests
        )
        candidate_scope_digests = tuple(
            scope.get("scope_definition_digest")
            for scope in candidate_data.get("active_scopes", [])
        )
        if (
            predecessor_scope_digests != expected_scope_digests
            or candidate_scope_digests != chain.scope_digests
        ):
            extra_issues.append(
                _stop_issue(
                    "AHK-STOP-SCOPE",
                    "The predecessor scope definitions differ from the locked chain.",
                    "Reload the current chain and retain its ordered scope definitions.",
                    candidate_path=candidate.path,
                )
            )
    if record_digest(candidate.text) != candidate.digest:
        extra_issues.append(
            _stop_issue(
                "AHK-STOP-PREDECESSOR",
                "The candidate source digest does not match its text.",
                "Reload and re-parse the candidate before evaluating it.",
                candidate_path=candidate.path,
            )
        )
    mapped = _mapped_successor_issues(validation_issues, candidate_path=candidate.path)
    combined_by_code = {issue.code: issue for issue in (*mapped, *extra_issues)}
    if combined_by_code:
        return _blocked_stop(snapshot, tuple(combined_by_code.values()))

    actual_response = render_terminal_response(candidate.path, candidate.text)
    normalized_actual_response = actual_response.replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    if normalized_actual_response != normalized_response:
        issue = _stop_issue(
            "AHK-STOP-RESPONSE",
            "The supplied response is not the renderer output for the candidate.",
            "Regenerate the complete terminal response from the candidate record.",
            candidate_path=candidate.path,
        )
        return _blocked_stop(snapshot, (issue,))

    record_id = candidate_data.get("record_id")
    authorization_id = candidate_data.get("authorization_id")
    root_id = candidate_data.get("authorized_root_scope_id")
    assert isinstance(record_id, str)
    assert isinstance(authorization_id, str)
    assert isinstance(root_id, str)
    active_scopes = candidate_data.get("active_scopes")
    assert isinstance(active_scopes, list)
    scope_digests = tuple(
        str(scope["scope_definition_digest"])
        for scope in active_scopes
        if isinstance(scope, Mapping)
    )
    record_type = candidate_data.get("record_type")
    completed = record_type == "completion-audit"
    next_chain = ChainState(
        authorization_id=authorization_id,
        locked_root_id=root_id,
        scope_digests=scope_digests,
        targeted_revision=chain.targeted_revision + 1,
        status="complete" if completed else "active",
        current_record_reference=RecordReference(
            record_id, candidate.path, candidate.digest
        ),
        authorization_user_turn_reference=chain.authorization_user_turn_reference,
        authorization_evidence_hmac=chain.authorization_evidence_hmac,
    )
    return _allow_stop(
        snapshot,
        chain=next_chain,
        mode=EnforcementMode.COMPLETE if completed else EnforcementMode.TRACKED,
    )


def _evaluate_initial_record(snapshot, candidate, data):
    """Install the first record only against the already registered authority."""
    chain = snapshot.chain
    proposal = snapshot.session.pending_transition_reference
    evidence = data.get("authorization_evidence", {})
    kind = "initial-user-turn"
    proposal_turn = None
    if proposal and proposal.status == "consumed":
        kind = (
            "v1-adoption" if proposal.kind == "v1-adoption" else "approved-transition"
        )
        proposal_turn = proposal.assistant_turn_reference
    expected_evidence = {
        "kind": kind,
        "user_turn_ref": chain.authorization_user_turn_reference,
        "proposal_turn_ref": proposal_turn,
        "evidence_hmac": chain.authorization_evidence_hmac,
    }
    valid = (
        not validate_markdown(candidate.text)
        and data.get("schema_version") == 2
        and data.get("authorization_id") == chain.authorization_id
        and data.get("authorized_root_scope_id") == chain.locked_root_id
        and tuple(
            scope.get("scope_definition_digest")
            for scope in data.get("active_scopes", [])
        )
        == chain.scope_digests
        and data.get("predecessor") is None
        and candidate.predecessor is None
        and evidence == expected_evidence
        and record_digest(candidate.text) == candidate.digest
        and render_terminal_response(candidate.path, candidate.text).replace(
            "\r\n", "\n"
        )
        == candidate.rendered_response.replace("\r\n", "\n")
    )
    if not valid:
        return _blocked_stop(
            snapshot,
            (
                _stop_issue(
                    "AHK-STOP-ROOT",
                    "Initial record differs from registered authority.",
                    "Render the first record with the registered root, immutable definitions, and user-turn evidence.",
                ),
            ),
        )
    completed = data["record_type"] == "completion-audit"
    updated = replace(
        chain,
        targeted_revision=chain.targeted_revision + 1,
        status="complete" if completed else "active",
        current_record_reference=RecordReference(
            data["record_id"], candidate.path, candidate.digest
        ),
    )
    return _allow_stop(
        snapshot,
        chain=updated,
        mode=EnforcementMode.COMPLETE if completed else EnforcementMode.TRACKED,
    )
