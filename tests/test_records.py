from __future__ import annotations

import copy
import json
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
    validate_markdown,
)

FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def issue_codes(text: str) -> set[str]:
    return {issue.code for issue in validate_markdown(text)}


class RecordValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.continuation = load_fixture("continuation.json")
        self.audit = load_fixture("completion-audit.json")

    def test_valid_record_dispatch(self) -> None:
        self.assertEqual(validate_markdown(render_record(self.continuation)), [])
        self.assertEqual(validate_markdown(render_record(self.audit)), [])

    def test_rejects_unknown_record_type(self) -> None:
        self.continuation["record_type"] = "summary"
        with self.assertRaisesRegex(ValueError, "record-type"):
            render_record(self.continuation)

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
