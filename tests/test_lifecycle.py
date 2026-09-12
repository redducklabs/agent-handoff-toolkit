from dataclasses import FrozenInstanceError, fields
import hashlib
import hmac
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.lifecycle import (  # noqa: E402
    AffirmationResult,
    AuthorityCategory,
    ChainState,
    DecisionKind,
    DecisionRequest,
    EnforcementMode,
    EventName,
    LifecycleDecision,
    LifecycleIssue,
    LifecycleMutation,
    LifecycleSnapshot,
    NormalizedEvent,
    RecordReference,
    SessionState,
    TerminalCandidate,
    classify_affirmation,
    evaluate_pre_tool,
    evaluate_stop,
    evaluate_user_prompt,
    issue_signature,
    render_decision_response,
    verify_decision_response,
)
from agent_handoff_toolkit.lineage import (  # noqa: E402
    record_digest,
    scope_definition_digest,
)
from agent_handoff_toolkit.records import (  # noqa: E402
    parse_markdown,
    render_record,
    render_terminal_response,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
LOCKED_SCOPE_DIGEST = "85ac1ab47671deca9e162ce2505a806038530e67e3bad3359e884d4085270e5b"
SECRET = b"decision-test-secret"
FIXTURES = ROOT / "tests" / "fixtures"
PREDECESSOR_PATH = "D:/repo/handoffs/record-001.md"
CANDIDATE_PATH = "D:/repo/handoffs/record-002.md"


def text_hmac(label: str, value: str) -> str:
    payload = f"agent-handoff-toolkit:{label}\x00{value}".encode()
    return hmac.new(SECRET, payload, hashlib.sha256).hexdigest()


def make_request(question: str, reason: str, **overrides: object) -> DecisionRequest:
    values: dict[str, object] = {
        "request_id": "decision-1",
        "authorization_id": "auth-1",
        "category": AuthorityCategory.MISSING_INPUT,
        "blocked_action_field": "target",
        "blocked_action_value": "production",
        "question_hmac": text_hmac("question", question),
        "reason_hmac": text_hmac("reason", reason),
    }
    values.update(overrides)
    return DecisionRequest(**values)  # type: ignore[arg-type]


def make_session(**overrides: object) -> SessionState:
    values: dict[str, object] = {
        "session_key": "session-1",
        "targeted_revision": 2,
        "mode": EnforcementMode.TRACKED,
        "authorization_id": "auth-1",
        "chain_revision": 4,
        "current_external_user_turn_reference": "turn-1",
        "bootstrap_challenge": "challenge-1",
    }
    values.update(overrides)
    return SessionState(**values)  # type: ignore[arg-type]


def make_chain(**overrides: object) -> ChainState:
    values: dict[str, object] = {
        "authorization_id": "auth-1",
        "locked_root_id": "issue-1",
        "scope_digests": (LOCKED_SCOPE_DIGEST,),
        "targeted_revision": 4,
        "status": "active",
        "current_record_reference": RecordReference(
            "record-1",
            PREDECESSOR_PATH,
            record_digest(
                render_record(make_record("continuation", record_id="record-1"))
            ),
        ),
    }
    values.update(overrides)
    return ChainState(**values)  # type: ignore[arg-type]


def make_event(event_name: EventName, **overrides: object) -> NormalizedEvent:
    values: dict[str, object] = {
        "host": "codex",
        "event": event_name,
        "session_key": "session-1",
        "turn_reference": "turn-2",
        "repository_root": "D:/repo",
        "transcript_reference": "transcript-1",
        "stop_hook_active": False,
        "latest_assistant_message": None,
        "current_user_message": None,
        "current_user_reference": None,
        "tool_name": None,
        "tool_input": None,
        "tool_capability": None,
        "external_user_turn": False,
    }
    values.update(overrides)
    return NormalizedEvent(**values)  # type: ignore[arg-type]


def make_candidate(**overrides: object) -> TerminalCandidate:
    values: dict[str, object] = {
        "candidate": {"record_id": "record-2"},
        "text": "record text",
        "path": "D:/repo/handoff.md",
        "digest": DIGEST_B,
        "rendered_response": "canonical response",
        "predecessor": {"record_id": "record-1"},
        "predecessor_path": "D:/repo/previous.md",
        "predecessor_source_digest": DIGEST_A,
        "trusted_transition_hmac": None,
        "expected_chain_revision": 4,
        "expected_session_revision": 2,
    }
    values.update(overrides)
    return TerminalCandidate(**values)  # type: ignore[arg-type]


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def restore_v1_sections(data: dict[str, object]) -> dict[str, object]:
    """Restore the derived sections schema v1 still renders but v2 dropped."""

    source = load_fixture(
        "continuation.json"
        if data["record_type"] == "continuation"
        else "completion-audit.json"
    )
    sections = data["sections"]
    origin = source["sections"]
    assert isinstance(sections, dict) and isinstance(origin, dict)
    for name, value in origin.items():
        sections.setdefault(name, value)
    return data


def make_scope(
    scope_id: str,
    *,
    remaining_work: bool,
    remaining_code: bool,
    status: str,
) -> dict[str, object]:
    scope: dict[str, object] = {
        "scope_id": scope_id,
        "scope_kind": "issue",
        "parent_scope_id": None,
        "highest_authorized": True,
        "scope_definition": {
            "title": f"Scope {scope_id}",
            "outcome": f"Complete the authorized outcome for {scope_id}.",
        },
        "remaining_work": remaining_work,
        "remaining_code": remaining_code,
        "remaining_code_detail": (
            "Implementation remains."
            if remaining_code
            else "No code remains within this scope."
        ),
        "status": status,
    }
    scope["scope_definition_digest"] = scope_definition_digest(scope)
    return scope


def make_record(
    record_type: str,
    *,
    record_id: str,
    root: str = "issue-1",
    predecessor: object = None,
) -> dict[str, object]:
    data = load_fixture(
        "continuation.json"
        if record_type == "continuation"
        else "completion-audit.json"
    )
    remaining = record_type == "continuation"
    data.update(
        {
            "schema_version": 2,
            "record_id": record_id,
            "authorization_id": "auth-1",
            "authorized_root_scope_id": root,
            "predecessor": predecessor,
            "authorization_evidence": {
                "kind": "initial-user-turn",
                "user_turn_ref": "turn-user-1",
                "proposal_turn_ref": None,
                "evidence_hmac": "1" * 64,
            },
            "transition": None,
            "active_scopes": [
                make_scope(
                    root,
                    remaining_work=remaining,
                    remaining_code=remaining,
                    status="in-progress" if remaining else "complete",
                )
            ],
        }
    )
    if record_type == "completion-audit":
        data["completed_scope_id"] = root
    # Schema v2 keeps no narrative copy of a metadata field, so an author
    # supplies only the sections that have no metadata equivalent.
    sections = data["sections"]
    assert isinstance(sections, dict)
    for name in (
        "Verification evidence",
        "Exact next action",
        "Remaining code by active scope",
        "Next-session prompt",
    ):
        sections.pop(name, None)
    return data


def make_terminal_candidate(record_type: str = "continuation") -> TerminalCandidate:
    predecessor_source = make_record("continuation", record_id="record-1")
    predecessor_text = render_record(predecessor_source)
    predecessor = parse_markdown(predecessor_text)
    predecessor_digest = record_digest(predecessor_text)
    reference = {
        "record_id": "record-1",
        "path": PREDECESSOR_PATH,
        "sha256": predecessor_digest,
    }
    candidate_source = make_record(
        record_type,
        record_id="record-2",
        predecessor=reference,
    )
    candidate_text = render_record(candidate_source)
    candidate = parse_markdown(candidate_text)
    return TerminalCandidate(
        candidate=candidate,
        text=candidate_text,
        path=CANDIDATE_PATH,
        digest=record_digest(candidate_text),
        rendered_response=render_terminal_response(CANDIDATE_PATH, candidate_text),
        predecessor=predecessor,
        predecessor_path=PREDECESSOR_PATH,
        predecessor_source_digest=predecessor_digest,
        expected_chain_revision=4,
        expected_session_revision=2,
    )


def mutable_json(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_json(item) for item in value]
    return value


def make_candidate_from_valid(
    source: TerminalCandidate, candidate_data: dict[str, object]
) -> TerminalCandidate:
    candidate_text = render_record(candidate_data)
    parsed_candidate = parse_markdown(candidate_text)
    return TerminalCandidate(
        candidate=parsed_candidate,
        text=candidate_text,
        path=source.path,
        digest=record_digest(candidate_text),
        rendered_response=render_terminal_response(source.path, candidate_text),
        predecessor=source.predecessor,
        predecessor_path=source.predecessor_path,
        predecessor_source_digest=source.predecessor_source_digest,
        trusted_transition_hmac=source.trusted_transition_hmac,
        expected_chain_revision=source.expected_chain_revision,
        expected_session_revision=source.expected_session_revision,
    )


def make_terminal_candidate_with(
    source: TerminalCandidate, **overrides: object
) -> TerminalCandidate:
    values: dict[str, object] = {
        "candidate": source.candidate,
        "text": source.text,
        "path": source.path,
        "digest": source.digest,
        "rendered_response": source.rendered_response,
        "predecessor": source.predecessor,
        "predecessor_path": source.predecessor_path,
        "predecessor_source_digest": source.predecessor_source_digest,
        "trusted_transition_hmac": source.trusted_transition_hmac,
        "expected_chain_revision": source.expected_chain_revision,
        "expected_session_revision": source.expected_session_revision,
    }
    values.update(overrides)
    return TerminalCandidate(**values)  # type: ignore[arg-type]


class EnumTests(unittest.TestCase):
    def test_enums_expose_the_required_wire_values(self) -> None:
        self.assertEqual(EventName.STOP.value, "stop")
        self.assertEqual(EnforcementMode.AWAITING_DECISION.value, "awaiting-decision")
        self.assertEqual(DecisionKind.POLICY_FAILURE.value, "policy-failure")
        self.assertEqual(
            AuthorityCategory.MUTUALLY_EXCLUSIVE_CHOICE.value,
            "mutually-exclusive-choice",
        )


class AffirmationTests(unittest.TestCase):
    def test_accepts_bounded_unambiguous_adjacent_affirmatives(self) -> None:
        for value in (
            "yes",
            "YEAH!",
            "yep.",
            "approved",
            "looks right",
            "go ahead",
            "go with 2",
            "yeah, go with 2",
        ):
            with self.subTest(value=value):
                self.assertEqual(classify_affirmation(value), AffirmationResult.APPROVE)

    def test_rejects_direct_negatives(self) -> None:
        for value in ("no", "nope", "reject", "rejected"):
            with self.subTest(value=value):
                self.assertEqual(classify_affirmation(value), AffirmationResult.REJECT)

    def test_marks_qualified_unrelated_unsafe_and_overlong_text_ambiguous(self) -> None:
        for value in (
            "yes, but change the root",
            "I have been considering this proposal",
            "yes\x00",
            "yes " + "please " * 100,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    classify_affirmation(value), AffirmationResult.AMBIGUOUS
                )


class FrozenModelTests(unittest.TestCase):
    def test_models_are_frozen_and_snapshot_caller_owned_values(self) -> None:
        tool_input = {"path": "D:/repo/file.py", "flags": ["one"]}
        event = make_event(
            EventName.PRE_TOOL_USE,
            tool_name="write",
            tool_input=tool_input,
            tool_capability="mutation-capable",
        )
        candidate_data = {"record_id": "record-2", "values": ["one"]}
        candidate = make_candidate(candidate=candidate_data)
        chain = make_chain(scope_digests=[DIGEST_A])

        tool_input["path"] = "changed"
        tool_input["flags"].append("two")
        candidate_data["record_id"] = "changed"
        candidate_data["values"].append("two")

        self.assertEqual(event.tool_input["path"], "D:/repo/file.py")
        self.assertEqual(event.tool_input["flags"], ("one",))
        self.assertEqual(candidate.candidate["record_id"], "record-2")
        self.assertEqual(candidate.candidate["values"], ("one",))
        self.assertEqual(chain.scope_digests, (DIGEST_A,))
        with self.assertRaises(FrozenInstanceError):
            chain.status = "complete"  # type: ignore[misc]

    def test_normalized_event_state_serialization_excludes_transient_content(
        self,
    ) -> None:
        event = make_event(
            EventName.PRE_TOOL_USE,
            latest_assistant_message="private assistant content",
            current_user_message="private user content",
            tool_name="write",
            tool_input={"content": "private tool content"},
            tool_capability="mutation-capable",
        )

        serialized = event.to_state_dict()

        self.assertNotIn("latest_assistant_message", serialized)
        self.assertNotIn("current_user_message", serialized)
        self.assertNotIn("tool_input", serialized)
        self.assertEqual(serialized["tool_name"], "write")

    def test_models_reject_invalid_enums_ids_revisions_and_digests(self) -> None:
        invalid_factories = (
            lambda: make_event(EventName.STOP, event="stop"),
            lambda: make_session(mode="tracked"),
            lambda: make_session(session_key="bad key"),
            lambda: make_session(targeted_revision=-1),
            lambda: make_session(targeted_revision=True),
            lambda: make_chain(status="paused"),
            lambda: make_chain(scope_digests=("A" * 64,)),
            lambda: make_candidate(digest="short"),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_models_reject_unsafe_or_oversized_fields(self) -> None:
        invalid_factories = (
            lambda: make_event(EventName.STOP, host=" codex"),
            lambda: make_event(EventName.STOP, repository_root="bad\x00path"),
            lambda: make_event(
                EventName.PRE_TOOL_USE,
                tool_name="write",
                tool_input={"content": "x" * 5000},
                tool_capability="mutation-capable",
            ),
            lambda: LifecycleIssue("AHK-STOP-WORK", "x" * 500, "correct"),
            lambda: LifecycleDecision(DecisionKind.BLOCK, (), "x" * 2000),
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_decision_and_mutation_validate_tuple_and_replacement_models(self) -> None:
        issue = LifecycleIssue("AHK-STOP-WORK", "Work remains", "Continue it")
        session = make_session()
        chain = make_chain()
        mutation = LifecycleMutation(session=session, chain=chain)
        decision = LifecycleDecision(
            kind=DecisionKind.ALLOW,
            issues=[issue],
            reason="Allowed",
            mutation=mutation,
        )

        self.assertEqual(decision.issues, (issue,))
        self.assertIs(decision.mutation.session, session)
        with self.assertRaises(ValueError):
            LifecycleMutation(session="not-state")  # type: ignore[arg-type]

    def test_snapshot_requires_matching_model_types(self) -> None:
        snapshot = LifecycleSnapshot(chain=make_chain(), session=make_session())
        self.assertEqual(snapshot.chain.authorization_id, "auth-1")
        with self.assertRaises(ValueError):
            LifecycleSnapshot(chain={}, session=make_session())  # type: ignore[arg-type]

    def test_decision_request_validates_category_action_field_and_hmacs(self) -> None:
        request = DecisionRequest(
            request_id="decision-1",
            authorization_id="auth-1",
            category=AuthorityCategory.MISSING_INPUT,
            blocked_action_field="target",
            blocked_action_value=["production", "staging"],
            question_hmac=DIGEST_A,
            reason_hmac=DIGEST_B,
        )
        self.assertEqual(request.blocked_action_value, ("production", "staging"))

        for overrides in (
            {"category": "missing-input"},
            {"blocked_action_field": "scope"},
            {"question_hmac": "A" * 64},
            {"request_id": "bad id"},
            {"blocked_action_value": []},
        ):
            values = {
                "request_id": "decision-1",
                "authorization_id": "auth-1",
                "category": AuthorityCategory.MISSING_INPUT,
                "blocked_action_field": "target",
                "blocked_action_value": "production",
                "question_hmac": DIGEST_A,
                "reason_hmac": DIGEST_B,
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                DecisionRequest(**values)  # type: ignore[arg-type]


class DecisionResponseTests(unittest.TestCase):
    def test_renders_and_verifies_only_the_exact_canonical_response(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)

        response = render_decision_response(question, reason, request, SECRET)

        self.assertEqual(
            response,
            "Authorized work is paused for one required user decision.\n\n"
            "Decision needed: Which deployment target should I use?\n"
            "Blocked action field: target\n"
            "Reason: The authorized action names two mutually exclusive targets.",
        )
        self.assertTrue(verify_decision_response(response, request, SECRET))
        for altered in (
            "Preamble\n" + response,
            response + "\nSuffix",
            response.replace("deployment target", "environment"),
            response.replace("Blocked action field: target", "Target: production"),
        ):
            with self.subTest(altered=altered):
                self.assertFalse(verify_decision_response(altered, request, SECRET))

    def test_request_persists_hmacs_without_plaintext_question_or_reason(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)

        field_names = {field.name for field in fields(request)}
        persisted_text = repr(request)

        self.assertNotIn("question", field_names)
        self.assertNotIn("reason", field_names)
        self.assertNotIn(question, persisted_text)
        self.assertNotIn(reason, persisted_text)

    def test_rejects_continue_stop_and_handoff_only_questions(self) -> None:
        reason = "No authorized decision is actually required."
        for question in (
            "Should I continue?",
            "Do you want me to stop?",
            "Should I create a handoff?",
            "Continue, stop, or create a handoff?",
            "Would you like me to keep working?",
            "Should I proceed with the work?",
            "Should I write a handoff for issue 1?",
            "Should I carry on?",
            "Shall I pause?",
        ):
            with self.subTest(question=question), self.assertRaises(ValueError):
                render_decision_response(
                    question, reason, make_request(question, reason), SECRET
                )

    def test_allows_a_real_object_action_that_uses_stop_as_its_verb(self) -> None:
        question = "May I stop the production database before replacing its storage?"
        reason = "The database shutdown is an external effect requiring approval."
        request = make_request(
            question,
            reason,
            category=AuthorityCategory.EXTERNAL_EFFECT,
            blocked_action_field="action",
            blocked_action_value="Stop the production database.",
        )

        response = render_decision_response(question, reason, request, SECRET)

        self.assertTrue(verify_decision_response(response, request, SECRET))

    def test_rejects_prefix_free_single_process_action_question(self) -> None:
        question = "Continue?"
        reason = "No authorized decision is actually required."

        with self.assertRaises(ValueError):
            render_decision_response(
                question, reason, make_request(question, reason), SECRET
            )

    def test_rejects_prefixed_process_action_menu(self) -> None:
        question = "Should I continue or stop?"
        reason = "No authorized decision is actually required."

        with self.assertRaises(ValueError):
            render_decision_response(
                question, reason, make_request(question, reason), SECRET
            )

    def test_rejects_mismatched_hmac_unsafe_empty_and_oversized_text(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        invalid_calls = (
            lambda: render_decision_response(
                question,
                reason,
                make_request(question, reason, question_hmac=DIGEST_A),
                SECRET,
            ),
            lambda: render_decision_response(
                "", reason, make_request(question, reason), SECRET
            ),
            lambda: render_decision_response(
                question + "\nInjected", reason, make_request(question, reason), SECRET
            ),
            lambda: render_decision_response(
                "é" * 201 + "?", reason, make_request(question, reason), SECRET
            ),
            lambda: render_decision_response(
                question, " reason", make_request(question, reason), SECRET
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_verification_rejects_wrong_secret_and_accepts_normalized_line_endings(
        self,
    ) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)
        response = render_decision_response(question, reason, request, SECRET)

        self.assertFalse(verify_decision_response(response, request, b"wrong-secret"))
        self.assertTrue(
            verify_decision_response(response.replace("\n", "\r\n"), request, SECRET)
        )


class EventTransitionTests(unittest.TestCase):
    def test_external_user_reenters_completed_session_as_fresh_untracked(self):
        snapshot = LifecycleSnapshot(
            make_chain(status="complete"), make_session(mode=EnforcementMode.COMPLETE)
        )
        event = make_event(
            EventName.USER_PROMPT_SUBMIT,
            external_user_turn=True,
            current_user_reference="fresh-user",
        )
        result = evaluate_user_prompt(event, snapshot)
        self.assertEqual(result.mutation.session.mode, EnforcementMode.UNTRACKED)
        self.assertIsNone(result.mutation.session.authorization_id)
        self.assertIsNone(result.mutation.session.chain_revision)
        self.assertIsNone(result.mutation.chain)
        self.assertEqual(
            result.mutation.session.current_external_user_turn_reference, "fresh-user"
        )

    def test_untracked_intrinsic_read_only_and_tool_free_events_allow(self) -> None:
        snapshot = LifecycleSnapshot(
            chain=None,
            session=make_session(
                mode=EnforcementMode.UNTRACKED,
                authorization_id=None,
                chain_revision=None,
            ),
        )
        for capability in ("intrinsic-read-only", "tool-free"):
            event = make_event(
                EventName.PRE_TOOL_USE,
                tool_name="read" if capability == "intrinsic-read-only" else None,
                tool_input={} if capability == "intrinsic-read-only" else None,
                tool_capability=capability,
            )
            with self.subTest(capability=capability):
                decision = evaluate_pre_tool(event, snapshot, bootstrap_allowed=False)
                self.assertEqual(decision.kind, DecisionKind.ALLOW)
                self.assertEqual(decision.issues, ())

    def test_untracked_mutation_and_unknown_tools_block_without_bootstrap(self) -> None:
        snapshot = LifecycleSnapshot(
            chain=None,
            session=make_session(
                mode=EnforcementMode.UNTRACKED,
                authorization_id=None,
                chain_revision=None,
            ),
        )
        for capability in ("mutation-capable", "unknown"):
            event = make_event(
                EventName.PRE_TOOL_USE,
                tool_name="write",
                tool_input={"path": "D:/repo/file.py"},
                tool_capability=capability,
            )
            with self.subTest(capability=capability):
                decision = evaluate_pre_tool(event, snapshot, bootstrap_allowed=False)
                self.assertEqual(decision.kind, DecisionKind.BLOCK)
                self.assertEqual(
                    {issue.code for issue in decision.issues},
                    {"AHK-PRETOOL-TRACKING"},
                )

    def test_validated_bootstrap_and_tracked_tools_allow(self) -> None:
        event = make_event(
            EventName.PRE_TOOL_USE,
            tool_name="shell",
            tool_input={"command": "bounded bootstrap"},
            tool_capability="mutation-capable",
        )
        untracked = LifecycleSnapshot(
            chain=None,
            session=make_session(
                mode=EnforcementMode.UNTRACKED,
                authorization_id=None,
                chain_revision=None,
            ),
        )
        tracked = LifecycleSnapshot(chain=make_chain(), session=make_session())

        self.assertEqual(
            evaluate_pre_tool(event, untracked, bootstrap_allowed=True).kind,
            DecisionKind.ALLOW,
        )
        self.assertEqual(
            evaluate_pre_tool(event, tracked, bootstrap_allowed=False).kind,
            DecisionKind.ALLOW,
        )

    def test_external_user_turn_resets_correction_cycle_and_pending_decision(
        self,
    ) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        session = make_session(
            mode=EnforcementMode.AWAITING_DECISION,
            pending_decision_reference=make_request(question, reason),
            correction_cycle_count=2,
            last_issue_signature=DIGEST_A,
        )
        snapshot = LifecycleSnapshot(chain=make_chain(), session=session)
        event = make_event(
            EventName.USER_PROMPT_SUBMIT,
            current_user_message="Use production.",
            current_user_reference="turn-3",
            external_user_turn=True,
        )

        decision = evaluate_user_prompt(event, snapshot, decision_resolved=True)

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.mutation.session.correction_cycle_count, 0)
        self.assertIsNone(decision.mutation.session.last_issue_signature)
        self.assertEqual(decision.mutation.session.mode, EnforcementMode.TRACKED)
        self.assertIsNone(decision.mutation.session.pending_decision_reference)
        self.assertEqual(
            decision.mutation.session.current_external_user_turn_reference,
            "turn-3",
        )
        self.assertEqual(decision.mutation.session.targeted_revision, 3)

    def test_external_nonanswer_resets_cycle_but_retains_pending_decision(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)
        snapshot = LifecycleSnapshot(
            chain=make_chain(),
            session=make_session(
                mode=EnforcementMode.AWAITING_DECISION,
                pending_decision_reference=request,
                correction_cycle_count=2,
                last_issue_signature=DIGEST_A,
            ),
        )
        event = make_event(
            EventName.USER_PROMPT_SUBMIT,
            current_user_message="I do not know yet.",
            current_user_reference="turn-3",
            external_user_turn=True,
        )

        decision = evaluate_user_prompt(event, snapshot)

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.mutation.session.correction_cycle_count, 0)
        self.assertIsNone(decision.mutation.session.last_issue_signature)
        self.assertEqual(
            decision.mutation.session.mode, EnforcementMode.AWAITING_DECISION
        )
        self.assertIs(decision.mutation.session.pending_decision_reference, request)

    def test_model_continuation_does_not_reset_correction_cycle(self) -> None:
        session = make_session(
            correction_cycle_count=2,
            last_issue_signature=DIGEST_A,
        )
        snapshot = LifecycleSnapshot(chain=make_chain(), session=session)
        event = make_event(
            EventName.USER_PROMPT_SUBMIT,
            current_user_message="synthetic continuation",
            current_user_reference="turn-3",
            external_user_turn=False,
        )

        decision = evaluate_user_prompt(event, snapshot)

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertIsNone(decision.mutation)
        self.assertEqual(snapshot.session.correction_cycle_count, 2)


class IssueSignatureTests(unittest.TestCase):
    def test_signature_ignores_issue_order_and_evidence_wording(self) -> None:
        first = (
            LifecycleIssue(
                "AHK-STOP-ROOT",
                "First wording",
                "Correct root",
                expected="issue-1",
                actual="local-1",
            ),
            LifecycleIssue("AHK-STOP-SCOPE", "Scope changed", "Restore scope"),
        )
        second = (
            LifecycleIssue(
                "AHK-STOP-SCOPE", "Different summary", "Different correction"
            ),
            LifecycleIssue(
                "AHK-STOP-ROOT",
                "Different wording",
                "Another correction",
                expected="issue-1",
                actual="local-2",
            ),
        )

        self.assertEqual(
            issue_signature("auth-1", 4, first),
            issue_signature("auth-1", 4, second),
        )

    def test_signature_changes_with_authorization_revision_or_code_set(self) -> None:
        issues = (LifecycleIssue("AHK-STOP-WORK", "Work", "Continue"),)
        baseline = issue_signature("auth-1", 4, issues)

        self.assertNotEqual(baseline, issue_signature("auth-2", 4, issues))
        self.assertNotEqual(baseline, issue_signature("auth-1", 5, issues))
        self.assertNotEqual(
            baseline,
            issue_signature(
                "auth-1",
                4,
                (LifecycleIssue("AHK-STOP-ROOT", "Root", "Restore"),),
            ),
        )


class StopEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = LifecycleSnapshot(chain=make_chain(), session=make_session())

    def stop_event(self, message: str | None, **overrides: object) -> NormalizedEvent:
        return make_event(
            EventName.STOP,
            latest_assistant_message=message,
            **overrides,
        )

    def test_unfinished_tracked_work_without_candidate_blocks_exact_action(
        self,
    ) -> None:
        decision = evaluate_stop(
            self.stop_event("I am done."), self.snapshot, candidate=None
        )

        self.assertEqual(decision.kind, DecisionKind.BLOCK)
        self.assertEqual({issue.code for issue in decision.issues}, {"AHK-STOP-WORK"})
        self.assertIn("existing authorized exact action", decision.reason)
        self.assertEqual(decision.mutation.session.correction_cycle_count, 1)

    def test_exact_decision_response_allows_and_preserves_awaiting_scope(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)
        session = make_session(
            mode=EnforcementMode.AWAITING_DECISION,
            pending_decision_reference=request,
            correction_cycle_count=2,
            last_issue_signature=DIGEST_A,
        )
        snapshot = LifecycleSnapshot(chain=make_chain(), session=session)
        response = render_decision_response(question, reason, request, SECRET)

        decision = evaluate_stop(
            self.stop_event(response),
            snapshot,
            candidate=None,
            decision_secret=SECRET,
        )

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(
            decision.mutation.session.mode, EnforcementMode.AWAITING_DECISION
        )
        self.assertIs(decision.mutation.session.pending_decision_reference, request)
        self.assertEqual(decision.mutation.session.authorization_id, "auth-1")
        self.assertEqual(decision.mutation.session.correction_cycle_count, 0)
        self.assertEqual(decision.mutation.chain, make_chain())

    def test_missing_or_noncanonical_decision_response_blocks(self) -> None:
        question = "Which deployment target should I use?"
        reason = "The authorized action names two mutually exclusive targets."
        request = make_request(question, reason)
        snapshot = LifecycleSnapshot(
            chain=make_chain(),
            session=make_session(
                mode=EnforcementMode.AWAITING_DECISION,
                pending_decision_reference=request,
            ),
        )
        canonical = render_decision_response(question, reason, request, SECRET)

        for message in (None, "Preamble\n" + canonical, canonical + "\nSuffix"):
            with self.subTest(message=message):
                decision = evaluate_stop(
                    self.stop_event(message),
                    snapshot,
                    candidate=None,
                    decision_secret=SECRET,
                )
                self.assertEqual(decision.kind, DecisionKind.BLOCK)
                self.assertEqual(
                    {issue.code for issue in decision.issues},
                    {"AHK-STOP-DECISION"},
                )

    def test_exact_continuation_allows_and_advances_current_record(self) -> None:
        candidate = make_terminal_candidate("continuation")

        decision = evaluate_stop(
            self.stop_event(candidate.rendered_response),
            self.snapshot,
            candidate=candidate,
        )

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(
            decision.mutation.chain.current_record_reference.record_id, "record-2"
        )
        self.assertEqual(decision.mutation.chain.targeted_revision, 5)
        self.assertEqual(decision.mutation.chain.status, "active")
        self.assertEqual(decision.mutation.session.chain_revision, 5)
        self.assertEqual(decision.mutation.session.targeted_revision, 3)
        self.assertEqual(decision.mutation.session.correction_cycle_count, 0)

    def test_exact_locked_root_audit_allows_and_completes_chain(self) -> None:
        candidate = make_terminal_candidate("completion-audit")

        decision = evaluate_stop(
            self.stop_event(candidate.rendered_response),
            self.snapshot,
            candidate=candidate,
        )

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.mutation.chain.status, "complete")
        self.assertEqual(decision.mutation.session.mode, EnforcementMode.COMPLETE)
        self.assertEqual(decision.mutation.chain.locked_root_id, "issue-1")

    def test_candidate_mapping_must_match_authoritative_record_text(self) -> None:
        continuation = make_terminal_candidate("continuation")
        audit = make_terminal_candidate("completion-audit")
        mixed = make_terminal_candidate_with(
            continuation,
            candidate=audit.candidate,
        )

        decision = evaluate_stop(
            self.stop_event(mixed.rendered_response),
            self.snapshot,
            candidate=mixed,
        )

        self.assertEqual(decision.kind, DecisionKind.BLOCK)
        self.assertEqual(
            {issue.code for issue in decision.issues}, {"AHK-STOP-RESPONSE"}
        )
        self.assertNotEqual(decision.mutation.session.mode, EnforcementMode.COMPLETE)

    def test_predecessor_authorization_must_match_the_live_chain(self) -> None:
        valid = make_terminal_candidate("continuation")
        predecessor = mutable_json(valid.predecessor)
        candidate_data = mutable_json(valid.candidate)
        assert isinstance(predecessor, dict)
        assert isinstance(candidate_data, dict)
        predecessor["authorization_id"] = "auth-other"
        predecessor_text = render_record(predecessor)
        predecessor_digest = record_digest(predecessor_text)
        candidate_data["authorization_id"] = "auth-other"
        candidate_data["predecessor"]["sha256"] = predecessor_digest
        candidate_text = render_record(candidate_data)
        candidate = TerminalCandidate(
            candidate=candidate_data,
            text=candidate_text,
            path=valid.path,
            digest=record_digest(candidate_text),
            rendered_response=render_terminal_response(valid.path, candidate_text),
            predecessor=predecessor,
            predecessor_path=valid.predecessor_path,
            predecessor_source_digest=predecessor_digest,
            expected_chain_revision=valid.expected_chain_revision,
            expected_session_revision=valid.expected_session_revision,
        )

        decision = evaluate_stop(
            self.stop_event(candidate.rendered_response),
            self.snapshot,
            candidate=candidate,
        )

        self.assertEqual(decision.kind, DecisionKind.BLOCK)
        self.assertIn("AHK-STOP-PREDECESSOR", {issue.code for issue in decision.issues})

    def test_handwritten_or_contradictory_preamble_before_valid_response_blocks(
        self,
    ) -> None:
        candidate = make_terminal_candidate("continuation")
        message = "Everything is complete.\n\n" + candidate.rendered_response

        decision = evaluate_stop(
            self.stop_event(message), self.snapshot, candidate=candidate
        )

        self.assertEqual(decision.kind, DecisionKind.BLOCK)
        self.assertEqual(
            {issue.code for issue in decision.issues}, {"AHK-STOP-RESPONSE"}
        )

    def test_invented_narrower_root_maps_successor_issue_to_stop_root(self) -> None:
        valid = make_terminal_candidate("completion-audit")
        candidate_data = mutable_json(valid.candidate)
        assert isinstance(candidate_data, dict)
        candidate_data["authorized_root_scope_id"] = "local-task"
        candidate_data["completed_scope_id"] = "local-task"
        candidate_data["active_scopes"] = [
            make_scope(
                "local-task",
                remaining_work=False,
                remaining_code=False,
                status="complete",
            )
        ]
        candidate_text = render_record(candidate_data)
        candidate = TerminalCandidate(
            candidate=candidate_data,
            text=candidate_text,
            path=CANDIDATE_PATH,
            digest=record_digest(candidate_text),
            rendered_response=render_terminal_response(CANDIDATE_PATH, candidate_text),
            predecessor=valid.predecessor,
            predecessor_path=valid.predecessor_path,
            predecessor_source_digest=valid.predecessor_source_digest,
            expected_chain_revision=4,
            expected_session_revision=2,
        )

        decision = evaluate_stop(
            self.stop_event(candidate.rendered_response),
            self.snapshot,
            candidate=candidate,
        )

        self.assertEqual(decision.kind, DecisionKind.BLOCK)
        self.assertIn("AHK-STOP-ROOT", {issue.code for issue in decision.issues})

    def test_predecessor_and_scope_issues_map_to_stable_stop_codes(self) -> None:
        valid = make_terminal_candidate("continuation")

        wrong_predecessor = make_terminal_candidate("continuation")
        wrong_predecessor_data = mutable_json(wrong_predecessor.candidate)
        assert isinstance(wrong_predecessor_data, dict)
        wrong_predecessor_data["predecessor"]["record_id"] = "record-other"
        wrong_predecessor = make_candidate_from_valid(
            wrong_predecessor, wrong_predecessor_data
        )

        changed_scope = make_terminal_candidate("continuation")
        changed_scope_data = mutable_json(changed_scope.candidate)
        assert isinstance(changed_scope_data, dict)
        changed_scope_data["active_scopes"][0]["scope_definition"]["outcome"] = (
            "Invented replacement outcome."
        )
        changed_scope_data["active_scopes"][0]["scope_definition_digest"] = (
            scope_definition_digest(changed_scope_data["active_scopes"][0])
        )
        changed_scope = make_candidate_from_valid(changed_scope, changed_scope_data)

        for candidate, expected_code in (
            (wrong_predecessor, "AHK-STOP-PREDECESSOR"),
            (changed_scope, "AHK-STOP-SCOPE"),
        ):
            with self.subTest(expected_code=expected_code):
                decision = evaluate_stop(
                    self.stop_event(candidate.rendered_response),
                    self.snapshot,
                    candidate=candidate,
                )
                self.assertIn(expected_code, {i.code for i in decision.issues})

        self.assertEqual(valid.predecessor["record_id"], "record-1")

    def test_snapshot_or_candidate_revision_mismatch_blocks_as_stale(self) -> None:
        candidate = make_terminal_candidate("continuation")
        stale_snapshots = (
            LifecycleSnapshot(
                chain=make_chain(targeted_revision=5), session=make_session()
            ),
            LifecycleSnapshot(
                chain=make_chain(), session=make_session(chain_revision=3)
            ),
        )
        stale_candidates = (
            make_terminal_candidate_with(candidate, expected_chain_revision=3),
            make_terminal_candidate_with(candidate, expected_session_revision=1),
        )

        for snapshot in stale_snapshots:
            with self.subTest(snapshot=snapshot):
                decision = evaluate_stop(
                    self.stop_event(candidate.rendered_response),
                    snapshot,
                    candidate=candidate,
                )
                self.assertEqual(
                    {issue.code for issue in decision.issues}, {"AHK-STOP-STALE"}
                )
        for stale_candidate in stale_candidates:
            with self.subTest(candidate=stale_candidate):
                decision = evaluate_stop(
                    self.stop_event(stale_candidate.rendered_response),
                    self.snapshot,
                    candidate=stale_candidate,
                )
                self.assertEqual(
                    {issue.code for issue in decision.issues}, {"AHK-STOP-STALE"}
                )

    def test_cosmetic_message_and_candidate_changes_never_reset_cycle_count(
        self,
    ) -> None:
        first = evaluate_stop(
            self.stop_event("first cosmetic message"), self.snapshot, candidate=None
        )
        second_snapshot = LifecycleSnapshot(
            chain=self.snapshot.chain, session=first.mutation.session
        )
        candidate = make_terminal_candidate_with(
            make_terminal_candidate("continuation"),
            rendered_response="different candidate evidence",
        )
        second = evaluate_stop(
            self.stop_event("second cosmetic message"),
            second_snapshot,
            candidate=candidate,
        )

        self.assertEqual(first.mutation.session.correction_cycle_count, 1)
        self.assertEqual(second.mutation.session.correction_cycle_count, 2)

    def test_third_block_circuit_breaks_even_when_stop_hook_is_active(self) -> None:
        snapshot = self.snapshot
        decisions = []
        for index in range(3):
            decision = evaluate_stop(
                self.stop_event(f"attempt {index}", stop_hook_active=index > 0),
                snapshot,
                candidate=None,
            )
            decisions.append(decision)
            snapshot = LifecycleSnapshot(
                chain=snapshot.chain, session=decision.mutation.session
            )

        self.assertEqual(decisions[0].kind, DecisionKind.BLOCK)
        self.assertEqual(decisions[1].kind, DecisionKind.BLOCK)
        self.assertEqual(decisions[2].kind, DecisionKind.POLICY_FAILURE)
        self.assertIn("AHK-STOP-CIRCUIT", {issue.code for issue in decisions[2].issues})
        self.assertNotIn("complete", decisions[2].reason.casefold())

    def test_corrected_stop_before_cap_passes_and_resets_cycle(self) -> None:
        candidate = make_terminal_candidate("continuation")
        snapshot = LifecycleSnapshot(
            chain=make_chain(),
            session=make_session(
                correction_cycle_count=2,
                last_issue_signature=DIGEST_A,
            ),
        )

        decision = evaluate_stop(
            self.stop_event(candidate.rendered_response, stop_hook_active=True),
            snapshot,
            candidate=candidate,
        )

        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.mutation.session.correction_cycle_count, 0)
        self.assertIsNone(decision.mutation.session.last_issue_signature)


if __name__ == "__main__":
    unittest.main()
