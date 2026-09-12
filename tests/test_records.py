from __future__ import annotations

import copy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit import (  # noqa: E402
    parse_markdown,
    render_record,
    render_tail,
    render_terminal_response,
    validate_successor,
    validate_markdown,
)
from agent_handoff_toolkit.lineage import (  # noqa: E402
    record_digest,
    scope_definition_digest,
)

FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def issue_codes(text: str) -> set[str]:
    return {issue.code for issue in validate_markdown(text)}


V2_PREDECESSOR_PATH = "D:/repo/handoffs/record-001.md"

# Schema v2 keeps no narrative copy of a metadata field, so an author supplies
# only the sections that have no metadata equivalent.
V2_DROPPED_SECTIONS = (
    "Verification evidence",
    "Exact next action",
    "Remaining code by active scope",
    "Next-session prompt",
)


def drop_v2_sections(data: dict[str, object]) -> dict[str, object]:
    sections = data["sections"]
    assert isinstance(sections, dict)
    for name in V2_DROPPED_SECTIONS:
        sections.pop(name, None)
    return data


def make_v2_scope(
    scope_id: str,
    *,
    scope_kind: str,
    parent_scope_id: str | None,
    highest_authorized: bool,
    remaining_work: bool,
    remaining_code: bool,
    status: str,
) -> dict[str, object]:
    scope: dict[str, object] = {
        "scope_id": scope_id,
        "scope_kind": scope_kind,
        "parent_scope_id": parent_scope_id,
        "highest_authorized": highest_authorized,
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


def make_v2_continuation(
    *,
    record_id: str = "record-002",
    authorization_id: str = "auth-001",
    root: str = "issue-1323",
    children: tuple[str, ...] = (),
    predecessor: object = None,
) -> dict[str, object]:
    data = load_fixture("continuation.json")
    data.update(
        {
            "schema_version": 2,
            "record_id": record_id,
            "authorization_id": authorization_id,
            "authorized_root_scope_id": root,
            "predecessor": predecessor,
            "authorization_evidence": {
                "kind": "initial-user-turn",
                "user_turn_ref": "turn-user-001",
                "proposal_turn_ref": None,
                "evidence_hmac": "1" * 64,
            },
            "transition": None,
        }
    )
    scopes = [
        make_v2_scope(
            root,
            scope_kind="issue",
            parent_scope_id=None,
            highest_authorized=True,
            remaining_work=True,
            remaining_code=True,
            status="in-progress",
        )
    ]
    for child in children:
        scopes.append(
            make_v2_scope(
                child,
                scope_kind="unit",
                parent_scope_id=root,
                highest_authorized=False,
                remaining_work=True,
                remaining_code=True,
                status="pending",
            )
        )
    data["active_scopes"] = scopes
    return drop_v2_sections(data)


def make_v2_audit(
    *,
    record_id: str = "record-audit-001",
    authorization_id: str = "auth-001",
    root: str = "issue-1323",
    children: tuple[str, ...] = (),
    predecessor: object = None,
) -> dict[str, object]:
    data = load_fixture("completion-audit.json")
    data.update(
        {
            "schema_version": 2,
            "record_id": record_id,
            "authorization_id": authorization_id,
            "authorized_root_scope_id": root,
            "predecessor": predecessor,
            "authorization_evidence": {
                "kind": "initial-user-turn",
                "user_turn_ref": "turn-user-001",
                "proposal_turn_ref": None,
                "evidence_hmac": "1" * 64,
            },
            "transition": None,
            "completed_scope_id": root,
        }
    )
    scopes = [
        make_v2_scope(
            root,
            scope_kind="issue",
            parent_scope_id=None,
            highest_authorized=True,
            remaining_work=False,
            remaining_code=False,
            status="complete",
        )
    ]
    for child in children:
        scopes.append(
            make_v2_scope(
                child,
                scope_kind="unit",
                parent_scope_id=root,
                highest_authorized=False,
                remaining_work=False,
                remaining_code=False,
                status="complete",
            )
        )
    data["active_scopes"] = scopes
    return drop_v2_sections(data)


def successor_of(
    predecessor: dict[str, object],
    *,
    record_type: str = "continuation",
    predecessor_path: str = V2_PREDECESSOR_PATH,
) -> dict[str, object]:
    root = str(predecessor["authorized_root_scope_id"])
    children = tuple(
        str(scope["scope_id"])
        for scope in predecessor["active_scopes"]
        if isinstance(scope, dict) and scope["scope_id"] != root
    )
    reference = {
        "record_id": predecessor["record_id"],
        "path": predecessor_path,
        "sha256": record_digest(render_record(predecessor)),
    }
    if record_type == "completion-audit":
        return make_v2_audit(
            authorization_id=str(predecessor["authorization_id"]),
            root=root,
            children=children,
            predecessor=reference,
        )
    return make_v2_continuation(
        record_id="record-003",
        authorization_id=str(predecessor["authorization_id"]),
        root=root,
        children=children,
        predecessor=reference,
    )


class RecordValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.continuation = load_fixture("continuation.json")
        self.audit = load_fixture("completion-audit.json")

    def test_valid_record_dispatch(self) -> None:
        self.assertEqual(validate_markdown(render_record(self.continuation)), [])
        self.assertEqual(validate_markdown(render_record(self.audit)), [])

    def test_well_formed_v2_continuation_and_audit_validate(self) -> None:
        for data in (make_v2_continuation(), make_v2_audit()):
            with self.subTest(record_type=data["record_type"]):
                rendered = render_record(data)
                self.assertEqual(validate_markdown(rendered), [])
                self.assertEqual(parse_markdown(rendered)["schema_version"], 2)

    def test_v2_record_interfaces_are_exported(self) -> None:
        import agent_handoff_toolkit

        self.assertTrue(callable(getattr(agent_handoff_toolkit, "validate_successor")))
        self.assertTrue(
            callable(getattr(agent_handoff_toolkit, "render_terminal_response"))
        )

    def test_v2_requires_exact_record_lineage_fields(self) -> None:
        for mutation in ("missing", "unknown"):
            data = make_v2_continuation()
            if mutation == "missing":
                del data["record_id"]
            else:
                data["lineage_extra"] = "unexpected"
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "lineage-field"),
            ):
                render_record(data)

    def test_v2_rejects_invalid_lineage_identifiers_and_root_links(self) -> None:
        data = make_v2_continuation()
        data["record_id"] = "bad id"
        with self.assertRaisesRegex(ValueError, "lineage-id"):
            render_record(data)

        data = make_v2_continuation()
        data["authorized_root_scope_id"] = "invented-root"
        with self.assertRaisesRegex(ValueError, "lineage-root"):
            render_record(data)

    def test_v2_requires_exact_scope_shape_and_matching_definition_digest(
        self,
    ) -> None:
        data = make_v2_continuation()
        data["active_scopes"][0]["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "lineage-scope"):
            render_record(data)

        data = make_v2_continuation()
        data["active_scopes"][0]["scope_definition_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "lineage-definition"):
            render_record(data)

    def test_v2_rejects_malformed_predecessor_evidence_and_transition(self) -> None:
        malformed_cases = []

        data = make_v2_continuation()
        data["predecessor"] = {
            "record_id": "record-001",
            "path": "relative/record.md",
            "sha256": "0" * 64,
        }
        malformed_cases.append((data, "lineage-predecessor"))

        data = make_v2_continuation()
        data["predecessor"] = {
            "record_id": "record-001",
            "path": "D:/repo/handoffs/../record.md",
            "sha256": "0" * 64,
        }
        malformed_cases.append((data, "lineage-predecessor"))

        data = make_v2_continuation()
        del data["authorization_evidence"]["user_turn_ref"]
        malformed_cases.append((data, "lineage-evidence"))

        data = make_v2_continuation()
        data["transition"] = {"evidence_hmac": "2" * 64}
        malformed_cases.append((data, "lineage-transition"))

        data = make_v2_continuation()
        data["authorization_evidence"] = {
            "kind": "approved-transition",
            "user_turn_ref": "turn-user-002",
            "proposal_turn_ref": "turn-proposal-002",
            "evidence_hmac": "2" * 64,
        }
        data["transition"] = {
            "from_authorization_id": "auth-001",
            "to_authorization_id": "auth-002",
            "old_root_scope_id": "issue-1000",
            "new_root_scope_id": "issue-1323",
            "proposal_turn_ref": "turn-proposal-002",
            "approval_turn_ref": "turn-user-002",
            "evidence_hmac": "2" * 64,
        }
        malformed_cases.append((data, "lineage-transition"))

        for data, issue_code in malformed_cases:
            with (
                self.subTest(issue_code=issue_code),
                self.assertRaisesRegex(ValueError, issue_code),
            ):
                render_record(data)

    def test_v2_rejects_inconsistent_evidence_kinds(self) -> None:
        data = make_v2_continuation()
        data["authorization_evidence"]["proposal_turn_ref"] = "turn-proposal-001"
        with self.assertRaisesRegex(ValueError, "lineage-evidence"):
            render_record(data)

        data = make_v2_continuation(
            predecessor={
                "record_id": "record-001",
                "path": V2_PREDECESSOR_PATH,
                "sha256": "0" * 64,
            }
        )
        data["authorization_evidence"].update(
            {
                "kind": "v1-adoption",
                "proposal_turn_ref": "turn-proposal-001",
            }
        )
        with self.assertRaisesRegex(ValueError, "lineage-evidence"):
            render_record(data)

        data = make_v2_continuation()
        data["authorization_evidence"].update(
            {
                "kind": "approved-transition",
                "proposal_turn_ref": "turn-proposal-001",
            }
        )
        with self.assertRaisesRegex(ValueError, "lineage-evidence"):
            render_record(data)

    def test_rejects_unknown_record_type(self) -> None:
        self.continuation["record_type"] = "summary"
        with self.assertRaisesRegex(ValueError, "record-type"):
            render_record(self.continuation)

    def test_schema_version_arrays_and_objects_report_validation_issues(self) -> None:
        for value in ([], {}):
            for data in (copy.deepcopy(self.continuation), make_v2_continuation()):
                data["schema_version"] = value
                with (
                    self.subTest(record_type=data["record_type"], value=value),
                    self.assertRaisesRegex(ValueError, "schema-version"),
                ):
                    render_record(data)

    def test_evidence_kind_arrays_and_objects_report_validation_issues(self) -> None:
        for value in ([], {}):
            data = make_v2_continuation()
            data["authorization_evidence"]["kind"] = value
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "lineage-evidence"),
            ):
                render_record(data)

    def test_requires_exact_ordered_sections(self) -> None:
        text = render_record(self.continuation)
        text = text.replace(
            "## Objective\n",
            "## Extra\nUnexpected.\n\n## Objective\n",
            1,
        )
        self.assertIn("section-shape", issue_codes(text))

        text = render_record(self.continuation)
        text = text.replace("## Objective", "## TEMP", 1)
        text = text.replace("## User decisions", "## Objective", 1)
        text = text.replace("## TEMP", "## User decisions", 1)
        self.assertIn("section-shape", issue_codes(text))

    def test_requires_one_rooted_acyclic_scope_chain(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["parent_scope_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "scope-parent"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][0]["parent_scope_id"] = "core-cli"
        with self.assertRaisesRegex(ValueError, "scope-root"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["parent_scope_id"] = "core-cli"
        with self.assertRaisesRegex(ValueError, "scope-cycle"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["highest_authorized"] = True
        with self.assertRaisesRegex(ValueError, "scope-highest"):
            render_record(data)

    def test_highest_scope_remaining_work_matches_record_type(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["active_scopes"][0]["remaining_work"] = False
        with self.assertRaisesRegex(ValueError, "scope-state"):
            render_record(data)

    def test_remaining_code_implies_work_and_descendants_propagate(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["remaining_work"] = False
        with self.assertRaisesRegex(ValueError, "scope-consistency"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][0]["remaining_code"] = False
        with self.assertRaisesRegex(ValueError, "scope-propagation"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["remaining_work"] = False
        data["active_scopes"][1]["remaining_code"] = False
        data["active_scopes"][1]["status"] = "complete"
        data["active_scopes"].append(
            {
                "scope_id": "nested-unit",
                "scope_kind": "unit",
                "parent_scope_id": "core-cli",
                "highest_authorized": False,
                "remaining_work": True,
                "remaining_code": False,
                "remaining_code_detail": "Review work remains, but no code changes are known.",
                "status": "in-progress",
            }
        )
        with self.assertRaisesRegex(ValueError, "scope-propagation"):
            render_record(data)

        data = copy.deepcopy(self.audit)
        data["active_scopes"].append(
            {
                "scope_id": "unfinished-child",
                "scope_kind": "phase",
                "parent_scope_id": "standalone-contract",
                "highest_authorized": False,
                "remaining_work": True,
                "remaining_code": True,
                "remaining_code_detail": "Implementation remains.",
                "status": "in-progress",
            }
        )
        with self.assertRaisesRegex(ValueError, "scope-propagation"):
            render_record(data)

    def test_scope_status_is_bounded_and_matches_highest_record_state(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["active_scopes"][0]["status"] = "complete"
        with self.assertRaisesRegex(ValueError, "scope-status"):
            render_record(data)

        data = copy.deepcopy(self.audit)
        data["active_scopes"][0]["status"] = "in-progress"
        with self.assertRaisesRegex(ValueError, "scope-status"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["status"] = "almost-finished"
        with self.assertRaisesRegex(ValueError, "scope-status"):
            render_record(data)

        data = copy.deepcopy(self.audit)
        data["active_scopes"][0]["remaining_work"] = True
        with self.assertRaisesRegex(ValueError, "scope-state"):
            render_record(data)

    def test_scope_kind_and_remaining_code_detail_are_validated(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["scope_kind"] = "task"
        with self.assertRaisesRegex(ValueError, "scope-kind"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["active_scopes"][1]["remaining_code_detail"] = ""
        with self.assertRaisesRegex(ValueError, "scope-field"):
            render_record(data)

    def test_verification_result_and_supporting_text_are_validated(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["verification"][0]["result"] = "skipped"
        with self.assertRaisesRegex(ValueError, "verification-result"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        del data["verification"][0]["evidence"]
        with self.assertRaisesRegex(ValueError, "verification-evidence"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        del data["verification"][1]["reason"]
        with self.assertRaisesRegex(ValueError, "verification-reason"):
            render_record(data)

    def test_continuation_requires_empty_gates_and_actionable_fields(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["next_session_gates"] = ["Which branch?"]
        with self.assertRaisesRegex(ValueError, "question-gate"):
            render_record(data)

        for field in ("action", "target", "constraints", "completion_condition"):
            data = copy.deepcopy(self.continuation)
            data["exact_action"][field] = ""
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "exact-action"),
            ):
                render_record(data)

    def test_continuation_and_audit_fields_are_mutually_exclusive(self) -> None:
        data = copy.deepcopy(self.audit)
        data["next_session_prompt"] = "Restart from here."
        with self.assertRaisesRegex(ValueError, "field-exclusion"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["authorization_basis"] = "Completed."
        with self.assertRaisesRegex(ValueError, "field-exclusion"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["completed_scope_id"] = "core-cli"
        with self.assertRaisesRegex(ValueError, "field-exclusion"):
            render_record(data)

    def test_audit_completed_scope_matches_highest_authorized_scope(self) -> None:
        data = copy.deepcopy(self.audit)
        data["completed_scope_id"] = "another-scope"
        with self.assertRaisesRegex(ValueError, "completed-scope"):
            render_record(data)

    def test_prompt_requires_canonical_whitespace_and_newlines(self) -> None:
        for prompt in (
            " leading space",
            "trailing space ",
            "leading newline\n",
            "line one\r\nline two",
            "line one\u2028line two",
            "line one\x00line two",
        ):
            data = copy.deepcopy(self.continuation)
            data["next_session_prompt"] = prompt
            with (
                self.subTest(prompt=repr(prompt)),
                self.assertRaisesRegex(ValueError, "next-prompt-format"),
            ):
                render_record(data)

    def test_prompt_rejects_oversized_or_full_record_content(self) -> None:
        oversized = " ".join(f"word{index}" for index in range(121))
        full_record = (
            "<!-- agent-handoff-metadata\n{}\n-->\n\n"
            "# Session continuation\n\n## Objective\n\nPasted record."
        )
        fenced_document = "```markdown\n## Objective\nPasted record.\n```"

        for prompt, issue_code in (
            (oversized, "next-prompt-size"),
            (full_record, "next-prompt-document"),
            (fenced_document, "next-prompt-document"),
        ):
            data = copy.deepcopy(self.continuation)
            data["next_session_prompt"] = prompt
            with (
                self.subTest(prompt=prompt[:40]),
                self.assertRaisesRegex(ValueError, issue_code),
            ):
                render_record(data)

    def test_prompt_rejects_no_action_assertions_for_continuations(self) -> None:
        for prompt in (
            "None.",
            "Nothing to do.",
            "No action required.",
            "What you need to do: None.",
            "1. None.",
            "12) No action required.",
            "+ Nothing remains.",
            "- Decision: preserve current branch.\nWhat you need to do: None.",
        ):
            data = copy.deepcopy(self.continuation)
            data["next_session_prompt"] = prompt
            with (
                self.subTest(prompt=prompt),
                self.assertRaisesRegex(ValueError, "next-prompt-action"),
            ):
                render_record(data)

        data = copy.deepcopy(self.continuation)
        data["next_session_prompt"] = "1. None of the focused tests may fail."
        render_record(data)

    def test_prompt_rejects_more_than_six_nonempty_lines(self) -> None:
        data = copy.deepcopy(self.continuation)
        data["next_session_prompt"] = "\n".join(
            f"- Gate {index}." for index in range(1, 8)
        )

        with self.assertRaisesRegex(ValueError, "next-prompt-lines"):
            render_record(data)

    def test_prompt_limits_accept_the_boundary_and_reject_the_next_unit(self) -> None:
        for accepted, rejected, issue_code in (
            (" ".join(["word"] * 120), " ".join(["word"] * 121), "next-prompt-size"),
            ("x" * 1200, "x" * 1201, "next-prompt-size"),
            ("\n".join(["gate"] * 6), "\n".join(["gate"] * 7), "next-prompt-lines"),
        ):
            with self.subTest(issue=issue_code, accepted_length=len(accepted)):
                data = copy.deepcopy(self.continuation)
                data["next_session_prompt"] = accepted
                render_record(data)
                data["next_session_prompt"] = rejected
                with self.assertRaisesRegex(ValueError, issue_code):
                    render_record(data)

    def test_exact_action_rejects_document_or_multiline_injection(self) -> None:
        document = "<!-- agent-handoff-metadata -->"
        for field in ("action", "target", "constraints", "completion_condition"):
            for value in (
                document,
                "first line\n## Objective",
                "first line\u2028spoofed label",
                "first line\x00spoofed label",
                ["Safe item.", document],
            ):
                data = copy.deepcopy(self.continuation)
                data["exact_action"][field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, "exact-action-format"),
                ):
                    render_record(data)

    def test_exact_action_rejects_no_action_assertions(self) -> None:
        no_action_values = (
            "None.",
            "Nothing to do.",
            "Nothing remains.",
            "No action required.",
            "What you need to do: None.",
            "1. None.",
            "12) No action required.",
            "+ Nothing remains.",
        )
        for field in ("action", "target", "constraints", "completion_condition"):
            for value in no_action_values:
                data = copy.deepcopy(self.continuation)
                data["exact_action"][field] = value
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, "exact-action-state"),
                ):
                    render_record(data)

        data = copy.deepcopy(self.continuation)
        data["exact_action"]["constraints"] = [
            "Preserve live state.",
            "No action required.",
        ]
        with self.assertRaisesRegex(ValueError, "exact-action-state"):
            render_record(data)

        data = copy.deepcopy(self.continuation)
        data["exact_action"]["completion_condition"] = (
            "Ensure none of the focused tests fail."
        )
        render_record(data)


class SuccessorValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predecessor = make_v2_continuation(
            record_id="record-001",
            children=("task-7a", "task-7b"),
        )

    def codes(
        self,
        candidate: dict[str, object],
        *,
        predecessor: dict[str, object] | None = None,
        expected_path: str = V2_PREDECESSOR_PATH,
        predecessor_sha256: str | None = None,
        approved_transition_hmac: str | None = None,
    ) -> set[str]:
        selected_predecessor = predecessor or self.predecessor
        return {
            issue.code
            for issue in validate_successor(
                candidate,
                selected_predecessor,
                expected_predecessor_path=expected_path,
                predecessor_sha256=(
                    predecessor_sha256
                    if predecessor_sha256 is not None
                    else record_digest(render_record(selected_predecessor))
                ),
                approved_transition_hmac=approved_transition_hmac,
            )
        }

    def test_successor_requires_a_keyword_only_predecessor_digest(self) -> None:
        parameter = inspect.signature(validate_successor).parameters[
            "predecessor_sha256"
        ]

        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_successor_rejects_predecessor_identity_path_and_digest_mismatch(
        self,
    ) -> None:
        for field, value in (
            ("record_id", "record-other"),
            ("path", "D:/repo/handoffs/record-other.md"),
            ("sha256", "f" * 64),
        ):
            candidate = successor_of(self.predecessor)
            candidate["predecessor"][field] = value
            with self.subTest(field=field):
                self.assertIn("lineage-predecessor", self.codes(candidate))

    def test_successor_uses_the_supplied_source_byte_digest(self) -> None:
        canonical_text = render_record(self.predecessor)
        source_text = canonical_text.replace(
            '"authorization_id": "auth-001"',
            '"authorization_id" : "auth-001"',
            1,
        )
        source_digest = record_digest(source_text)
        self.assertNotEqual(source_digest, record_digest(canonical_text))
        parsed_predecessor = parse_markdown(source_text)
        candidate = successor_of(self.predecessor)
        candidate["predecessor"]["sha256"] = source_digest

        self.assertEqual(
            self.codes(
                candidate,
                predecessor=parsed_predecessor,
                predecessor_sha256=source_digest,
            ),
            set(),
        )

    def test_successor_validates_the_supplied_predecessor_digest(self) -> None:
        candidate = successor_of(self.predecessor)
        for invalid_digest in ("A" * 64, [], {}):
            with self.subTest(invalid_digest=invalid_digest):
                self.assertIn(
                    "lineage-predecessor",
                    self.codes(
                        candidate,
                        predecessor_sha256=invalid_digest,
                    ),
                )

    def test_successor_rejects_changed_authorization_without_transition(self) -> None:
        candidate = successor_of(self.predecessor)
        candidate["authorization_id"] = "auth-002"

        self.assertIn("lineage-authorization", self.codes(candidate))

    def test_successor_rejects_reusing_the_predecessor_record_id(self) -> None:
        candidate = successor_of(self.predecessor)
        candidate["record_id"] = self.predecessor["record_id"]

        self.assertIn("lineage-record", self.codes(candidate))

    def test_successor_rejects_dropped_reordered_or_invented_scopes(self) -> None:
        mutations = []

        candidate = successor_of(self.predecessor)
        candidate["active_scopes"].pop()
        mutations.append(("dropped", candidate))

        candidate = successor_of(self.predecessor)
        candidate["active_scopes"][1:] = reversed(candidate["active_scopes"][1:])
        mutations.append(("reordered", candidate))

        candidate = successor_of(self.predecessor)
        candidate["active_scopes"].append(
            make_v2_scope(
                "invented-task",
                scope_kind="unit",
                parent_scope_id="issue-1323",
                highest_authorized=False,
                remaining_work=True,
                remaining_code=True,
                status="pending",
            )
        )
        mutations.append(("invented", candidate))

        for mutation, candidate in mutations:
            with self.subTest(mutation=mutation):
                self.assertIn("lineage-scope", self.codes(candidate))

    def test_successor_rejects_reparented_inherited_scope(self) -> None:
        predecessor = make_v2_continuation(
            record_id="record-001",
            children=("phase-1", "task-7a"),
        )
        predecessor["active_scopes"][1]["scope_kind"] = "phase"
        predecessor["active_scopes"][1]["scope_definition_digest"] = (
            scope_definition_digest(predecessor["active_scopes"][1])
        )
        predecessor["active_scopes"][2]["parent_scope_id"] = "phase-1"
        predecessor["active_scopes"][2]["scope_definition_digest"] = (
            scope_definition_digest(predecessor["active_scopes"][2])
        )
        candidate = successor_of(predecessor)
        candidate["active_scopes"][2]["parent_scope_id"] = "issue-1323"
        candidate["active_scopes"][2]["scope_definition_digest"] = (
            scope_definition_digest(candidate["active_scopes"][2])
        )

        self.assertIn(
            "lineage-definition",
            self.codes(candidate, predecessor=predecessor),
        )

    def test_successor_rejects_immutable_scope_changes_with_recomputed_digest(
        self,
    ) -> None:
        for field in ("title", "outcome", "scope_kind"):
            candidate = successor_of(self.predecessor)
            scope = candidate["active_scopes"][1]
            if field == "scope_kind":
                scope[field] = "phase"
            else:
                scope["scope_definition"][field] = f"Changed {field}."
            scope["scope_definition_digest"] = scope_definition_digest(scope)
            with self.subTest(field=field):
                self.assertIn("lineage-definition", self.codes(candidate))

    def test_successor_permits_mutable_progress_and_verification_changes(self) -> None:
        candidate = successor_of(self.predecessor)
        root, completed_child, pending_child = candidate["active_scopes"]
        root.update(
            {
                "remaining_code": False,
                "remaining_code_detail": "No code remains; review is pending.",
                "status": "blocked",
            }
        )
        completed_child.update(
            {
                "remaining_work": False,
                "remaining_code": False,
                "remaining_code_detail": "Implementation and review are complete.",
                "status": "complete",
            }
        )
        pending_child.update(
            {
                "remaining_code": False,
                "remaining_code_detail": "No code remains; focused review is pending.",
            }
        )
        candidate["verification"] = [
            {
                "check": "focused structural check",
                "result": "pass",
                "evidence": "The structural check completed successfully.",
            }
        ]

        self.assertEqual(self.codes(candidate), set())

    def test_successor_allows_only_a_trusted_internally_consistent_transition(
        self,
    ) -> None:
        trusted_hmac = "a" * 64
        candidate = successor_of(self.predecessor)
        candidate.update(
            {
                "authorization_id": "auth-002",
                "authorized_root_scope_id": "epic-2000",
                "active_scopes": [
                    make_v2_scope(
                        "epic-2000",
                        scope_kind="epic",
                        parent_scope_id=None,
                        highest_authorized=True,
                        remaining_work=True,
                        remaining_code=True,
                        status="in-progress",
                    )
                ],
                "authorization_evidence": {
                    "kind": "approved-transition",
                    "user_turn_ref": "turn-user-002",
                    "proposal_turn_ref": "turn-proposal-002",
                    "evidence_hmac": trusted_hmac,
                },
                "transition": {
                    "from_authorization_id": "auth-001",
                    "to_authorization_id": "auth-002",
                    "old_root_scope_id": "issue-1323",
                    "new_root_scope_id": "epic-2000",
                    "proposal_turn_ref": "turn-proposal-002",
                    "approval_turn_ref": "turn-user-002",
                    "evidence_hmac": trusted_hmac,
                },
            }
        )

        self.assertEqual(
            self.codes(candidate, approved_transition_hmac=trusted_hmac),
            set(),
        )
        self.assertIn("lineage-transition", self.codes(candidate))
        self.assertIn(
            "lineage-transition",
            self.codes(candidate, approved_transition_hmac="b" * 64),
        )

        for field, value in (
            ("from_authorization_id", "auth-other"),
            ("to_authorization_id", "auth-other"),
            ("old_root_scope_id", "root-other"),
            ("new_root_scope_id", "root-other"),
        ):
            inconsistent = copy.deepcopy(candidate)
            inconsistent["transition"][field] = value
            with self.subTest(field=field):
                self.assertIn(
                    "lineage-transition",
                    self.codes(
                        inconsistent,
                        approved_transition_hmac=trusted_hmac,
                    ),
                )

    def test_trusted_transition_can_change_definition_without_changing_root_id(
        self,
    ) -> None:
        trusted_hmac = "c" * 64
        candidate = successor_of(self.predecessor)
        candidate["authorization_id"] = "auth-002"
        candidate["active_scopes"][0]["scope_definition"]["outcome"] = (
            "Complete the expanded authorized issue outcome."
        )
        candidate["active_scopes"][0]["scope_definition_digest"] = (
            scope_definition_digest(candidate["active_scopes"][0])
        )
        candidate["authorization_evidence"] = {
            "kind": "approved-transition",
            "user_turn_ref": "turn-user-003",
            "proposal_turn_ref": "turn-proposal-003",
            "evidence_hmac": trusted_hmac,
        }
        candidate["transition"] = {
            "from_authorization_id": "auth-001",
            "to_authorization_id": "auth-002",
            "old_root_scope_id": "issue-1323",
            "new_root_scope_id": "issue-1323",
            "proposal_turn_ref": "turn-proposal-003",
            "approval_turn_ref": "turn-user-003",
            "evidence_hmac": trusted_hmac,
        }

        self.assertEqual(
            self.codes(candidate, approved_transition_hmac=trusted_hmac),
            set(),
        )

    def test_successor_rejects_invented_narrower_completed_root(self) -> None:
        audit = make_v2_audit(
            root="local-task-7a",
            predecessor=successor_of(self.predecessor)["predecessor"],
        )

        self.assertIn("lineage-root", self.codes(audit))

    def test_successor_rejects_completing_a_child_of_the_locked_root(self) -> None:
        audit = successor_of(self.predecessor, record_type="completion-audit")
        audit["completed_scope_id"] = "task-7a"

        self.assertIn("lineage-root", self.codes(audit))


class RecordRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.continuation = load_fixture("continuation.json")
        self.audit = load_fixture("completion-audit.json")

    def test_render_is_deterministic_and_round_trips(self) -> None:
        first = render_record(self.continuation)
        second = render_record(copy.deepcopy(self.continuation))
        self.assertEqual(first, second)
        parsed = parse_markdown(first)
        self.assertEqual(render_record(parsed), first)
        self.assertEqual(
            {key: value for key, value in parsed.items() if key != "sections"},
            {
                key: value
                for key, value in self.continuation.items()
                if key != "sections"
            },
        )
        for section in (
            "Objective",
            "Authoritative references",
            "User decisions",
            "Repository state",
            "Completed work",
            "Incomplete work and risks",
            "External effects",
        ):
            self.assertEqual(
                parsed["sections"][section], self.continuation["sections"][section]
            )
        self.assertIn("<!-- agent-handoff-metadata", first)

    def test_renderer_derives_prompt_and_structured_sections_from_metadata(
        self,
    ) -> None:
        self.continuation["sections"]["Verification evidence"] = "Everything passed."
        self.continuation["sections"]["Exact next action"] = "Do something else."
        self.continuation["sections"]["Remaining code by active scope"] = (
            "Nothing remains."
        )
        self.continuation["sections"]["Next-session prompt"] = "A different prompt."

        text = render_record(self.continuation)
        parsed = parse_markdown(text)
        self.assertNotIn("Everything passed.", text)
        self.assertNotIn("Do something else.", text)
        self.assertNotIn("Nothing remains.", text)
        self.assertNotIn("A different prompt.", text)
        self.assertIn(
            '"result": "not-run"', parsed["sections"]["Verification evidence"]
        )
        self.assertIn(
            '"completion_condition": "Claude and Codex payload fixtures pass."',
            parsed["sections"]["Exact next action"],
        )
        self.assertIn(
            '"remaining_code": true',
            parsed["sections"]["Remaining code by active scope"],
        )
        self.assertEqual(
            parsed["sections"]["Next-session prompt"],
            f"```text\n{self.continuation['next_session_prompt']}\n```",
        )

    def test_validator_rejects_contradictory_derived_sections(self) -> None:
        text = render_record(self.continuation)
        parsed = parse_markdown(text)
        replacements = {
            "Verification evidence": "Everything passed.",
            "Exact next action": "Do something else.",
            "Remaining code by active scope": "Nothing remains.",
            "Next-session prompt": "```text\nA different prompt.\n```",
        }
        for section, replacement in replacements.items():
            with self.subTest(section=section):
                contradictory = text.replace(
                    parsed["sections"][section], replacement, 1
                )
                self.assertIn("section-consistency", issue_codes(contradictory))

        audit_text = render_record(self.audit)
        audit_verification = parse_markdown(audit_text)["sections"][
            "Verification evidence"
        ]
        contradictory_audit = audit_text.replace(
            audit_verification, "No verification was performed.", 1
        )
        self.assertIn("section-consistency", issue_codes(contradictory_audit))

    def test_v2_carries_one_visible_metadata_copy_and_no_derived_sections(
        self,
    ) -> None:
        """Schema v2 stores each fact once, in a metadata block a reader can see."""

        data = make_v2_continuation()
        text = render_record(data)
        self.assertEqual(validate_markdown(text), [])
        self.assertTrue(text.startswith("```json agent-handoff-metadata\n"))
        self.assertNotIn("<!-- agent-handoff-metadata", text)

        headings = [
            line.removeprefix("## ")
            for line in text.splitlines()
            if line.startswith("## ")
        ]
        self.assertEqual(
            headings,
            [
                "Objective",
                "Authoritative references",
                "User decisions",
                "Repository state",
                "Completed work",
                "Incomplete work and risks",
                "External effects",
            ],
        )
        # Each dropped section existed only as a second copy of metadata.
        for dropped in (
            "## Verification evidence",
            "## Exact next action",
            "## Remaining code by active scope",
            "## Next-session prompt",
        ):
            self.assertNotIn(dropped, text)
        self.assertEqual(text.count(str(data["next_session_prompt"])), 1)
        self.assertEqual(
            text.count(str(data["active_scopes"][0]["scope_definition_digest"])), 1
        )

        parsed = parse_markdown(text)
        self.assertEqual(parsed["schema_version"], 2)
        self.assertEqual(parsed["verification"], data["verification"])
        self.assertEqual(parsed["exact_action"], data["exact_action"])
        self.assertEqual(render_record(parsed), text)

    def test_v2_audit_keeps_the_sentinel_before_visible_metadata(self) -> None:
        text = render_record(make_v2_audit())
        self.assertEqual(validate_markdown(text), [])
        self.assertTrue(
            text.startswith(
                "> Audit record — not a handoff. Do not use this file to start or "
                "continue a session.\n\n```json agent-handoff-metadata\n"
            )
        )
        headings = [
            line.removeprefix("## ")
            for line in text.splitlines()
            if line.startswith("## ")
        ]
        self.assertEqual(
            headings,
            [
                "Completed objective",
                "Authoritative references",
                "User decisions",
                "Final repository state",
                "Completed work",
                "Known risks or separately tracked follow-ups",
                "External effects",
            ],
        )
        self.assertNotIn("## Verification evidence", text)

    def test_v2_rejects_the_v1_metadata_comment_form(self) -> None:
        text = render_record(make_v2_continuation())
        payload, remainder = text.split("```json agent-handoff-metadata\n", 1)[1].split(
            "\n```\n", 1
        )
        commented = "<!-- agent-handoff-metadata\n" + payload + "\n-->\n" + remainder
        self.assertIn("metadata-form", issue_codes(commented))

    def test_v1_rejects_the_v2_visible_metadata_form(self) -> None:
        text = render_record(self.continuation)
        payload, remainder = text.split("<!-- agent-handoff-metadata\n", 1)[1].split(
            "\n-->\n", 1
        )
        fenced = "```json agent-handoff-metadata\n" + payload + "\n```\n" + remainder
        self.assertIn("metadata-form", issue_codes(fenced))

    def test_v1_keeps_its_comment_metadata_and_derived_sections(self) -> None:
        text = render_record(self.continuation)
        self.assertTrue(text.startswith("<!-- agent-handoff-metadata\n"))
        self.assertIn("## Verification evidence", text)
        self.assertIn("## Next-session prompt", text)

    def test_a_metadata_fence_inside_a_section_is_body_text_not_metadata(self) -> None:
        """Only the block at the record's fixed position is metadata."""

        planted = (
            "Body text.\n\n```json agent-handoff-metadata\n"
            '{\n  "record_type": "completion-audit",\n  "schema_version": 2\n}\n```'
        )
        for data in (self.continuation, make_v2_continuation()):
            with self.subTest(schema_version=data["schema_version"]):
                data["sections"]["Objective"] = planted
                text = render_record(data)
                # The imitation is ordinary body text, so the record stays
                # exactly as valid as it was without it.
                self.assertEqual(validate_markdown(text), [])
                parsed = parse_markdown(text)
                self.assertEqual(parsed["record_type"], "continuation")
                self.assertEqual(parsed["schema_version"], data["schema_version"])
                self.assertEqual(parsed["sections"]["Objective"], planted)

    def test_render_tail_command_emits_the_enforced_terminal_response(self) -> None:
        """`Stop` enforces `render_terminal_response`, so the CLI must emit it."""

        for data in (
            self.continuation,
            self.audit,
            make_v2_continuation(),
            make_v2_audit(),
        ):
            with self.subTest(
                schema_version=data["schema_version"], record_type=data["record_type"]
            ):
                text = render_record(data)
                with tempfile.TemporaryDirectory() as directory:
                    record = Path(directory) / "record.md"
                    record.write_text(text, encoding="utf-8", newline="\n")
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "agent_handoff_toolkit.cli",
                            "render-tail",
                            str(record),
                        ],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout.replace("\r\n", "\n"),
                        render_terminal_response(record, text) + "\n",
                    )

    def test_a_second_metadata_block_cannot_hide_beside_the_visible_one(self) -> None:
        """A v2 record carries one metadata block; a hidden comment is rejected."""

        text = render_record(make_v2_continuation())
        hidden = (
            text + '\n<!-- agent-handoff-metadata\n{"record_type": "completion-audit",'
            ' "schema_version": 1}\n-->\n'
        )
        self.assertIn("metadata-form", issue_codes(hidden))

    def test_headings_inside_backtick_and_tilde_fences_are_not_sections(self) -> None:
        objective = (
            "````markdown\n## Hidden in backticks\n```\n"
            "## Still hidden\n````\n\n"
            "~~~markdown\n## Hidden in tildes\n~~~~"
        )
        self.continuation["sections"]["Objective"] = objective
        text = render_record(self.continuation)
        self.assertEqual(validate_markdown(text), [])
        self.assertEqual(parse_markdown(text)["sections"]["Objective"], objective)

    def test_render_uses_canonical_section_order_not_json_key_order(self) -> None:
        self.continuation["sections"] = dict(
            reversed(list(self.continuation["sections"].items()))
        )
        text = render_record(self.continuation)
        headings = [
            line.removeprefix("## ")
            for line in text.splitlines()
            if line.startswith("## ")
        ]
        self.assertEqual(headings[0], "Objective")
        self.assertEqual(headings[-1], "Next-session prompt")

    def test_audit_begins_with_sentinel(self) -> None:
        text = render_record(self.audit)
        self.assertTrue(
            text.startswith(
                "> Audit record — not a handoff. Do not use this file to start or continue a session."
            )
        )

    def test_continuation_tail_has_exact_prompt_and_absolute_final_link(self) -> None:
        text = render_record(self.continuation)
        tail = render_tail(
            r"C:\work spaces\handoffs\继续工作.md",
            text,
        )
        prompt = self.continuation["next_session_prompt"]
        self.assertEqual(
            tail,
            "This session is stopped because authorized work remains.\n\n"
            "What you need to do: Start a new session from the continuation "
            "handoff below.\n\n"
            "```text\n"
            "Continue from handoff: C:/work spaces/handoffs/继续工作.md\n"
            "Exact next action: Implement the host payload normalizer.\n"
            "Target: src/handoff_toolkit/hooks.py\n"
            "Constraints: Keep the adapter independent of record policy.\n"
            "Completion gate: Claude and Codex payload fixtures pass.\n"
            "Essential blockers, decisions, and validation gates:\n"
            f"{prompt}\n"
            "```\n\n"
            "[Continuation handoff](<C:/work%20spaces/handoffs/"
            "%E7%BB%A7%E7%BB%AD%E5%B7%A5%E4%BD%9C.md>)",
        )
        self.assertEqual(tail.rstrip().splitlines()[-1], tail.splitlines()[-1])
        self.assertNotIn("<!-- agent-handoff-metadata", tail)
        self.assertNotIn("## Objective", tail)

    def test_v2_terminal_response_is_the_entire_canonical_continuation(self) -> None:
        text = render_record(make_v2_continuation()).replace("\n", "\r\n")

        response = render_terminal_response(
            r"C:\work spaces\handoffs\继续工作.md",
            text,
        )

        self.assertEqual(
            response,
            "This session is stopped because authorized work remains.\n\n"
            "What you need to do: Start a new session from the continuation "
            "handoff below.\n\n"
            "```text\n"
            "Continue from handoff: C:/work spaces/handoffs/继续工作.md\n"
            "Exact next action: Implement the host payload normalizer.\n"
            "Target: src/handoff_toolkit/hooks.py\n"
            "Constraints: Keep the adapter independent of record policy.\n"
            "Completion gate: Claude and Codex payload fixtures pass.\n"
            "Essential blockers, decisions, and validation gates:\n"
            "- Decision: preserve newer live state and report material conflicts "
            "before editing.\n"
            "```\n\n"
            "[Continuation handoff](<C:/work%20spaces/handoffs/"
            "%E7%BB%A7%E7%BB%AD%E5%B7%A5%E4%BD%9C.md>)",
        )
        self.assertEqual(response, response.rstrip())
        self.assertNotIn("Summary:", response)

    def test_v2_terminal_response_is_only_the_canonical_audit_link(self) -> None:
        response = render_terminal_response(
            "/tmp/work spaces/audit.md",
            render_record(make_v2_audit()),
        )

        self.assertEqual(
            response,
            "[Audit record (not a handoff)](</tmp/work%20spaces/audit.md>)",
        )

    def test_v2_terminal_response_rejects_noncanonical_no_action_content(
        self,
    ) -> None:
        text = render_record(make_v2_continuation()).replace(
            "Implement the host payload normalizer.",
            "None.",
        )

        with self.assertRaisesRegex(ValueError, "exact-action-state"):
            render_terminal_response("/tmp/handoff.md", text)

    def test_terminal_response_preserves_v1_tail_bytes(self) -> None:
        text = render_record(self.continuation)
        path = "/tmp/work spaces/continuation.md"

        self.assertEqual(
            render_terminal_response(path, text),
            render_tail(path, text),
        )

    def test_posix_tail_and_audit_tail(self) -> None:
        continuation_tail = render_tail(
            "/tmp/work spaces/continuación.md",
            render_record(self.continuation),
        )
        self.assertTrue(
            continuation_tail.endswith(
                "[Continuation handoff](</tmp/work%20spaces/continuaci%C3%B3n.md>)"
            )
        )

        audit_tail = render_tail(
            "/tmp/work spaces/audit.md",
            render_record(self.audit),
        )
        self.assertEqual(
            audit_tail,
            "[Audit record (not a handoff)](</tmp/work%20spaces/audit.md>)",
        )
        self.assertNotIn("```", audit_tail)

    def test_tail_rejects_prompt_code_blocks_instead_of_reproducing_them(self) -> None:
        self.continuation["next_session_prompt"] = (
            "Inspect this example:\n```python\nprint('safe')\n```\nThen continue."
        )
        with self.assertRaisesRegex(ValueError, "next-prompt-document"):
            render_record(self.continuation)

    def test_tail_rejects_a_generated_summary_over_the_output_budget(self) -> None:
        self.continuation["exact_action"]["constraints"] = "x" * 2200

        with self.assertRaisesRegex(ValueError, "continuation-tail-size"):
            render_record(self.continuation)

    def test_tail_preserves_list_valued_v1_exact_action_fields(self) -> None:
        self.continuation["exact_action"]["constraints"] = [
            "Keep record policy in the core.",
            "Keep adapters host-specific.",
        ]

        tail = render_tail("/tmp/handoff.md", render_record(self.continuation))

        self.assertIn(
            "Constraints: Keep record policy in the core.; Keep adapters host-specific.",
            tail,
        )

    def test_tail_rejects_an_oversized_complete_output_from_path_expansion(
        self,
    ) -> None:
        text = render_record(self.continuation)
        oversized_path = "/tmp/" + ("é" * 1000) + ".md"

        with self.assertRaisesRegex(ValueError, "continuation-tail-size"):
            render_tail(oversized_path, text)

    def test_validation_uses_the_real_record_path_for_tail_budget(self) -> None:
        self.continuation["exact_action"]["constraints"] = "x" * 1700
        text = render_record(self.continuation)
        record_path = "C:/" + ("nested-directory/" * 12) + "handoff.md"

        self.assertEqual(validate_markdown(text), [])
        self.assertIn(
            "continuation-tail-size",
            {issue.code for issue in validate_markdown(text, record_path=record_path)},
        )

    def test_validation_reports_unsafe_record_paths_as_issues(self) -> None:
        text = render_record(self.continuation)

        issues = validate_markdown(text, record_path="<unsafe>.md")

        self.assertIn("unsafe-path", {issue.code for issue in issues})

    def test_tail_rejects_unsafe_markdown_path_characters(self) -> None:
        text = render_record(self.continuation)
        for path in (
            "/tmp/bad>\nINJECTED.md",
            "/tmp/bad<destination.md",
            "/tmp/carriage\rreturn.md",
            "/tmp/nul\x00byte.md",
            "/tmp/control\x01.md",
            "/tmp/line-separator\u2028injected.md",
        ):
            with (
                self.subTest(path=repr(path)),
                self.assertRaisesRegex(ValueError, "unsafe-path"),
            ):
                render_tail(path, text)

    def test_tail_allows_unicode_format_characters_in_paths(self) -> None:
        text = render_record(self.continuation)
        path = "/tmp/👩‍💻-क्‍ष.md"
        tail = render_tail(path, text)
        self.assertTrue(
            tail.endswith(
                "[Continuation handoff](</tmp/"
                "%F0%9F%91%A9%E2%80%8D%F0%9F%92%BB-"
                "%E0%A4%95%E0%A5%8D%E2%80%8D%E0%A4%B7.md>)"
            )
        )


class CommandLineTests(unittest.TestCase):
    def run_cli(
        self, *args: str, env_overrides: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **__import__("os").environ,
            "PYTHONPATH": str(ROOT / "src"),
        }
        if env_overrides:
            environment.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-m", "agent_handoff_toolkit", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_validate_returns_nonzero_for_invalid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.md"
            path.write_text("# not a record\n", encoding="utf-8")
            result = self.run_cli("validate", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("metadata-missing", result.stderr)

    def test_render_validate_and_render_tail_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "record with spaces.md"
            rendered = self.run_cli(
                "render",
                str(FIXTURES / "continuation.json"),
                "--output",
                str(output),
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            self.assertTrue(output.exists())

            validated = self.run_cli("validate", str(output))
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("valid:", validated.stdout)

            tail = self.run_cli("render-tail", str(output))
            self.assertEqual(tail.returncode, 0, tail.stderr)
            self.assertEqual(
                tail.stdout.rstrip().splitlines()[-1],
                "[Continuation handoff](<"
                f"{output.resolve().as_posix().replace(' ', '%20')}>)",
            )

    def test_validate_uses_the_output_path_for_tail_budget(self) -> None:
        data = load_fixture("continuation.json")
        data["exact_action"]["constraints"] = "x" * 1700
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deep = root.joinpath(*(["nested-directory-with-padding"] * 5))
            deep.mkdir(parents=True)
            record = deep / "continuation.md"
            record.write_text(render_record(data), encoding="utf-8", newline="\n")

            result = self.run_cli("validate", str(record))

        self.assertEqual(result.returncode, 1)
        self.assertIn("continuation-tail-size", result.stderr)

    def test_render_tail_forces_utf8_when_ambient_encoding_is_legacy(self) -> None:
        data = load_fixture("continuation.json")
        data["next_session_prompt"] = "继续工作并保留验证证据。"
        with tempfile.TemporaryDirectory() as directory:
            unicode_directory = Path(directory) / "交接"
            unicode_directory.mkdir()
            record = unicode_directory / "继续工作.md"
            record.write_text(render_record(data), encoding="utf-8", newline="\n")

            result = self.run_cli(
                "render-tail",
                str(record),
                env_overrides={"PYTHONIOENCODING": "cp1252"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("继续工作并保留验证证据。", result.stdout)
        self.assertIn(
            "%E4%BA%A4%E6%8E%A5/%E7%BB%A7%E7%BB%AD%E5%B7%A5%E4%BD%9C.md",
            result.stdout.replace("\\", "/"),
        )
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
