"""Host policy tests use only ephemeral synthetic input and local state."""

import base64
from dataclasses import replace
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import (  # noqa: E402
    HookExecution,
    normalize_event,
    render_hook_execution,
)
from agent_handoff_toolkit.hooks import run_hook  # noqa: E402
from agent_handoff_toolkit.lifecycle import (  # noqa: E402
    DecisionKind,
    EnforcementMode,
    EventName,
    LifecycleDecision,
    LifecycleIssue,
)
from agent_handoff_toolkit.lifecycle_operations import LifecycleService  # noqa: E402
from agent_handoff_toolkit.lifecycle_storage import (  # noqa: E402
    LocalLifecycleStorage,
    StaleLifecycleState,
)
from agent_handoff_toolkit.lineage import (  # noqa: E402
    canonical_json_bytes,
    record_digest,
)
from agent_handoff_toolkit.records import (  # noqa: E402
    render_record,
    render_resume_prompt,
    render_terminal_response,
)
from test_lifecycle import make_record  # noqa: E402


def payload(root, event="Stop", **changes):
    value = {
        "session_id": "session-1",
        "cwd": str(root),
        "transcript_path": str(root / "transcript.jsonl"),
        "hook_event_name": event,
        "turn_id": "turn-1",
    }
    if event == "Stop":
        value.update(stop_hook_active=False, last_assistant_message="Informational.")
    elif event == "PreToolUse":
        value.update(tool_name="Bash", tool_input={"command": "git status"})
    else:
        value.update(prompt="Inspect the synthetic task.")
    value.update(changes)
    return value


class NormalizationTests(unittest.TestCase):
    def test_normalizes_hosts_aliases_transient_fields_and_hashes_session(self):
        for host in ("claude", "codex"):
            for alias, canonical, enum in (
                ("Stop", "Stop", EventName.STOP),
                ("pre_tool_use", "PreToolUse", EventName.PRE_TOOL_USE),
                (
                    "user-prompt-submit",
                    "UserPromptSubmit",
                    EventName.USER_PROMPT_SUBMIT,
                ),
            ):
                with self.subTest(host=host, event=alias):
                    data = payload(ROOT, canonical)
                    if host == "claude":
                        data.pop("turn_id")
                    event = normalize_event(host, alias, data, ROOT)
                    self.assertEqual(event.host, host)
                    self.assertEqual(event.event, enum)
                    self.assertEqual(event.repository_root, ROOT.as_posix())
                    self.assertEqual(len(event.session_key), 64)
                    self.assertNotIn("session-1", event.session_key)
                    self.assertEqual(
                        event.external_user_turn, enum is EventName.USER_PROMPT_SUBMIT
                    )
                    if enum is EventName.PRE_TOOL_USE:
                        self.assertEqual(event.tool_input["command"], "git status")
                        self.assertEqual(event.tool_capability, "mutation-capable")

    def test_rejects_missing_wrong_type_oversize_null_and_unsafe_fields(self):
        for host in ("claude", "codex"):
            for event_name in ("Stop", "PreToolUse", "UserPromptSubmit"):
                required = ["session_id", "cwd", "transcript_path"]
                required += {
                    "Stop": ["stop_hook_active", "last_assistant_message"],
                    "PreToolUse": ["tool_name", "tool_input"],
                    "UserPromptSubmit": ["prompt"],
                }[event_name]
                if host == "codex":
                    required.append("turn_id")
                for field in required:
                    for value in (None, [], 42, "\x00", "x" * 131073):
                        if (
                            host == "codex"
                            and field == "transcript_path"
                            and value is None
                        ):
                            continue
                        data = payload(ROOT, event_name)
                        data[field] = value
                        with (
                            self.subTest(
                                host=host,
                                event=event_name,
                                field=field,
                                kind=type(value).__name__,
                            ),
                            self.assertRaises(ValueError),
                        ):
                            normalize_event(host, event_name, data, ROOT)
                    data = payload(ROOT, event_name)
                    del data[field]
                    with self.subTest(missing=field), self.assertRaises(ValueError):
                        normalize_event(host, event_name, data, ROOT)

    def test_rejects_spoofed_host_event_cwd_and_nested_unbounded_input(self):
        for changes in (
            {"hook_event_name": "Stop"},
            {"cwd": str(ROOT.parent)},
            {"tool_input": {"nested": ["x"] * 257}},
            {"tool_input": {"command": "x" * 4097}},
            {"external_user_turn": "true"},
            {"extra": "\t"},
            {"extra": "\u2028"},
        ):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                normalize_event(
                    "codex", "PreToolUse", payload(ROOT, "PreToolUse", **changes), ROOT
                )
        with self.assertRaises(ValueError):
            normalize_event("CODEX", "Stop", payload(ROOT), ROOT)

    def test_source_line_endings_normalize_without_trimming_message(self):
        event = normalize_event(
            "codex",
            "Stop",
            payload(ROOT, last_assistant_message="First\r\nSecond"),
            ROOT,
        )
        self.assertEqual(event.latest_assistant_message, "First\nSecond")
        for value in (
            " trailing ",
            "\nleading",
            "unsafe\u2028line",
            "unsafe\u202etext",
        ):
            with self.assertRaises(ValueError):
                normalize_event(
                    "codex", "Stop", payload(ROOT, last_assistant_message=value), ROOT
                )

    def test_host_output_is_silent_on_allow_and_blocking_json_on_failure(self):
        issue = LifecycleIssue(
            "AHK-STOP-WORK", "Work remains.", "Continue authorized work."
        )
        for host in ("claude", "codex"):
            self.assertEqual(
                render_hook_execution(
                    host, EventName.STOP, LifecycleDecision(DecisionKind.ALLOW)
                ),
                HookExecution(),
            )
            blocked = render_hook_execution(
                host, EventName.STOP, LifecycleDecision(DecisionKind.BLOCK, (issue,))
            )
            self.assertEqual(blocked.exit_code, 0)
            self.assertEqual(blocked.stderr, "")
            self.assertEqual(json.loads(blocked.stdout)["decision"], "block")
            failed = render_hook_execution(
                host,
                EventName.STOP,
                LifecycleDecision(DecisionKind.POLICY_FAILURE, (issue,)),
            )
            data = json.loads(failed.stdout)
            self.assertFalse(data["continue"])
            self.assertIn("policy failure", data["systemMessage"].lower())
            self.assertNotIn("decision", data)

    def test_hook_reason_never_echoes_arbitrary_issue_content_and_stays_bounded(self):
        sentinel = "sensitive-synthetic-sentinel"
        issue = LifecycleIssue(
            "AHK-STOP-ROOT",
            sentinel,
            sentinel,
            expected="root-1",
            actual=sentinel + " credential text",
            candidate_path="/" + "x" * 2000 + ".md",
        )
        output = render_hook_execution(
            "codex",
            EventName.STOP,
            LifecycleDecision(DecisionKind.BLOCK, (issue,), sentinel),
        )
        reason = json.loads(output.stdout)["reason"]
        self.assertNotIn(sentinel, reason)
        self.assertIn("root-1", reason)
        self.assertIn("AHK-STOP-ROOT", reason)
        self.assertLessEqual(len(reason.encode()), 1200)

    def test_real_user_corrective_prefix_does_not_establish_synthetic_provenance(self):
        event = normalize_event(
            "codex",
            "UserPromptSubmit",
            payload(
                ROOT, "UserPromptSubmit", prompt="AHK-STOP-WORK: This is a real user."
            ),
            ROOT,
        )
        self.assertTrue(event.external_user_turn)


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        self.service = LifecycleService(self.storage, "session-1")
        (self.root / "handoffs").mkdir()
        (self.root / ".agent-handoff-toolkit").mkdir()
        # Installed hooks resolve the repository root by walking ancestors for
        # `.git`. Mark this temporary consumer as that root so an unrelated
        # repository above the platform temporary directory cannot claim it.
        (self.root / ".git").mkdir()
        (self.root / ".agent-handoff-toolkit" / "runner.py").write_text(
            "# owned runner\n"
        )

    def invoke(self, name="Stop", host="codex", **changes):
        return run_hook(
            host,
            name,
            json.dumps(payload(self.root, name, **changes)),
            self.root,
            self.storage,
        )

    def register(self):
        self.assertEqual(self.invoke("UserPromptSubmit"), HookExecution())
        snapshot = self.storage.load_snapshot("session-1")
        scope = make_record("continuation", record_id="first")["active_scopes"][0]
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(scope["scope_definition"]))
            .decode()
            .rstrip("=")
        )
        return self.service.register_root(
            challenge=snapshot.session.bootstrap_challenge,
            scope_id="issue-1",
            scope_kind="issue",
            scope_definition_b64=encoded,
            expected_session_revision=snapshot.session.targeted_revision,
        )

    def record(
        self, kind="continuation", name="first", predecessor=None, data=None, crlf=False
    ):
        state = self.storage.load_snapshot("session-1")
        data = data or make_record(kind, record_id=name, predecessor=predecessor)
        data["authorization_id"] = state.chain.authorization_id
        data["authorization_evidence"] = {
            "kind": "initial-user-turn",
            "user_turn_ref": state.chain.authorization_user_turn_reference,
            "proposal_turn_ref": None,
            "evidence_hmac": state.chain.authorization_evidence_hmac,
        }
        text = render_record(data)
        if crlf:
            text = text.replace("\n", "\r\n")
        path = self.root / "handoffs" / (name + ".md")
        path.write_bytes(text.encode())
        return path, text, render_terminal_response(path, text)

    def assert_block(self, execution, code):
        self.assertEqual(execution.exit_code, 0)
        self.assertEqual(execution.stderr, "")
        data = json.loads(execution.stdout)
        reason = data.get(
            "reason",
            data.get("hookSpecificOutput", {}).get(
                "permissionDecisionReason", data.get("systemMessage", "")
            ),
        )
        self.assertIn(code, reason)
        self.assertLessEqual(len(reason.encode()), 1200)

    def test_untracked_information_and_intrinsic_reads_are_silent(self):
        for host, names in (
            ("claude", ("Read", "Glob", "Grep")),
            ("codex", ("view_image",)),
        ):
            self.assertEqual(self.invoke(host=host), HookExecution())
            for name in names:
                self.assertEqual(
                    self.invoke("PreToolUse", host=host, tool_name=name, tool_input={}),
                    HookExecution(),
                )

    def test_untracked_mutations_require_exact_current_bootstrap_and_owned_runner(self):
        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        command = f"python {runner} lifecycle join --session-key {session.session_key} --challenge {capability} --authorization-id auth-1 --expected-chain-revision 1 --expected-session-revision {session.targeted_revision}"
        for host in ("claude", "codex"):
            for tool in ("Bash", "Write", "apply_patch", "mcp__fs__read", "unknown"):
                self.assert_block(
                    self.invoke("PreToolUse", host=host, tool_name=tool), "AHK-PRE-ROOT"
                )
            self.assertEqual(
                self.invoke("PreToolUse", host=host, tool_input={"command": command}),
                HookExecution(),
            )
            for malicious in (
                command.replace("/", "\\"),
                command + "; whoami",
                command + " && echo x",
                command.replace(capability, "expired"),
                command.replace("revision 1", "revision 0"),
            ):
                self.assert_block(
                    self.invoke(
                        "PreToolUse", host=host, tool_input={"command": malicious}
                    ),
                    "AHK-PRE-ROOT",
                )
            feedback = json.loads(self.invoke("PreToolUse", host=host).stdout)[
                "hookSpecificOutput"
            ]["permissionDecisionReason"]
            self.assertIn("--challenge " + capability, feedback)
            self.assertIn("--expected-session-revision 1", feedback)
            self.assertEqual(feedback.count("Command: "), 1)
            self.assertIn("lifecycle register-root --session-key", feedback)
        (self.root / ".agent-handoff-toolkit" / "runner.py").unlink()
        self.assert_block(
            self.invoke("PreToolUse", tool_input={"command": command}),
            "AHK-HOOK-RUNTIME",
        )

    def test_tracked_tools_are_silent_and_sensitive_content_never_persists(self):
        sentinel = "sensitive-synthetic-sentinel"
        raw_session = "private-host-session-sentinel"
        output = self.invoke(
            "UserPromptSubmit", session_id=raw_session, prompt=sentinel
        )
        self.assertNotIn(sentinel, output.stdout + output.stderr)
        self.assertNotIn(raw_session, self.storage.registry_path.read_text())
        self.assertNotIn(sentinel, self.storage.registry_path.read_text())
        self.register()
        for name in ("Bash", "apply_patch", "mcp__fs__write", "unknown"):
            self.assertEqual(
                self.invoke(
                    "PreToolUse", tool_name=name, tool_input={"command": sentinel}
                ),
                HookExecution(),
            )
        self.assertNotIn(sentinel, self.storage.registry_path.read_text())

    def test_exact_initial_continuation_and_audit_publish_atomically(self):
        self.register()
        path, text, message = self.record(crlf=True)
        before = self.storage.load_snapshot("session-1")
        self.assertEqual(
            self.invoke(last_assistant_message=message.replace("\n", "\r\n")),
            HookExecution(),
        )
        after = self.storage.load_snapshot("session-1")
        self.assertEqual(
            after.chain.targeted_revision, before.chain.targeted_revision + 1
        )
        self.assertEqual(after.session.chain_revision, after.chain.targeted_revision)
        self.assertEqual(
            after.chain.current_record_reference.sha256,
            hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest(),
        )
        predecessor = {
            "record_id": "first",
            "path": path.as_posix(),
            "sha256": record_digest(text),
        }
        _, _, audit = self.record("completion-audit", "audit", predecessor)
        self.assertEqual(self.invoke(last_assistant_message=audit), HookExecution())
        final = self.storage.load_snapshot("session-1")
        self.assertEqual(final.chain.status, "complete")
        self.assertEqual(final.session.mode, EnforcementMode.COMPLETE)

    def test_block_cycle_deduplication_cosmetic_changes_and_external_reset(self):
        self.register()
        first = self.invoke(last_assistant_message="No record.")
        self.assert_block(first, "AHK-STOP-WORK")
        reason = json.loads(first.stdout)["reason"]
        before = self.storage.load_snapshot("session-1").session
        self.assertIsNotNone(before.pending_correction_hmac)
        self.assertNotIn(reason, self.storage.registry_path.read_text())
        self.assertEqual(
            self.invoke("UserPromptSubmit", prompt=reason, turn_id="synthetic-turn"),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 1
        )
        second = self.invoke(
            last_assistant_message="Cosmetic correction.", stop_hook_active=True
        )
        self.assert_block(second, "AHK-STOP-WORK")
        self.assertNotEqual(first.stdout, second.stdout)
        third = self.invoke(
            last_assistant_message="Another correction.", stop_hook_active=True
        )
        self.assertFalse(json.loads(third.stdout)["continue"])
        self.assertIn("policy failure", third.stdout)
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit",
                prompt="AHK-STOP-WORK: Actual user turn.",
                turn_id="real-user",
            ),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 0
        )
        self.assert_block(self.invoke(), "AHK-STOP-WORK")

    def test_runtime_input_corruption_and_cas_races_block_without_content(self):
        self.register()
        for raw in (
            "{",
            "[]",
            '{"session_id":"session-1","session_id":"other"}',
            "x" * 131073,
        ):
            self.assert_block(
                run_hook("codex", "Stop", raw, self.root, self.storage),
                "AHK-HOOK-RUNTIME",
            )
        _, _, message = self.record()
        with patch.object(
            self.storage,
            "compare_and_swap",
            side_effect=StaleLifecycleState("sensitive detail"),
        ):
            result = self.invoke(last_assistant_message=message)
        self.assert_block(result, "AHK-STOP-STALE")
        self.assertNotIn("sensitive detail", result.stdout)
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )
        self.storage.registry_path.write_text("corrupt")
        self.assert_block(self.invoke(), "AHK-HOOK-RUNTIME")

    def test_preamble_wrong_path_and_unsafe_candidates_block_without_enumeration(self):
        self.register()
        path, _, message = self.record()
        for value in (
            "Preamble\n" + message,
            message + "\nTrailing",
            message.replace(path.as_posix(), (self.root / "outside.md").as_posix()),
            message.replace("first.md", "first.json"),
            message.replace("first.md", "first<unsafe>.md"),
            message + "\n[extra](</other.md>)",
        ):
            with (
                patch.object(
                    Path, "glob", side_effect=AssertionError("enumerated handoffs")
                ),
                patch.object(
                    Path, "iterdir", side_effect=AssertionError("enumerated handoffs")
                ),
            ):
                result = self.invoke(last_assistant_message=value)
            self.assertTrue(result.stdout)
            self.invoke("UserPromptSubmit", turn_id="reset-" + str(len(value)))
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )

    def test_exact_continuation_reference_resumes_but_paraphrase_cannot(self):
        self.register()
        path, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit",
                session_id="new-session",
                prompt="Please continue from the newest handoff.",
            ),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("new-session").session.mode,
            EnforcementMode.UNTRACKED,
        )
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit",
                session_id="new-session",
                turn_id="user-2",
                prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
            ),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("new-session").session.authorization_id,
            self.storage.load_snapshot("session-1").session.authorization_id,
        )

    def propose(self, turn="assistant-1"):
        snapshot = self.storage.load_snapshot("session-1")
        fields = ("scope_id", "scope_kind", "parent_scope_id", "scope_definition")
        old = [
            {
                key: make_record("continuation", record_id="unused")["active_scopes"][
                    0
                ][key]
                for key in fields
            }
        ]
        new = [
            {
                key: make_record("continuation", record_id="unused", root="issue-2")[
                    "active_scopes"
                ][0][key]
                for key in fields
            }
        ]
        return self.service.propose_transition(
            old_scopes=old,
            new_scopes=new,
            assistant_turn_reference=turn,
            expected_chain_revision=snapshot.chain.targeted_revision,
            expected_session_revision=snapshot.session.targeted_revision,
        )

    def test_pending_transition_requires_first_real_same_session_user_response(self):
        self.register()
        _, _, message = self.record()
        self.invoke(last_assistant_message=message)
        proposed = self.propose()
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit",
                prompt="yes",
                turn_id="model-event",
                external_user_turn=False,
            ),
            HookExecution(),
        )
        self.assertEqual(self.storage.load_snapshot("session-1"), proposed)
        self.invoke(
            "UserPromptSubmit",
            session_id="unrelated",
            prompt="yes",
            turn_id="other-user",
        )
        self.assertEqual(self.storage.load_snapshot("session-1"), proposed)
        ambiguous = self.invoke(
            "UserPromptSubmit", prompt="Yes, but keep both roots.", turn_id="user-2"
        )
        self.assertIn("clarify", ambiguous.stdout.lower())
        state = self.storage.load_snapshot("session-1")
        self.assertEqual(state.chain.locked_root_id, "issue-1")
        self.assertEqual(state.session.pending_transition_reference.status, "pending")
        nonadjacent = self.invoke("UserPromptSubmit", prompt="yes", turn_id="user-3")
        self.assertIn("clarify", nonadjacent.stdout.lower())
        self.assertEqual(
            self.storage.load_snapshot("session-1").chain.locked_root_id, "issue-1"
        )
        self.propose("assistant-3")
        self.assertEqual(
            self.invoke("UserPromptSubmit", prompt="yes", turn_id="user-4"),
            HookExecution(),
        )
        approved = self.storage.load_snapshot("session-1")
        self.assertEqual(approved.chain.locked_root_id, "issue-2")
        self.assertIsNotNone(approved.chain.publication_evidence)

    def test_rejection_cancels_adjacent_proposal_without_changing_root(self):
        self.register()
        _, _, message = self.record()
        self.invoke(last_assistant_message=message)
        self.propose()
        self.assertEqual(
            self.invoke("UserPromptSubmit", prompt="no", turn_id="reject-turn"),
            HookExecution(),
        )
        state = self.storage.load_snapshot("session-1")
        self.assertEqual(state.chain.locked_root_id, "issue-1")
        self.assertIsNone(state.session.pending_transition_reference)

    def decision(self, category="repository-approval"):
        state = self.storage.load_snapshot("session-1")
        return self.service.request_decision(
            question="Approve the selected target?",
            reason="Repository approval is required.",
            category=category,
            blocked_action_field="target",
            blocked_action_value="staging",
            expected_chain_revision=state.chain.targeted_revision,
            expected_session_revision=state.session.targeted_revision,
        )

    def test_exact_decision_allows_but_model_assertion_and_nonanswer_retain(self):
        self.register()
        message = self.decision()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assert_block(
            self.invoke(last_assistant_message="Preamble\n" + message),
            "AHK-STOP-DECISION",
        )
        retained = self.invoke(
            "UserPromptSubmit",
            turn_id="user-2",
            prompt="I do not know yet",
            decision_resolved=True,
        )
        self.assertIn("clarify", retained.stdout.lower())
        state = self.storage.load_snapshot("session-1")
        self.assertEqual(state.session.mode, EnforcementMode.AWAITING_DECISION)
        self.assertEqual(state.session.correction_cycle_count, 0)
        self.assertEqual(
            self.invoke("UserPromptSubmit", turn_id="user-3", prompt="yes"),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.TRACKED,
        )

    def test_required_nonempty_input_is_resolved_only_by_deterministic_answer(self):
        self.register()
        self.decision("missing-input")
        for index, text in enumerate(
            ("I do not know yet", "maybe", "yes", "not sure", "I need more time")
        ):
            self.invoke(
                "UserPromptSubmit", turn_id="user-" + str(index + 2), prompt=text
            )
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.mode,
                EnforcementMode.AWAITING_DECISION,
            )
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit", turn_id="answered", prompt="target: staging"
            ),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.TRACKED,
        )

    def test_runtime_failures_from_malformed_stop_count_toward_visible_circuit(self):
        self.register()
        for attempt in (1, 2):
            self.assert_block(
                self.invoke(last_assistant_message=None), "AHK-HOOK-RUNTIME"
            )
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.correction_cycle_count,
                attempt,
            )
        self.assertFalse(
            json.loads(self.invoke(last_assistant_message=None).stdout)["continue"]
        )

    def test_field_validation_rejects_oversized_tool_input_before_attempting_state(
        self,
    ):
        self.assert_block(
            self.invoke("PreToolUse", tool_input={"command": "x" * 8192}),
            "AHK-HOOK-RUNTIME",
        )

    def test_candidate_source_whitespace_binds_digest_and_predecessor(self):
        self.register()
        path, text, message = self.record()
        # Harmless metadata JSON presentation remains part of the source digest.
        text = text.replace('"schema_version": 2', '"schema_version":  2')
        path.write_bytes(text.replace("\n", "\r\n").encode())
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assertEqual(
            self.storage.load_snapshot(
                "session-1"
            ).chain.current_record_reference.sha256,
            hashlib.sha256(text.encode()).hexdigest(),
        )
        predecessor = {
            "record_id": "first",
            "path": path.as_posix(),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        _, _, successor = self.record(name="second", predecessor=predecessor)
        self.assertEqual(self.invoke(last_assistant_message=successor), HookExecution())

    def test_junction_and_symlink_sources_are_rejected(self):
        self.register()
        path, text, message = self.record()
        info = path.stat()

        class Reparse:
            st_mode = info.st_mode
            st_file_attributes = 0x400

        original = Path.lstat

        def reparse(target, *args, **kwargs):
            return Reparse() if target == path else original(target, *args, **kwargs)

        with patch.object(Path, "lstat", reparse):
            self.assert_block(
                self.invoke(last_assistant_message=message), "AHK-HOOK-RUNTIME"
            )
        link = self.root / "handoffs" / "link.md"
        try:
            link.symlink_to(path)
        except OSError:
            # Windows may lack symlink privilege; the reparse branch above runs everywhere.
            return
        self.assert_block(
            self.invoke(last_assistant_message=render_terminal_response(link, text)),
            "AHK-HOOK-RUNTIME",
        )

    @unittest.skipUnless(os.name == "nt", "Windows junction containment")
    def test_real_windows_junction_escape_is_rejected(self):
        self.register()
        _, text, _ = self.record()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escape.md").write_bytes(text.encode())
        junction = self.root / "handoffs" / "junction"
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"New-Item -ItemType Junction -Path '{junction}' -Target '{outside}' | Out-Null",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.assert_block(
                self.invoke(
                    last_assistant_message=render_terminal_response(
                        junction / "escape.md", text
                    )
                ),
                "AHK-HOOK-RUNTIME",
            )
        finally:
            junction.rmdir()

    def test_narrowed_and_invented_initial_roots_block(self):
        self.register()
        for index, root in enumerate(("invented-root", "child-only")):
            data = make_record("continuation", record_id="bad-" + str(index), root=root)
            _, _, message = self.record(name="bad-" + str(index), data=data)
            self.assert_block(
                self.invoke(last_assistant_message=message), "AHK-STOP-ROOT"
            )
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )

    def test_stale_joined_session_cannot_publish(self):
        self.register()
        path, text, message = self.record()
        self.invoke(last_assistant_message=message)
        self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
        )
        predecessor = {
            "record_id": "first",
            "path": path.as_posix(),
            "sha256": record_digest(text),
        }
        _, _, successor = self.record(name="second", predecessor=predecessor)
        self.assertEqual(self.invoke(last_assistant_message=successor), HookExecution())
        self.assert_block(
            self.invoke(session_id="joined", last_assistant_message=successor),
            "AHK-STOP-STALE",
        )

    def test_approved_transition_publication_uses_chain_evidence(self):
        self.register()
        path, text, message = self.record()
        self.invoke(last_assistant_message=message)
        self.propose()
        self.invoke("UserPromptSubmit", prompt="yes", turn_id="user-2")
        snapshot = self.storage.load_snapshot("session-1")
        proof = snapshot.chain.publication_evidence
        data = make_record(
            "continuation",
            record_id="second",
            root="issue-2",
            predecessor={
                "record_id": "first",
                "path": path.as_posix(),
                "sha256": record_digest(text),
            },
        )
        data["authorization_id"] = snapshot.chain.authorization_id
        data["authorization_evidence"] = {
            "kind": "approved-transition",
            "user_turn_ref": proof.approval_turn_reference,
            "proposal_turn_ref": proof.assistant_turn_reference,
            "evidence_hmac": proof.evidence_hmac,
        }
        data["transition"] = {
            "from_authorization_id": proof.from_authorization_id,
            "to_authorization_id": proof.to_authorization_id,
            "old_root_scope_id": "issue-1",
            "new_root_scope_id": "issue-2",
            "proposal_turn_ref": proof.assistant_turn_reference,
            "approval_turn_ref": proof.approval_turn_reference,
            "evidence_hmac": proof.evidence_hmac,
        }
        second = self.root / "handoffs" / "second.md"
        source = render_record(data)
        second.write_bytes(source.encode())
        self.assertEqual(
            self.invoke(
                last_assistant_message=render_terminal_response(second, source)
            ),
            HookExecution(),
        )
        final = self.storage.load_snapshot("session-1")
        self.assertEqual(final.chain.current_record_reference.record_id, "second")
        self.assertIsNone(final.chain.publication_evidence)

    def test_exact_correction_hmac_is_one_use_and_prefix_spoofs_are_external(self):
        self.register()
        result = self.invoke()
        reason = json.loads(result.stdout)["reason"]
        self.invoke("UserPromptSubmit", prompt=reason, turn_id="synthetic")
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 1
        )
        self.invoke("UserPromptSubmit", prompt=reason, turn_id="external-copy")
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 0
        )

    def test_new_provenance_fields_reject_invalid_values_and_never_accept_cli_authority(
        self,
    ):
        self.register()
        state = self.storage.load_snapshot("session-1")
        with self.assertRaises(ValueError):
            replace(state.session, pending_correction_hmac="invalid")
        _, _, message = self.record()
        self.invoke(last_assistant_message=message)
        proposed = self.propose()
        proposal = proposed.session.pending_transition_reference
        self.assertEqual(proposal.source_user_turn_reference, "turn-1")
        with self.assertRaises(ValueError):
            replace(proposal, source_user_turn_reference="unsafe\nreference")
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit", prompt="yes", turn_id="assistant-1"
            ).stdout.count("CLARIFY"),
            1,
        )
        self.assertEqual(
            self.storage.load_snapshot("session-1").chain.locked_root_id, "issue-1"
        )

    def test_repeated_stale_stops_count_without_changing_chain(self):
        self.register()
        path, text, message = self.record()
        self.invoke(last_assistant_message=message)
        self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
        )
        previous_id = "first"
        for attempt in (1, 2, 3):
            predecessor = {
                "record_id": previous_id,
                "path": path.as_posix(),
                "sha256": record_digest(text),
            }
            previous_id = "record-" + str(attempt)
            path, text, message = self.record(name=previous_id, predecessor=predecessor)
            self.assertEqual(
                self.invoke(last_assistant_message=message), HookExecution()
            )
            chain = self.storage.load_snapshot("session-1").chain
            result = self.invoke(session_id="joined", last_assistant_message=message)
            if attempt < 3:
                self.assert_block(result, "AHK-STOP-STALE")
            else:
                self.assertFalse(json.loads(result.stdout)["continue"])
            self.assertEqual(
                self.storage.load_snapshot("joined").session.correction_cycle_count,
                attempt,
            )
            self.assertEqual(self.storage.load_snapshot("session-1").chain, chain)

    def test_bookkeeping_cas_race_reloads_and_blocks_stale(self):
        self.register()
        before = self.storage.load_snapshot("session-1")
        original = self.storage.compare_and_swap
        calls = 0

        def race_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise StaleLifecycleState("concurrent update")
            return original(*args, **kwargs)

        with patch.object(self.storage, "compare_and_swap", race_once):
            self.assert_block(self.invoke(), "AHK-STOP-STALE")
        after = self.storage.load_snapshot("session-1")
        self.assertEqual(after.session.correction_cycle_count, 1)
        self.assertEqual(after.chain, before.chain)

    def test_circuit_remains_visible_until_real_user_turn(self):
        self.register()
        for _ in range(3):
            self.invoke()
        _, _, message = self.record()
        fourth = self.invoke(last_assistant_message=message, stop_hook_active=True)
        self.assertFalse(json.loads(fourth.stdout)["continue"])
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )
        self.invoke(
            "UserPromptSubmit", prompt="Use the rendered record.", turn_id="external"
        )
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())

    def test_real_cli_lifecycle_feedback_and_silent_success_for_both_hosts(self):
        self.register()
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "AHK_STATE_ROOT": str(self.storage.state_root),
        }
        for host in ("claude", "codex"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_handoff_toolkit.cli",
                    "hook",
                    "--platform",
                    host,
                    "--event",
                    "stop",
                ],
                input=json.dumps(payload(self.root)),
                text=True,
                capture_output=True,
                cwd=self.root,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")
            self.assertIn("AHK-STOP-WORK", result.stdout)
            self.assertEqual(result.stderr, "")
        self.invoke("UserPromptSubmit", prompt="Use the record.", turn_id="external")
        _, _, message = self.record()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_handoff_toolkit.cli",
                "hook",
                "--platform",
                "codex",
                "--event",
                "stop",
            ],
            input=json.dumps(payload(self.root, last_assistant_message=message)),
            text=True,
            capture_output=True,
            cwd=self.root,
            env=environment,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_codex_nullable_transcript_for_all_lifecycle_events(self):
        for name in ("Stop", "PreToolUse", "UserPromptSubmit"):
            event = normalize_event(
                "codex", name, payload(self.root, name, transcript_path=None), self.root
            )
            self.assertIsNone(event.transcript_reference)
        self.register()
        self.assert_block(self.invoke(transcript_path=None), "AHK-STOP-WORK")

    def test_malformed_identifiable_stop_fields_reach_circuit_in_four_attempts(self):
        self.register()
        for index, changes in enumerate(
            (
                {"last_assistant_message": "x" * 20000},
                {"unrelated": "\x00"},
                {"tool_input": [1] * 300},
                {"last_assistant_message": "\u2028"},
            ),
            1,
        ):
            output = self.invoke(**changes)
            if index < 3:
                self.assert_block(output, "AHK-HOOK-RUNTIME")
                self.assertIsNotNone(
                    self.storage.load_snapshot(
                        "session-1"
                    ).session.pending_correction_hmac
                )
            else:
                self.assertFalse(json.loads(output.stdout)["continue"])
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.correction_cycle_count,
                index,
            )

    def test_renderer_copied_multiline_prompt_only_resumes_exact_record(self):
        self.register()
        path, _, message = self.record()
        self.invoke(last_assistant_message=message)
        copied = message.split("```text\n", 1)[1].split("\n```", 1)[0]
        for index, text in enumerate(
            (
                "Continue from handoff: " + path.as_posix(),
                copied + "\nMore",
                "Please " + copied,
                copied.replace("Exact next action:", "Next:"),
            )
        ):
            self.invoke(
                "UserPromptSubmit", session_id="other-" + str(index), prompt=text
            )
            self.assertEqual(
                self.storage.load_snapshot("other-" + str(index)).session.mode,
                EnforcementMode.UNTRACKED,
            )
        self.assertEqual(
            self.invoke("UserPromptSubmit", session_id="copy", prompt=copied),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot("copy").session.authorization_id,
            self.storage.load_snapshot("session-1").session.authorization_id,
        )

    def test_completed_session_reentry_demands_new_authority(self):
        self.register()
        _, _, audit = self.record("completion-audit", "audit")
        self.assertEqual(self.invoke(last_assistant_message=audit), HookExecution())
        completed = self.storage.load_snapshot("session-1").chain
        self.assertEqual(
            self.invoke(
                "UserPromptSubmit", prompt="Start another task.", turn_id="fresh-user"
            ),
            HookExecution(),
        )
        fresh = self.storage.load_snapshot("session-1")
        self.assertEqual(fresh.session.mode, EnforcementMode.UNTRACKED)
        self.assertIsNone(fresh.chain)
        self.assertIsNone(fresh.session.authorization_id)
        self.assertIsNotNone(fresh.session.bootstrap_challenge)
        self.assertEqual(self.storage.load_chain(completed.authorization_id), completed)
        self.assert_block(self.invoke("PreToolUse"), "AHK-PRE-ROOT")
        self.assertEqual(self.invoke(), HookExecution())

    def test_emitted_absolute_control_command_executes_owned_runner_from_other_cwd(
        self,
    ):
        init = subprocess.run(
            ["git", "init", str(self.root)], capture_output=True, text=True
        )
        self.assertEqual(init.returncode, 0)
        owned = self.root / ".agent-handoff-toolkit"
        (owned / "runner.py").write_bytes(
            (ROOT / "distribution" / "runner.py").read_bytes()
        )
        shutil.copytree(
            ROOT / "src" / "agent_handoff_toolkit",
            owned / "src" / "agent_handoff_toolkit",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.storage = LocalLifecycleStorage(self.root)
        self.service = LifecycleService(self.storage, "session-1")
        self.invoke("UserPromptSubmit")
        reason = json.loads(self.invoke("PreToolUse").stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        command = reason.split("Command: ", 1)[1]
        definition = (
            base64.urlsafe_b64encode(
                canonical_json_bytes(
                    {
                        "title": "Synthetic task",
                        "outcome": "Complete the synthetic authorized outcome.",
                    }
                )
            )
            .decode()
            .rstrip("=")
        )
        command = (
            command.replace("{scope_id}", "synthetic-root")
            .replace("{scope_kind}", "standalone")
            .replace("{scope_definition_b64}", definition)
        )
        self.assertEqual(command.split()[1], (owned / "runner.py").as_posix())
        self.assertNotIn("session-1", command)
        relative = command.replace(
            (owned / "runner.py").as_posix(), ".agent-handoff-toolkit/runner.py"
        )
        denied = self.invoke("PreToolUse", tool_input={"command": relative})
        repaired = json.loads(denied.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ].split("Command: ", 1)[1]
        self.assertEqual(repaired, command)
        other = self.root / "other-cwd"
        other.mkdir()
        self.assertEqual(
            self.invoke(
                "PreToolUse", tool_input={"command": repaired, "workdir": str(other)}
            ),
            HookExecution(),
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AHK_") and key != "PYTHONPATH"
        }
        result = subprocess.run(
            repaired.split(), cwd=other, env=environment, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        state = self.storage.load_snapshot("session-1")
        self.assertEqual(state.chain.locked_root_id, "synthetic-root")
        self.assertEqual(
            json.loads(result.stdout)["session_key"], state.session.session_key
        )
        replay = subprocess.run(
            repaired.split(), cwd=other, env=environment, text=True, capture_output=True
        )
        self.assertNotEqual(replay.returncode, 0)

    def test_duplicate_root_attempt_selects_one_concrete_join_command(self):
        state = self.register()
        self.invoke(
            "UserPromptSubmit", session_id="other", prompt="Join the same task."
        )
        other = self.storage.load_snapshot("other").session
        definition = (
            base64.urlsafe_b64encode(
                canonical_json_bytes(
                    make_record("continuation", record_id="unused")["active_scopes"][0][
                        "scope_definition"
                    ]
                )
            )
            .decode()
            .rstrip("=")
        )
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        command = f"python {runner} lifecycle register-root --session-key {other.session_key} --challenge {self.storage.control_capability(other)} --scope-id issue-1 --scope-kind issue --scope-definition-b64 {definition} --expected-session-revision {other.targeted_revision}"
        output = self.invoke(
            "PreToolUse", session_id="other", tool_input={"command": command}
        )
        reason = json.loads(output.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn("lifecycle join", reason)
        self.assertIn("--authorization-id " + state.chain.authorization_id, reason)
        self.assertEqual(reason.count("Command: "), 1)
        self.assertNotIn("{", reason)
        self.assertEqual(
            self.invoke(
                "PreToolUse",
                session_id="other",
                tool_input={"command": reason.split("Command: ", 1)[1]},
            ),
            HookExecution(),
        )

    def test_stale_tracked_control_recovers_through_concrete_inspect_command(self):
        self.register()
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        stale = f"python {runner} lifecycle inspect"
        result = self.invoke("PreToolUse", tool_input={"command": stale})
        reason = json.loads(result.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn("AHK-CONTROL-BINDING", reason)
        command = reason.split("Command: ", 1)[1]
        self.assertEqual(
            self.invoke("PreToolUse", tool_input={"command": command}), HookExecution()
        )
        self.invoke(
            "UserPromptSubmit", prompt="Continue this task.", turn_id="new-user"
        )
        self.assert_block(
            self.invoke("PreToolUse", tool_input={"command": command}),
            "AHK-CONTROL-BINDING",
        )


if __name__ == "__main__":
    unittest.main()
