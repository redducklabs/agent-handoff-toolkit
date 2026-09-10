"""Explicit lifecycle control and narrowly bounded bootstrap command parsing."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import secrets
from types import MappingProxyType

from .lineage import (
    canonical_json_bytes,
    evidence_hmac,
    record_digest as source_digest,
    scope_definition_digest,
    validate_identifier,
    validate_scope_definition,
)
from .lifecycle import (
    AffirmationResult,
    AuthorityCategory,
    AuthorizationProposal,
    ChainState,
    DecisionRequest,
    EnforcementMode,
    EventName,
    LifecycleMutation,
    RecordReference,
    classify_affirmation,
    render_decision_response,
)
from .lifecycle_storage import LocalLifecycleStorage, StaleLifecycleState
from .records import parse_markdown, validate_markdown


SCOPE_KINDS = {"unit", "issue", "phase", "epic", "rollout", "standalone"}
_FLAGS = {
    "register-root": (
        "challenge",
        "scope-id",
        "scope-kind",
        "scope-definition-b64",
        "expected-session-revision",
    ),
    "resume": ("challenge", "record", "expected-session-revision"),
    "join": (
        "challenge",
        "authorization-id",
        "expected-chain-revision",
        "expected-session-revision",
    ),
    "adopt-v1": ("challenge", "record", "expected-session-revision"),
}


@dataclass(frozen=True)
class BootstrapCommand:
    operation: str
    arguments: Mapping[str, str | int]

    def __post_init__(self):
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


def canonical_record_path(value: str) -> str:
    """Validate a platform-independent, absolute single-record pointer."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("invalid record path")
    normalized = value.replace("\\", "/")
    if re.fullmatch(r"(?:[A-Z]:/|/)[A-Za-z0-9._/-]+\.md", normalized) is None:
        raise ValueError("record path must be absolute Markdown")
    if normalized.startswith("//") or any(
        part in {"", ".", ".."} for part in normalized.split("/")[1:]
    ):
        raise ValueError("record path must be canonical")
    if str(PurePosixPath(normalized)) != normalized:
        raise ValueError("record path must be canonical")
    return normalized


def decode_scope_definition(value: str) -> dict[str, str]:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise ValueError("invalid base64url scope definition")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        definition = validate_scope_definition(json.loads(raw.decode("utf-8")))
        canonical = canonical_json_bytes(definition)
        if (
            canonical != raw
            or base64.urlsafe_b64encode(canonical).decode().rstrip("=") != value
        ):
            raise ValueError("scope definition must be canonical JSON")
        return definition
    except (UnicodeError, ValueError) as error:
        raise ValueError("invalid canonical scope definition") from error


def parse_bootstrap_command(
    command: str, runner_path: Path, challenge: str
) -> BootstrapCommand | None:
    """Parse fixed tokens without shell evaluation; caller supplies the live challenge."""
    try:
        validate_identifier(challenge, label="challenge")
        if (
            not isinstance(command, str)
            or len(command) > 8192
            or re.fullmatch(r"[A-Za-z0-9._:/\\ -]+", command) is None
        ):
            return None
        tokens = command.split(" ")
        if (
            len(tokens) < 4
            or "" in tokens
            or tokens[0] != "python"
            or tokens[2] != "lifecycle"
        ):
            return None
        runner_token = tokens[1].replace("\\", "/")
        if (
            runner_token != ".agent-handoff-toolkit/runner.py"
            and runner_token != runner_path.resolve().as_posix()
        ):
            return None
        if Path(runner_token).resolve() != runner_path.resolve():
            return None
        operation = tokens[3]
        flags = _FLAGS.get(operation)
        if flags is None or len(tokens) != 4 + len(flags) * 2:
            return None
        arguments = {}
        for index, flag in enumerate(flags):
            if tokens[4 + index * 2] != "--" + flag:
                return None
            value = tokens[5 + index * 2]
            if flag.endswith("revision"):
                if re.fullmatch(r"0|[1-9][0-9]{0,15}", value) is None:
                    return None
                value = int(value)
            elif flag == "record":
                value = canonical_record_path(value)
            elif flag == "scope-definition-b64":
                decode_scope_definition(value)
            else:
                validate_identifier(value, label=flag)
            arguments[flag.replace("-", "_")] = value
        if arguments["challenge"] != challenge:
            return None
        if operation == "register-root" and arguments["scope_kind"] not in SCOPE_KINDS:
            return None
        return BootstrapCommand(operation, arguments)
    except (ValueError, OSError, TypeError):
        return None


def _immutable_scopes(scopes):
    if not isinstance(scopes, (list, tuple)) or len(scopes) > 64:
        raise ValueError("invalid scopes")
    result = []
    seen = set()
    for scope in scopes:
        definition = {
            key: scope[key]
            for key in ("scope_id", "scope_kind", "parent_scope_id", "scope_definition")
        }
        scope_definition_digest(definition)
        if (
            definition["scope_kind"] not in SCOPE_KINDS
            or definition["scope_id"] in seen
        ):
            raise ValueError("invalid or duplicate scope")
        parent = definition["parent_scope_id"]
        if (not result and parent is not None) or (result and parent not in seen):
            raise ValueError("scopes require one root and parents before children")
        result.append(definition)
        seen.add(definition["scope_id"])
    return tuple(result)


class LifecycleService:
    """Use immutable model mutations and targeted CAS for every durable change.

    Host adapters supply transient record bytes and trusted normalized user events.
    They must verify transcript roles/adjacency before calling observe_user_turn.
    No CLI operation can assert that a user approved a proposal.
    """

    def __init__(self, storage: LocalLifecycleStorage, session_id: str):
        self.storage = storage
        self.session_id = session_id

    def _snapshot(self, expected_session_revision, expected_chain_revision=None):
        for revision in (expected_session_revision, expected_chain_revision):
            if revision is not None and (type(revision) is not int or revision < 0):
                raise ValueError("invalid expected revision")
        snapshot = self.storage.load_snapshot(self.session_id)
        if snapshot.session.targeted_revision != expected_session_revision:
            raise StaleLifecycleState("targeted session revision changed")
        if (
            expected_chain_revision is not None
            and (snapshot.chain.targeted_revision if snapshot.chain else 0)
            != expected_chain_revision
        ):
            raise StaleLifecycleState("targeted chain revision changed")
        if (
            snapshot.chain
            and snapshot.session.chain_revision != snapshot.chain.targeted_revision
        ):
            raise StaleLifecycleState("session requires chain reconciliation")
        return snapshot

    def _bootstrap(self, challenge, expected_session_revision):
        validate_identifier(challenge, label="challenge")
        snapshot = self._snapshot(expected_session_revision)
        if snapshot.session.mode is not EnforcementMode.UNTRACKED:
            raise ValueError("bootstrap requires an untracked session")
        if (
            snapshot.session.current_external_user_turn_reference is None
            or snapshot.session.bootstrap_challenge is None
            or not hmac.compare_digest(snapshot.session.bootstrap_challenge, challenge)
        ):
            raise ValueError("invalid or expired bootstrap challenge")
        return snapshot

    def _commit(self, snapshot, session, chain=None, expected_chain_revision=None):
        return self.storage.compare_and_swap(
            self.session_id,
            expected_chain_revision
            if expected_chain_revision is not None
            else (snapshot.chain.targeted_revision if snapshot.chain else 0),
            snapshot.session.targeted_revision,
            LifecycleMutation(session, chain),
        )

    def inspect(self):
        snapshot = self.storage.load_snapshot(self.session_id)
        session, chain = snapshot.session, snapshot.chain
        proposal = session.pending_transition_reference
        return {
            "session_id": session.session_key,
            "session_revision": session.targeted_revision,
            "authorization_id": session.authorization_id,
            "chain_revision": chain.targeted_revision if chain else None,
            "mode": session.mode.value,
            "root_scope_id": chain.locked_root_id if chain else None,
            "pending_state": "decision"
            if session.pending_decision_reference
            else (
                proposal.kind if proposal and proposal.status != "consumed" else None
            ),
            "issue_codes": (
                ["AHK-STATE-STALE"]
                if chain and session.chain_revision != chain.targeted_revision
                else []
            )
            + (
                ["AHK-V1-SEMANTICS-UNPROVEN"]
                if proposal
                and proposal.kind == "v1-adoption"
                and proposal.status == "consumed"
                else []
            ),
        }

    def register_root(
        self,
        *,
        challenge,
        scope_id,
        scope_kind,
        scope_definition_b64,
        expected_session_revision,
    ):
        snapshot = self._bootstrap(challenge, expected_session_revision)
        scope = _immutable_scopes(
            [
                {
                    "scope_id": scope_id,
                    "scope_kind": scope_kind,
                    "parent_scope_id": None,
                    "scope_definition": decode_scope_definition(scope_definition_b64),
                }
            ]
        )[0]
        digests = (scope_definition_digest(scope),)
        if any(
            chain.status == "active"
            and chain.locked_root_id == scope_id
            and chain.scope_digests == digests
            for chain in self.storage.load_registry().chains.values()
        ):
            raise ValueError("identical active root exists; use explicit join")
        authorization_id = "auth-" + secrets.token_hex(16)
        turn = snapshot.session.current_external_user_turn_reference
        signature = evidence_hmac(
            self.storage.secret,
            {
                "kind": "initial-user-turn",
                "authorization_id": authorization_id,
                "user_turn_ref": turn,
                "proposal_turn_ref": None,
                "role": "user",
                "adjacent": True,
                "scope_digests": list(digests),
            },
        )
        chain = ChainState(
            authorization_id,
            scope_id,
            digests,
            1,
            "active",
            None,
            authorization_user_turn_reference=turn,
            authorization_evidence_hmac=signature,
        )
        session = replace(
            snapshot.session,
            targeted_revision=expected_session_revision + 1,
            mode=EnforcementMode.TRACKED,
            authorization_id=authorization_id,
            chain_revision=1,
            bootstrap_challenge=None,
        )
        return self._commit(snapshot, session, chain)

    def join(
        self,
        *,
        challenge,
        authorization_id,
        expected_chain_revision,
        expected_session_revision,
    ):
        snapshot = self._bootstrap(challenge, expected_session_revision)
        validate_identifier(authorization_id, label="authorization_id")
        chain = self.storage.load_chain(authorization_id)
        if chain is None or chain.status != "active":
            raise ValueError("join requires an existing active chain")
        if (
            type(expected_chain_revision) is not int
            or chain.targeted_revision != expected_chain_revision
        ):
            raise StaleLifecycleState("targeted chain revision changed")
        session = replace(
            snapshot.session,
            targeted_revision=expected_session_revision + 1,
            mode=EnforcementMode.TRACKED,
            authorization_id=authorization_id,
            chain_revision=expected_chain_revision,
            bootstrap_challenge=None,
        )
        return self._commit(
            snapshot, session, expected_chain_revision=expected_chain_revision
        )

    def resume(
        self,
        *,
        challenge,
        record_path,
        record_text,
        record_metadata,
        record_digest,
        expected_session_revision,
    ):
        self._bootstrap(challenge, expected_session_revision)
        path = canonical_record_path(record_path)
        if (
            not isinstance(record_text, str)
            or len(record_text.encode("utf-8")) > 131072
        ):
            raise ValueError("record exceeds input bound")
        parsed = parse_markdown(record_text)
        if (
            parsed != record_metadata
            or source_digest(record_text) != record_digest
            or validate_markdown(record_text)
        ):
            raise ValueError("record bytes, metadata, or digest mismatch")
        if (
            parsed.get("schema_version") != 2
            or parsed.get("record_type") != "continuation"
        ):
            raise ValueError("resume requires one v2 continuation")
        chain = self.storage.load_chain(parsed["authorization_id"])
        reference = RecordReference(parsed["record_id"], path, record_digest)
        if (
            chain is None
            or chain.status != "active"
            or chain.current_record_reference != reference
            or chain.locked_root_id != parsed["authorized_root_scope_id"]
            or chain.scope_digests
            != tuple(
                scope["scope_definition_digest"] for scope in parsed["active_scopes"]
            )
        ):
            raise ValueError("record differs from the live chain")
        return self.join(
            challenge=challenge,
            authorization_id=chain.authorization_id,
            expected_chain_revision=chain.targeted_revision,
            expected_session_revision=expected_session_revision,
        )

    def propose_transition(
        self,
        *,
        old_scopes,
        new_scopes,
        assistant_turn_reference,
        expected_chain_revision,
        expected_session_revision,
        kind="transition",
        selected_record=None,
    ):
        snapshot = self._snapshot(expected_session_revision, expected_chain_revision)
        if snapshot.session.pending_decision_reference is not None:
            raise ValueError(
                "resolve the pending decision before proposing scope changes"
            )
        pending = snapshot.session.pending_transition_reference
        old, new = _immutable_scopes(old_scopes), _immutable_scopes(new_scopes)
        if (
            pending is not None
            and pending.status != "consumed"
            and (
                pending.status != "pending"
                or pending.kind != kind
                or pending.old_scopes != old
                or pending.new_scopes != new
                or pending.selected_record != selected_record
            )
        ):
            raise ValueError("an authorization proposal is already pending")
        if not new:
            raise ValueError("proposal requires a new root")
        if kind == "transition":
            if (
                snapshot.chain is None
                or snapshot.chain.status != "active"
                or tuple(scope_definition_digest(scope) for scope in old)
                != snapshot.chain.scope_digests
                or old[0]["scope_id"] != snapshot.chain.locked_root_id
            ):
                raise ValueError(
                    "old immutable definitions differ from the active chain"
                )
        elif kind == "v1-adoption":
            if snapshot.chain is not None or old:
                raise ValueError("v1 adoption requires an untracked session")
        proposal = AuthorizationProposal(
            "proposal-" + secrets.token_hex(16),
            kind,
            assistant_turn_reference,
            snapshot.chain.authorization_id if snapshot.chain else None,
            "auth-" + secrets.token_hex(16),
            old,
            new,
            selected_record,
        )
        return self._commit(
            snapshot,
            replace(
                snapshot.session,
                targeted_revision=expected_session_revision + 1,
                pending_transition_reference=proposal,
            ),
        )

    def observe_user_turn(
        self,
        event,
        *,
        preceding_assistant_turn_reference,
        expected_chain_revision,
        expected_session_revision,
    ):
        snapshot = self._snapshot(expected_session_revision, expected_chain_revision)
        if (
            event.event is not EventName.USER_PROMPT_SUBMIT
            or event.session_key != snapshot.session.session_key
        ):
            raise ValueError("event does not identify this user-prompt session")
        if not event.external_user_turn:
            return snapshot
        turn = event.current_user_reference or event.turn_reference
        if turn == snapshot.session.current_external_user_turn_reference:
            return snapshot
        proposal = snapshot.session.pending_transition_reference
        chain = None
        if (
            proposal
            and proposal.status == "pending"
            and proposal.assistant_turn_reference == preceding_assistant_turn_reference
            and turn != snapshot.session.current_external_user_turn_reference
        ):
            classification = classify_affirmation(event.current_user_message)
            if classification is AffirmationResult.REJECT:
                proposal = None
            elif classification is AffirmationResult.APPROVE:
                digests = tuple(
                    scope_definition_digest(scope) for scope in proposal.new_scopes
                )
                signature = evidence_hmac(
                    self.storage.secret,
                    {
                        "kind": proposal.kind,
                        "proposal_id": proposal.proposal_id,
                        "from_authorization_id": proposal.from_authorization_id,
                        "to_authorization_id": proposal.to_authorization_id,
                        "proposal_turn_ref": proposal.assistant_turn_reference,
                        "user_turn_ref": turn,
                        "old_scope_digests": [
                            scope_definition_digest(scope)
                            for scope in proposal.old_scopes
                        ],
                        "scope_digests": list(digests),
                        "role": "user",
                        "adjacent": True,
                        "selected_record": {
                            "record_id": proposal.selected_record.record_id,
                            "path": proposal.selected_record.path,
                            "sha256": proposal.selected_record.sha256,
                        }
                        if proposal.selected_record
                        else None,
                    },
                )
                proposal = replace(
                    proposal,
                    status="approved",
                    approval_turn_reference=turn,
                    evidence_hmac=signature,
                )
                if proposal.kind == "transition":
                    chain = ChainState(
                        proposal.to_authorization_id,
                        proposal.new_scopes[0]["scope_id"],
                        digests,
                        1,
                        "active",
                        snapshot.chain.current_record_reference,
                        authorization_user_turn_reference=turn,
                        authorization_evidence_hmac=signature,
                    )
                    proposal = replace(proposal, status="consumed")
        session = replace(
            snapshot.session,
            targeted_revision=expected_session_revision + 1,
            current_external_user_turn_reference=turn,
            bootstrap_challenge="challenge-" + secrets.token_hex(16),
            pending_transition_reference=proposal,
            correction_cycle_count=0,
            last_issue_signature=None,
        )
        if chain:
            session = replace(
                session,
                mode=EnforcementMode.TRACKED,
                authorization_id=chain.authorization_id,
                chain_revision=1,
                bootstrap_challenge=None,
            )
        return self._commit(snapshot, session, chain)

    def request_decision(
        self,
        *,
        question,
        reason,
        category,
        blocked_action_field,
        blocked_action_value,
        expected_chain_revision,
        expected_session_revision,
    ):
        snapshot = self._snapshot(expected_session_revision, expected_chain_revision)
        if (
            snapshot.chain is None
            or snapshot.chain.status != "active"
            or snapshot.session.mode is not EnforcementMode.TRACKED
        ):
            raise ValueError("decision requires a tracked active chain")

        def text_hmac(label, value):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 400:
                raise ValueError("invalid decision content length")
            return hmac.new(
                self.storage.secret,
                f"agent-handoff-toolkit:{label}\0{value}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

        request = DecisionRequest(
            "decision-" + secrets.token_hex(16),
            snapshot.chain.authorization_id,
            AuthorityCategory(category),
            blocked_action_field,
            blocked_action_value,
            text_hmac("question", question),
            text_hmac("reason", reason),
        )
        response = render_decision_response(
            question, reason, request, self.storage.secret
        )
        self._commit(
            snapshot,
            replace(
                snapshot.session,
                targeted_revision=expected_session_revision + 1,
                mode=EnforcementMode.AWAITING_DECISION,
                pending_decision_reference=request,
            ),
        )
        return response

    def adopt_v1(
        self, *, challenge, record_path, record_text, expected_session_revision
    ):
        snapshot = self._bootstrap(challenge, expected_session_revision)
        proposal = snapshot.session.pending_transition_reference
        path = canonical_record_path(record_path)
        if (
            proposal is None
            or proposal.kind != "v1-adoption"
            or proposal.status != "approved"
            or proposal.approval_turn_reference
            != snapshot.session.current_external_user_turn_reference
            or proposal.selected_record.path != path
        ):
            raise ValueError(
                "adoption requires adjacent approval of the selected v1 record"
            )
        if (
            len(record_text.encode("utf-8")) > 131072
            or source_digest(record_text) != proposal.selected_record.sha256
            or validate_markdown(record_text)
        ):
            raise ValueError("selected v1 record changed or is invalid")
        parsed = parse_markdown(record_text)
        if (
            parsed.get("schema_version") != 1
            or parsed.get("record_type") != "continuation"
        ):
            raise ValueError("adoption requires one v1 continuation")
        roots = [
            scope for scope in parsed["active_scopes"] if scope["highest_authorized"]
        ]
        if (
            len(roots) != 1
            or roots[0]["scope_id"] != proposal.new_scopes[0]["scope_id"]
        ):
            raise ValueError("selected v1 root differs from the approved root")
        identity_fields = ("scope_id", "scope_kind", "parent_scope_id")
        if [
            tuple(scope[key] for key in identity_fields)
            for scope in parsed["active_scopes"]
        ] != [
            tuple(scope[key] for key in identity_fields)
            for scope in proposal.new_scopes
        ]:
            raise ValueError("selected v1 scopes differ from the approved identities")
        chain = ChainState(
            proposal.to_authorization_id,
            roots[0]["scope_id"],
            tuple(scope_definition_digest(scope) for scope in proposal.new_scopes),
            1,
            "active",
            None,
            authorization_user_turn_reference=proposal.approval_turn_reference,
            authorization_evidence_hmac=proposal.evidence_hmac,
        )
        session = replace(
            snapshot.session,
            targeted_revision=expected_session_revision + 1,
            mode=EnforcementMode.TRACKED,
            authorization_id=chain.authorization_id,
            chain_revision=1,
            bootstrap_challenge=None,
            pending_transition_reference=replace(proposal, status="consumed"),
        )
        return self._commit(snapshot, session, chain)
