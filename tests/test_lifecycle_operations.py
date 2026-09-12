"""Behavioral tests for lifecycle commands and the shell-free bootstrap gate."""

import base64
from dataclasses import FrozenInstanceError, replace
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.lifecycle_operations import (  # noqa: E402
    LifecycleService,
    parse_bootstrap_command,
)
from agent_handoff_toolkit.lifecycle import (  # noqa: E402
    AuthorityCategory,
    EnforcementMode,
    LifecycleMutation,
    RecordReference,
    EventName,
    NormalizedEvent,
    DecisionKind,
    TerminalCandidate,
    evaluate_stop,
)
from agent_handoff_toolkit.lifecycle_storage import (  # noqa: E402
    LocalLifecycleStorage,
    LifecycleStorageError,
    StaleLifecycleState,
)
from agent_handoff_toolkit.lineage import (  # noqa: E402
    canonical_json_bytes,
    record_digest,
)
from agent_handoff_toolkit.records import (
    render_record,
    parse_markdown,
    render_terminal_response,
)  # noqa: E402
from agent_handoff_toolkit.cli import main  # noqa: E402
from test_lifecycle import (  # noqa: E402
    make_record,
    make_scope,
    restore_v1_sections,
)


DEFINITION = "eyJvdXRjb21lIjoiQ29tcGxldGUgYWxsIGF1dGhvcml6ZWQgaXNzdWUgd29yay4iLCJ0aXRsZSI6Iklzc3VlIDEzMjMifQ"
RUNNER = (Path.cwd() / ".agent-handoff-toolkit" / "runner.py").as_posix()
PREFIX = f"python {RUNNER} lifecycle "
COMMANDS = {
    "register-root": f"--scope-id issue-1323 --scope-kind issue --scope-definition-b64 {DEFINITION}",
    "resume": "--record D:/repo/handoffs/current.md",
    "join": "--authorization-id auth-001 --expected-chain-revision 3",
    "adopt-v1": "--record D:/repo/handoffs/current-v1.md",
}


def command(operation):
    return f"{PREFIX}{operation} --session-key {'a' * 64} --challenge challenge-001 {COMMANDS[operation]} --expected-session-revision 0"


class BootstrapTests(unittest.TestCase):
    def test_relative_runner_is_rejected_even_when_process_cwd_matches(self):
        absolute = command("register-root")
        self.assertIsNone(
            self.parse(absolute.replace(RUNNER, ".agent-handoff-toolkit/runner.py"))
        )
        self.assertIsNotNone(self.parse(absolute))

    def parse(self, value, challenge="challenge-001"):
        return parse_bootstrap_command(
            value, Path.cwd() / ".agent-handoff-toolkit" / "runner.py", challenge
        )

    def test_exact_four_operations_and_immutable_arguments(self):
        for operation in COMMANDS:
            with self.subTest(operation=operation):
                result = self.parse(command(operation))
                self.assertEqual(result.operation, operation)
                self.assertEqual(result.arguments["expected_session_revision"], 0)
                with self.assertRaises(TypeError):
                    result.arguments["challenge"] = "different"
                with self.assertRaises(FrozenInstanceError):
                    result.operation = "different"

    def test_shell_suffixes_rejected_without_execution_on_both_slash_styles(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("shell invocation")),
            patch("subprocess.Popen", side_effect=AssertionError("process invocation")),
            patch("builtins.eval", side_effect=AssertionError("eval invocation")),
        ):
            for operation in COMMANDS:
                for slash in ("/", "\\"):
                    for suffix in (
                        ";whoami",
                        " && x",
                        " || x",
                        "|x",
                        ">x",
                        "<x",
                        "$(x)",
                        "`x`",
                        "\nx",
                        "\rx",
                        "\tx",
                        "\x00",
                        "\u2028",
                        " & x",
                        " %x%",
                        " (x)",
                    ):
                        with self.subTest(
                            operation=operation, slash=slash, suffix=repr(suffix)
                        ):
                            self.assertIsNone(
                                self.parse(
                                    command(operation).replace("/", slash) + suffix
                                )
                            )

    def test_bootstrap_rejects_backslashes_in_runner_or_record_on_every_host(self):
        for operation in COMMANDS:
            value = command(operation)
            self.assertIsNotNone(self.parse(value))
            self.assertIsNone(
                self.parse(
                    value.replace(
                        ".agent-handoff-toolkit/runner.py",
                        ".agent-handoff-toolkit\\runner.py",
                    )
                )
            )
            self.assertIsNone(self.parse(value.replace("/", "\\")))
        self.assertIsNone(
            self.parse(command("resume").replace("D:/repo/", "D:\\repo\\"))
        )

    def test_rejects_noncanonical_tokens_and_challenges(self):
        original = command("register-root")
        bad = [
            original + " --confirmed",
            original + " ",
            " " + original,
            original.replace("python ", "python3 "),
            original.replace("runner.py", "other.py"),
            original.replace(".agent-handoff-toolkit/", "../.agent-handoff-toolkit/"),
            original.replace(
                "--scope-id issue-1323 --scope-kind issue",
                "--scope-kind issue --scope-id issue-1323",
            ),
            original.replace("--scope-kind issue", "--scope-id issue-1323"),
            original.replace("issue-1323", "bad@id"),
            original.replace("--scope-kind issue", "--scope-kind arbitrary"),
            original.replace(DEFINITION, DEFINITION + "="),
            original.replace(DEFINITION, "***"),
            original.replace("revision 0", "revision -1"),
            original.replace("revision 0", "revision 00"),
            original.replace("revision 0", "revision " + "9" * 100),
            original.replace("challenge-001", "challenge-002"),
        ]
        for decoded in (
            b'{"title":"A","outcome":"B"}',
            b'{"outcome":"B","title":"A","extra":1}',
            b'{"outcome":"B","title":"A","title":"A"}',
            b'{"outcome":"B","title":" A"}',
        ):
            bad.append(
                original.replace(
                    DEFINITION, base64.urlsafe_b64encode(decoded).decode().rstrip("=")
                )
            )
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(self.parse(value))
        self.assertIsNone(self.parse(original, "expired-challenge"))
        self.assertIsNone(self.parse(original, ""))

    def test_record_paths_must_be_canonical_absolute_markdown(self):
        for path in (
            "relative.md",
            "D:/repo/../record.md",
            "D:/repo//record.md",
            "D:record.md",
            "D:/repo/record.json",
            "//server/share/record.md",
            "D:/repo/a:stream.md",
        ):
            with self.subTest(path=path):
                self.assertIsNone(
                    self.parse(
                        command("resume").replace("D:/repo/handoffs/current.md", path)
                    )
                )

    def test_excessive_json_nesting_is_rejected_without_escaping_the_parser(self):
        raw = b"[" * 1100 + b"]" * 1100
        value = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertIsNone(
            self.parse(command("register-root").replace(DEFINITION, value))
        )


class OperationsTests(unittest.TestCase):
    def test_cli_uses_derived_binding_ignoring_raw_session_environment(self):
        state = self.storage.load_snapshot("session-1")
        capability = self.storage.control_capability(state.session)
        output, errors = io.StringIO(), io.StringIO()
        args = [
            "lifecycle",
            "inspect",
            "--session-key",
            state.session.session_key,
            "--challenge",
            capability,
            "--expected-session-revision",
            "1",
        ]
        with (
            patch(
                "agent_handoff_toolkit.lifecycle_storage.LocalLifecycleStorage",
                return_value=self.storage,
            ),
            patch.dict(os.environ, {"AHK_SESSION_ID": "wrong-session"}),
            patch("sys.stdout", output),
            patch("sys.stderr", errors),
        ):
            self.assertEqual(main(args), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["session_key"], state.session.session_key)
        self.assertEqual(result["session_revision"], 2)
        self.assertNotEqual(result["challenge"], capability)
        self.assertEqual(errors.getvalue(), "")
        with (
            patch(
                "agent_handoff_toolkit.lifecycle_storage.LocalLifecycleStorage",
                return_value=self.storage,
            ),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            self.assertNotEqual(main(args), 0)

    def test_control_service_register_and_inspect_rotate_derived_capability(self):
        snapshot = self.storage.load_snapshot("session-1")
        key = snapshot.session.session_key
        capability = self.storage.control_capability(snapshot.session)
        service = LifecycleService.for_control(self.storage, key, capability, 1)
        updated = service.register_root(
            challenge=capability,
            scope_id="issue-1323",
            scope_kind="issue",
            scope_definition_b64=DEFINITION,
            expected_session_revision=1,
        )
        self.assertEqual(updated.session.mode, EnforcementMode.TRACKED)
        with self.assertRaises(ValueError):
            LifecycleService.for_control(self.storage, key, capability, 1)
        next_capability = self.storage.control_capability(updated.session)
        inspection = LifecycleService.for_control(self.storage, key, next_capability, 2)
        inspection.consume_control()
        self.assertEqual(inspection.inspect()["session_revision"], 3)
        self.assertEqual(
            inspection.inspect()["authorization_id"], updated.chain.authorization_id
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        self.service = LifecycleService(self.storage, "session-1")
        self.seed("session-1")

    def seed(self, session, turn="user-1"):
        snapshot = self.storage.load_snapshot(session)
        return self.storage.compare_and_swap(
            session,
            0,
            snapshot.session.targeted_revision,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                    current_external_user_turn_reference=turn,
                    bootstrap_challenge="challenge-001",
                )
            ),
        )

    def register(self, service=None):
        return (service or self.service).register_root(
            challenge="challenge-001",
            scope_id="issue-1323",
            scope_kind="issue",
            scope_definition_b64=DEFINITION,
            expected_session_revision=1,
        )

    def test_register_consumes_challenge_and_binds_turn_hmac(self):
        result = self.register()
        self.assertEqual(result.session.mode, EnforcementMode.TRACKED)
        self.assertIsNone(result.session.bootstrap_challenge)
        self.assertIsNone(result.chain.current_record_reference)
        self.assertEqual(result.chain.authorization_user_turn_reference, "user-1")
        self.assertEqual(len(result.chain.authorization_evidence_hmac), 64)
        payload = {
            "kind": "initial-user-turn",
            "authorization_id": result.chain.authorization_id,
            "user_turn_ref": "user-1",
            "proposal_turn_ref": None,
            "role": "user",
            "adjacent": True,
            "scope_digests": list(result.chain.scope_digests),
        }
        expected = hmac.new(
            self.storage.secret, canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
        self.assertEqual(result.chain.authorization_evidence_hmac, expected)
        before = self.storage.registry_path.read_bytes()
        with self.assertRaises(ValueError):
            self.register()
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_invalid_registration_stale_and_duplicate_leave_state_unchanged(self):
        before = self.storage.registry_path.read_bytes()
        for change in (
            {"challenge": "other"},
            {"scope_definition_b64": "bad"},
            {"scope_kind": "other"},
            {"scope_id": "bad@id"},
            {"expected_session_revision": 0},
        ):
            args = dict(
                challenge="challenge-001",
                scope_id="issue-1323",
                scope_kind="issue",
                scope_definition_b64=DEFINITION,
                expected_session_revision=1,
            )
            args.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.register_root(**args)
            self.assertEqual(self.storage.registry_path.read_bytes(), before)
        self.register()
        self.seed("session-2")
        before = self.storage.registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "join"):
            self.register(LifecycleService(self.storage, "session-2"))
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_join_preserves_both_leases_and_rejects_stale_unknown_terminal(self):
        root = self.register()
        self.seed("session-2")
        service = LifecycleService(self.storage, "session-2")
        for auth, revision in (("unknown", 1), (root.chain.authorization_id, 0)):
            with self.assertRaises(ValueError):
                service.join(
                    challenge="challenge-001",
                    authorization_id=auth,
                    expected_chain_revision=revision,
                    expected_session_revision=1,
                )
        result = service.join(
            challenge="challenge-001",
            authorization_id=root.chain.authorization_id,
            expected_chain_revision=1,
            expected_session_revision=1,
        )
        self.assertEqual(result.session.authorization_id, root.chain.authorization_id)
        self.assertEqual(
            len(
                self.storage.load_registry().active_leases(root.chain.authorization_id)
            ),
            2,
        )
        snapshot = self.storage.load_snapshot("session-1")
        self.storage.compare_and_swap(
            "session-1",
            1,
            2,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=3,
                    mode=EnforcementMode.COMPLETE,
                    chain_revision=2,
                ),
                replace(snapshot.chain, targeted_revision=2, status="complete"),
            ),
        )
        self.seed("session-3")
        with self.assertRaises(ValueError):
            LifecycleService(self.storage, "session-3").join(
                challenge="challenge-001",
                authorization_id=root.chain.authorization_id,
                expected_chain_revision=2,
                expected_session_revision=1,
            )

    def record_chain(self):
        root = self.register()
        data = make_record("continuation", record_id="record-1", root="issue-1323")
        data["authorization_id"] = root.chain.authorization_id
        scope = data["active_scopes"][0]
        scope["scope_definition"] = json.loads(
            base64.urlsafe_b64decode(DEFINITION + "==")
        )
        from agent_handoff_toolkit.lineage import scope_definition_digest

        scope["scope_definition_digest"] = scope_definition_digest(scope)
        data["authorization_evidence"]["user_turn_ref"] = "user-1"
        data["authorization_evidence"]["evidence_hmac"] = (
            root.chain.authorization_evidence_hmac
        )
        text = render_record(data)
        reference = RecordReference(
            "record-1", (self.root / "current.md").as_posix(), record_digest(text)
        )
        self.storage.compare_and_swap(
            "session-1",
            1,
            2,
            LifecycleMutation(
                replace(root.session, targeted_revision=3, chain_revision=2),
                replace(
                    root.chain, targeted_revision=2, current_record_reference=reference
                ),
            ),
        )
        return reference, text

    def test_resume_validates_exact_bytes_metadata_digest_and_live_record(self):
        reference, text = self.record_chain()
        self.seed("session-2")
        service = LifecycleService(self.storage, "session-2")
        metadata = parse_markdown(text)
        bad = [
            dict(record_path="relative.md"),
            dict(record_text=text + "\nchanged"),
            dict(record_digest="a" * 64),
            dict(record_metadata={"record_id": "other"}),
            dict(record_path=(self.root / "other.md").as_posix()),
        ]
        for change in bad:
            args = dict(
                challenge="challenge-001",
                record_path=reference.path,
                record_text=text,
                record_metadata=metadata,
                record_digest=reference.sha256,
                expected_session_revision=1,
            )
            args.update(change)
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                service.resume(**args)
        with (
            patch.object(Path, "glob", side_effect=AssertionError("history scan")),
            patch.object(Path, "iterdir", side_effect=AssertionError("history scan")),
        ):
            result = service.resume(
                challenge="challenge-001",
                record_path=reference.path,
                record_text=text,
                record_metadata=metadata,
                record_digest=reference.sha256,
                expected_session_revision=1,
            )
        self.assertEqual(result.chain.current_record_reference, reference)

    def proposal(self, kind="transition", record=None):
        root = self.storage.load_snapshot("session-1")
        old = make_scope(
            "issue-1323", remaining_work=True, remaining_code=True, status="in-progress"
        )
        old["scope_definition"] = json.loads(
            base64.urlsafe_b64decode(DEFINITION + "==")
        )
        new = make_scope(
            "issue-new", remaining_work=True, remaining_code=True, status="in-progress"
        )
        return self.service.propose_transition(
            old_scopes=[old] if root.chain else [],
            new_scopes=[new],
            assistant_turn_reference="assistant-1",
            kind=kind,
            selected_record=record,
            expected_chain_revision=root.chain.targeted_revision if root.chain else 0,
            expected_session_revision=root.session.targeted_revision,
        )

    def user_event(self, message, *, role=True, adjacent="assistant-1"):
        snapshot = self.storage.load_snapshot("session-1")
        turn = "user-next-" + str(snapshot.session.targeted_revision)
        event = NormalizedEvent(
            "codex",
            EventName.USER_PROMPT_SUBMIT,
            snapshot.session.session_key,
            turn,
            self.root.as_posix(),
            "transcript-1",
            False,
            current_user_message=message,
            current_user_reference=turn,
            external_user_turn=role,
        )
        return self.service.observe_user_turn(
            event,
            preceding_assistant_turn_reference=adjacent,
            expected_chain_revision=snapshot.chain.targeted_revision
            if snapshot.chain
            else 0,
            expected_session_revision=snapshot.session.targeted_revision,
        )

    def test_proposal_does_not_authorize_and_approval_atomically_supersedes(self):
        self.record_chain()
        root = self.storage.load_snapshot("session-1")
        proposal = self.proposal()
        self.assertEqual(proposal.chain, root.chain)
        self.assertIsNone(proposal.session.pending_transition_reference.evidence_hmac)
        result = self.user_event("yes")
        self.assertNotEqual(result.chain.authorization_id, root.chain.authorization_id)
        registry = self.storage.load_registry()
        self.assertEqual(
            registry.chains[root.chain.authorization_id].status, "superseded"
        )
        self.assertEqual(result.chain.publication_evidence.status, "consumed")
        self.assertIsNone(result.session.pending_transition_reference)
        self.assertEqual(len(result.chain.publication_evidence.evidence_hmac), 64)
        again = self.user_event("yes")
        self.assertEqual(again.chain.authorization_id, result.chain.authorization_id)

    def test_ambiguous_nonadjacent_assistant_retains_and_negative_cancels(self):
        self.record_chain()
        self.proposal()
        for message, external, previous in (
            ("yes", False, "assistant-1"),
            ("yes", True, "different"),
            ("yes but change it", True, "assistant-1"),
        ):
            result = self.user_event(message, role=external, adjacent=previous)
            self.assertEqual(
                result.session.pending_transition_reference.status, "pending"
            )
            self.assertIsNone(result.session.pending_transition_reference.evidence_hmac)
        result = self.user_event("no")
        self.assertIsNone(result.session.pending_transition_reference)

    def test_request_decision_renders_without_persisting_question_or_reason(self):
        self.register()
        question = "Which target " + secrets.token_hex(12) + "?"
        reason = "Target required " + secrets.token_hex(12)
        response = self.service.request_decision(
            question=question,
            reason=reason,
            category=AuthorityCategory.MISSING_INPUT,
            blocked_action_field="target",
            blocked_action_value="deployment",
            expected_chain_revision=1,
            expected_session_revision=2,
        )
        self.assertEqual(
            response,
            "Authorized work is paused for one required user decision.\n\n"
            f"Decision needed: {question}\nBlocked action field: target\nReason: {reason}",
        )
        raw = self.storage.registry_path.read_bytes()
        self.assertNotIn(question.encode(), raw)
        self.assertNotIn(reason.encode(), raw)
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.AWAITING_DECISION,
        )

    def test_invalid_decision_does_not_mutate(self):
        self.register()
        before = self.storage.registry_path.read_bytes()
        for change in (
            {"question": "Should I continue?"},
            {"question": "x" * 401},
            {"reason": ""},
            {"category": "made-up"},
            {"blocked_action_field": "scope_id"},
            {"blocked_action_value": ""},
        ):
            args = dict(
                question="Which target?",
                reason="Target required",
                category=AuthorityCategory.MISSING_INPUT,
                blocked_action_field="target",
                blocked_action_value="deployment",
                expected_chain_revision=1,
                expected_session_revision=2,
            )
            args.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.request_decision(**args)
            self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_inspect_read_only_and_content_free(self):
        self.register()
        before = self.storage.registry_path.read_bytes()
        result = self.service.inspect()
        self.assertEqual(result["mode"], "tracked")
        self.assertEqual(result["chain_revision"], 1)
        self.assertNotIn("scope_definition", json.dumps(result))
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_cas_failure_propagates_without_partial_mutation(self):
        before = self.storage.registry_path.read_bytes()
        with patch.object(
            self.storage, "compare_and_swap", side_effect=StaleLifecycleState("changed")
        ):
            with self.assertRaises(StaleLifecycleState):
                self.register()
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_v1_adoption_requires_selected_root_adjacent_approval_and_single_use(self):
        data = make_record("continuation", record_id="unused", root="issue-new")
        data["schema_version"] = 1
        for key in (
            "record_id",
            "authorization_id",
            "authorized_root_scope_id",
            "predecessor",
            "authorization_evidence",
            "transition",
        ):
            data.pop(key)
        for scope in data["active_scopes"]:
            scope.pop("scope_definition")
            scope.pop("scope_definition_digest")
        text = render_record(restore_v1_sections(data))
        selected = RecordReference(
            "legacy-selected",
            (self.root / "selected-v1.md").as_posix(),
            record_digest(text),
        )
        self.proposal(kind="v1-adoption", record=selected)
        with self.assertRaises(ValueError):
            self.service.adopt_v1(
                challenge="challenge-001",
                record_path=selected.path,
                record_text=text,
                expected_session_revision=2,
            )
        approved = self.user_event("yes")
        self.assertEqual(
            approved.session.pending_transition_reference.status, "approved"
        )
        for path, source in (
            (selected.path, text + "changed"),
            ((self.root / "other.md").as_posix(), text),
        ):
            with self.assertRaises(ValueError):
                self.service.adopt_v1(
                    challenge=approved.session.bootstrap_challenge,
                    record_path=path,
                    record_text=source,
                    expected_session_revision=3,
                )
        result = self.service.adopt_v1(
            challenge=approved.session.bootstrap_challenge,
            record_path=selected.path,
            record_text=text,
            expected_session_revision=3,
        )
        self.assertEqual(result.session.mode, EnforcementMode.TRACKED)
        self.assertEqual(result.session.pending_transition_reference.status, "consumed")
        self.assertIn(
            "AHK-V1-SEMANTICS-UNPROVEN", self.service.inspect()["issue_codes"]
        )
        with self.assertRaises(ValueError):
            self.service.adopt_v1(
                challenge=approved.session.bootstrap_challenge,
                record_path=selected.path,
                record_text=text,
                expected_session_revision=4,
            )

    def call_cli(self, arguments, stdin="", session_id="session-1"):
        arguments = list(arguments)
        snapshot = self.storage.load_snapshot(session_id)
        bindings = {
            "--session-key": snapshot.session.session_key,
            "--challenge": self.storage.control_capability(snapshot.session),
        }
        for flag, value in bindings.items():
            if flag not in arguments:
                arguments.extend((flag, value))
            elif arguments[arguments.index(flag) + 1] in {
                "a" * 64,
                "challenge-001",
                snapshot.session.bootstrap_challenge,
            }:
                arguments[arguments.index(flag) + 1] = value
        if "--expected-session-revision" not in arguments:
            arguments.extend(
                ("--expected-session-revision", str(snapshot.session.targeted_revision))
            )
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "AHK_STATE_ROOT": str(self.root / "state"),
                },
            ),
            patch("sys.stdin", io.StringIO(stdin)),
            patch("sys.stdout", output),
            patch("sys.stderr", errors),
        ):
            status = main(["lifecycle", *arguments])
        return status, output.getvalue(), errors.getvalue()

    def test_cli_registration_inspect_and_stdin_decision(self):
        args = command("register-root").split(" lifecycle ")[1].split()
        args[-1] = "1"
        status, output, errors = self.call_cli(args)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output)["mode"], "tracked")
        evidence = json.loads(output)["authorization_evidence"]
        self.assertEqual(evidence["kind"], "initial-user-turn")
        self.assertEqual(evidence["user_turn_ref"], "user-1")
        self.assertEqual(
            evidence["evidence_hmac"],
            self.storage.load_snapshot("session-1").chain.authorization_evidence_hmac,
        )
        self.assertNotIn("outcome", output)
        question = "Which destination " + secrets.token_hex(8) + "?"
        reason = "Destination required " + secrets.token_hex(8)
        status, output, errors = self.call_cli(
            [
                "request-decision",
                "--category",
                "missing-input",
                "--blocked-action-field",
                "target",
                "--blocked-action-value",
                "destination",
                "--reason",
                reason,
                "--expected-chain-revision",
                "1",
                "--expected-session-revision",
                "2",
            ],
            question,
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertIn(f"Decision needed: {question}", output)
        status, output, errors = self.call_cli(["inspect"])
        self.assertEqual(status, 0)
        self.assertNotIn(question, output)
        self.assertNotIn(reason, output)
        self.assertNotIn(question.encode(), self.storage.registry_path.read_bytes())

    def test_cli_input_and_runtime_exit_codes_and_no_raw_diagnostics(self):
        for args in (
            ["register-root"],
            ["adopt-v1", "--confirmed"],
            ["join", "--unknown", "private-marker"],
            ["register-root", "--challenge", "private-marker"],
        ):
            with self.subTest(args=args):
                status, output, errors = self.call_cli(args)
                self.assertEqual(status, 1)
                self.assertNotIn("private-marker", output + errors)
        with patch(
            "agent_handoff_toolkit.lifecycle_operations.LifecycleService.inspect",
            side_effect=LifecycleStorageError("private-marker"),
        ):
            status, output, errors = self.call_cli(["inspect"])
        self.assertEqual(status, 2)
        self.assertNotIn("private-marker", output + errors)

    def test_cli_decision_rejects_oversized_utf8_stdin_without_persisting(self):
        self.register()
        before = self.storage.registry_path.read_bytes()
        status, output, errors = self.call_cli(
            [
                "request-decision",
                "--category",
                "missing-input",
                "--blocked-action-field",
                "target",
                "--blocked-action-value",
                "destination",
                "--reason",
                "Target required",
                "--expected-chain-revision",
                "1",
                "--expected-session-revision",
                "2",
            ],
            "é" * 201,
        )
        self.assertEqual(status, 1)
        self.assertNotIn("é", output + errors)
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_replayed_external_turn_cannot_renew_consumed_challenge(self):
        before = self.storage.registry_path.read_bytes()
        session = self.storage.load_snapshot("session-1").session
        event = NormalizedEvent(
            "codex",
            EventName.USER_PROMPT_SUBMIT,
            session.session_key,
            "user-1",
            self.root.as_posix(),
            "transcript",
            False,
            current_user_reference="user-1",
            external_user_turn=True,
        )
        self.service.observe_user_turn(
            event,
            preceding_assistant_turn_reference=None,
            expected_chain_revision=0,
            expected_session_revision=1,
        )
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_pending_proposal_can_be_represented_but_not_silently_changed(self):
        self.record_chain()
        self.proposal()
        self.user_event("maybe")
        snapshot = self.storage.load_snapshot("session-1")
        pending = snapshot.session.pending_transition_reference
        result = self.service.propose_transition(
            old_scopes=pending.old_scopes,
            new_scopes=pending.new_scopes,
            assistant_turn_reference="assistant-clearer",
            expected_chain_revision=2,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertEqual(
            result.session.pending_transition_reference.assistant_turn_reference,
            "assistant-clearer",
        )
        self.assertIsNone(result.session.pending_transition_reference.evidence_hmac)
        approved = self.user_event("yes", adjacent="assistant-clearer")
        self.assertEqual(approved.chain.publication_evidence.status, "consumed")

    def test_first_record_of_registered_chain_is_accepted_and_installs_reference(self):
        snapshot = self.register()
        data = make_record("continuation", record_id="first-record", root="issue-1323")
        data["authorization_id"] = snapshot.chain.authorization_id
        data["active_scopes"][0]["scope_definition"] = json.loads(
            base64.urlsafe_b64decode(DEFINITION + "==")
        )
        data["active_scopes"][0]["scope_definition_digest"] = (
            snapshot.chain.scope_digests[0]
        )
        data["authorization_evidence"].update(
            user_turn_ref="user-1",
            evidence_hmac=snapshot.chain.authorization_evidence_hmac,
        )
        text = render_record(data)
        path = (self.root / "first.md").as_posix()
        candidate = TerminalCandidate(
            parse_markdown(text),
            text,
            path,
            record_digest(text),
            render_terminal_response(path, text),
            expected_chain_revision=1,
            expected_session_revision=2,
        )
        event = NormalizedEvent(
            "codex",
            EventName.STOP,
            snapshot.session.session_key,
            "assistant-final",
            self.root.as_posix(),
            "transcript",
            False,
            latest_assistant_message=candidate.rendered_response,
        )
        decision = evaluate_stop(event, snapshot, candidate=candidate)
        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(
            decision.mutation.chain.current_record_reference.record_id, "first-record"
        )
        altered = dict(data)
        altered["authorization_evidence"] = dict(
            data["authorization_evidence"], evidence_hmac="0" * 64
        )
        altered_text = render_record(altered)
        altered_response = render_terminal_response(path, altered_text)
        rejected = evaluate_stop(
            replace(event, latest_assistant_message=altered_response),
            snapshot,
            candidate=replace(
                candidate,
                candidate=parse_markdown(altered_text),
                text=altered_text,
                digest=record_digest(altered_text),
                rendered_response=altered_response,
            ),
        )
        self.assertEqual(rejected.kind, DecisionKind.BLOCK)

    def test_cli_unexpected_runtime_failure_is_bounded(self):
        with patch(
            "agent_handoff_toolkit.lifecycle_operations.LifecycleService.inspect",
            side_effect=RuntimeError("private-marker"),
        ):
            status, output, errors = self.call_cli(["inspect"])
        self.assertEqual(status, 2)
        self.assertNotIn("private-marker", output + errors)

    def test_cli_duplicate_equal_form_revision_is_rejected(self):
        args = command("register-root").split(" lifecycle ")[1].split()
        args[-1] = "1"
        before = self.storage.registry_path.read_bytes()
        status, _, _ = self.call_cli([*args, "--expected-session-revision=1"])
        self.assertEqual(status, 1)
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_cli_registration_evidence_excludes_initiating_user_content(self):
        private_content = "Synthetic initiating content " + secrets.token_hex(16)
        snapshot = self.user_event(private_content, adjacent=None)
        args = command("register-root").split(" lifecycle ")[1].split()
        args[args.index("--challenge") + 1] = snapshot.session.bootstrap_challenge
        args[-1] = str(snapshot.session.targeted_revision)
        status, output, errors = self.call_cli(args)
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output)["authorization_evidence"]["user_turn_ref"],
            snapshot.session.current_external_user_turn_reference,
        )
        self.assertNotIn(private_content, output + errors)
        self.assertNotIn(
            private_content.encode(), self.storage.registry_path.read_bytes()
        )

    def test_real_cli_forces_utf8_for_decision_stdin_and_stdout(self):
        self.register()
        snapshot = self.storage.load_snapshot("session-1")
        question = "Which café " + secrets.token_hex(8) + "?"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_handoff_toolkit",
                "lifecycle",
                "request-decision",
                "--session-key",
                snapshot.session.session_key,
                "--challenge",
                self.storage.control_capability(snapshot.session),
                "--category",
                "missing-input",
                "--blocked-action-field",
                "target",
                "--blocked-action-value",
                "destination",
                "--reason",
                "Target required",
                "--expected-chain-revision",
                "1",
                "--expected-session-revision",
                "2",
            ],
            cwd=ROOT,
            input=question.encode("utf-8"),
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONIOENCODING": "cp1252",
                "AHK_STATE_ROOT": str(self.root / "state"),
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(question, result.stdout.decode("utf-8"))
        self.assertEqual(result.stderr, b"")
        self.assertNotIn(
            question.encode("utf-8"), self.storage.registry_path.read_bytes()
        )

    def test_approved_transition_record_publishes_against_exact_old_reference(self):
        reference, previous_text = self.record_chain()
        previous = parse_markdown(previous_text)
        self.seed("session-2")
        LifecycleService(self.storage, "session-2").join(
            challenge="challenge-001",
            authorization_id=previous["authorization_id"],
            expected_chain_revision=2,
            expected_session_revision=1,
        )
        self.proposal()
        snapshot = self.user_event("yes")
        proposal = snapshot.chain.publication_evidence
        before = self.storage.registry_path.read_bytes()
        for changed_proof in (None, replace(proposal, proposal_id="other-proposal")):
            with self.assertRaisesRegex(LifecycleStorageError, "publication evidence"):
                self.storage.compare_and_swap(
                    "session-1",
                    snapshot.chain.targeted_revision,
                    snapshot.session.targeted_revision,
                    LifecycleMutation(
                        replace(
                            snapshot.session,
                            targeted_revision=snapshot.session.targeted_revision + 1,
                            chain_revision=2,
                        ),
                        replace(
                            snapshot.chain,
                            targeted_revision=2,
                            publication_evidence=changed_proof,
                        ),
                    ),
                )
            self.assertEqual(self.storage.registry_path.read_bytes(), before)
        redirected = self.storage.load_snapshot("session-2")
        self.assertEqual(redirected.chain.publication_evidence, proposal)
        self.assertIsNone(redirected.session.pending_transition_reference)
        self.seed("session-3")
        joined = LifecycleService(self.storage, "session-3").join(
            challenge="challenge-001",
            authorization_id=snapshot.chain.authorization_id,
            expected_chain_revision=1,
            expected_session_revision=1,
        )
        self.assertEqual(joined.chain.publication_evidence, proposal)
        self.seed("session-4")
        status, output, errors = self.call_cli(
            [
                "join",
                "--challenge",
                "challenge-001",
                "--authorization-id",
                snapshot.chain.authorization_id,
                "--expected-chain-revision",
                "1",
                "--expected-session-revision",
                "1",
            ],
            session_id="session-4",
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(
            json.loads(output)["publication_evidence"]["transition"]["evidence_hmac"],
            proposal.evidence_hmac,
        )
        pending_new = make_scope(
            "later-root", remaining_work=True, remaining_code=True, status="in-progress"
        )
        snapshot = self.service.propose_transition(
            old_scopes=proposal.new_scopes,
            new_scopes=[pending_new],
            assistant_turn_reference="assistant-later",
            expected_chain_revision=1,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertEqual(snapshot.chain.publication_evidence, proposal)
        self.assertNotEqual(
            snapshot.session.pending_transition_reference.proposal_id,
            proposal.proposal_id,
        )
        before = self.storage.registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "AHK-TRANSITION-PUBLICATION"):
            self.user_event("yes", adjacent="assistant-later")
        self.assertEqual(self.storage.registry_path.read_bytes(), before)
        data = make_record(
            "continuation",
            record_id="transition-record",
            root="issue-new",
            predecessor={
                "record_id": reference.record_id,
                "path": reference.path,
                "sha256": reference.sha256,
            },
        )
        data["authorization_id"] = snapshot.chain.authorization_id
        data["authorization_evidence"] = {
            "kind": "approved-transition",
            "user_turn_ref": proposal.approval_turn_reference,
            "proposal_turn_ref": proposal.assistant_turn_reference,
            "evidence_hmac": proposal.evidence_hmac,
        }
        data["transition"] = {
            "from_authorization_id": previous["authorization_id"],
            "to_authorization_id": snapshot.chain.authorization_id,
            "old_root_scope_id": "issue-1323",
            "new_root_scope_id": "issue-new",
            "proposal_turn_ref": proposal.assistant_turn_reference,
            "approval_turn_ref": proposal.approval_turn_reference,
            "evidence_hmac": proposal.evidence_hmac,
        }
        text = render_record(data)
        path = (self.root / "transition.md").as_posix()
        candidate = TerminalCandidate(
            parse_markdown(text),
            text,
            path,
            record_digest(text),
            render_terminal_response(path, text),
            predecessor=previous,
            predecessor_path=reference.path,
            predecessor_source_digest=reference.sha256,
            trusted_transition_hmac=proposal.evidence_hmac,
            expected_chain_revision=snapshot.chain.targeted_revision,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        event = NormalizedEvent(
            "codex",
            EventName.STOP,
            snapshot.session.session_key,
            "assistant-final",
            self.root.as_posix(),
            "transcript",
            False,
            latest_assistant_message=candidate.rendered_response,
        )
        decision = evaluate_stop(event, snapshot, candidate=candidate)
        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        joined_decision = evaluate_stop(
            replace(event, session_key=joined.session.session_key),
            joined,
            candidate=replace(
                candidate, expected_session_revision=joined.session.targeted_revision
            ),
        )
        self.assertEqual(joined_decision.kind, DecisionKind.ALLOW)
        result = self.storage.compare_and_swap(
            "session-1",
            snapshot.chain.targeted_revision,
            snapshot.session.targeted_revision,
            decision.mutation,
        )
        self.assertEqual(
            result.chain.current_record_reference.record_id, "transition-record"
        )
        self.assertIsNone(result.chain.publication_evidence)
        self.assertEqual(
            result.session.pending_transition_reference.assistant_turn_reference,
            "assistant-later",
        )
        for change in (
            {"predecessor_path": (self.root / "wrong.md").as_posix()},
            {"predecessor_source_digest": "0" * 64},
            {"trusted_transition_hmac": "0" * 64},
        ):
            rejected = evaluate_stop(
                event, snapshot, candidate=replace(candidate, **change)
            )
            self.assertEqual(rejected.kind, DecisionKind.BLOCK)

    def test_model_command_rejects_changed_pending_scope_proposal(self):
        self.record_chain()
        self.proposal()
        current = self.storage.load_snapshot("session-1")
        pending = current.session.pending_transition_reference
        different = make_scope(
            "different", remaining_work=True, remaining_code=True, status="in-progress"
        )
        with self.assertRaises(ValueError):
            self.service.propose_transition(
                old_scopes=pending.old_scopes,
                new_scopes=[different],
                assistant_turn_reference="assistant-2",
                expected_chain_revision=2,
                expected_session_revision=current.session.targeted_revision,
            )

    def test_approval_hmac_binds_successor_authorization_and_old_new_definitions(self):
        self.record_chain()
        self.proposal()
        snapshot = self.user_event("yes")
        proposal = snapshot.chain.publication_evidence
        self.assertEqual(proposal.to_authorization_id, snapshot.chain.authorization_id)
        from agent_handoff_toolkit.lineage import scope_definition_digest

        payload = {
            "kind": "transition",
            "proposal_id": proposal.proposal_id,
            "from_authorization_id": proposal.from_authorization_id,
            "to_authorization_id": proposal.to_authorization_id,
            "proposal_turn_ref": "assistant-1",
            "user_turn_ref": proposal.approval_turn_reference,
            "old_scope_digests": [
                scope_definition_digest(scope) for scope in proposal.old_scopes
            ],
            "scope_digests": list(snapshot.chain.scope_digests),
            "role": "user",
            "adjacent": True,
            "selected_record": None,
        }
        expected = hmac.new(
            self.storage.secret, canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
        self.assertEqual(proposal.evidence_hmac, expected)

    def test_transition_requires_published_initial_record_without_partial_state(self):
        self.register()
        before = self.storage.registry_path.read_bytes()
        with self.assertRaisesRegex(
            ValueError, "AHK-TRANSITION-INITIAL.*publish the initial record first"
        ):
            self.proposal()
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_approval_rejects_legacy_pending_proposal_without_initial_record(self):
        self.record_chain()
        snapshot = self.proposal()
        self.storage.compare_and_swap(
            "session-1",
            snapshot.chain.targeted_revision,
            snapshot.session.targeted_revision,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                    chain_revision=3,
                ),
                replace(
                    snapshot.chain, targeted_revision=3, current_record_reference=None
                ),
            ),
        )
        before = self.storage.registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "AHK-TRANSITION-INITIAL"):
            self.user_event("yes")
        self.assertEqual(self.storage.registry_path.read_bytes(), before)

    def test_cli_transition_before_initial_record_returns_corrective_issue(self):
        self.register()
        old = make_scope(
            "issue-1323", remaining_work=True, remaining_code=True, status="in-progress"
        )
        old["scope_definition"] = json.loads(
            base64.urlsafe_b64decode(DEFINITION + "==")
        )
        new = make_scope(
            "other-root", remaining_work=True, remaining_code=True, status="in-progress"
        )
        encoded = [
            base64.urlsafe_b64encode(canonical_json_bytes([scope])).decode().rstrip("=")
            for scope in (old, new)
        ]
        status, _, errors = self.call_cli(
            [
                "propose-transition",
                "--old-scopes-b64",
                encoded[0],
                "--new-scopes-b64",
                encoded[1],
                "--assistant-turn-reference",
                "assistant-1",
                "--expected-chain-revision",
                "1",
                "--expected-session-revision",
                "2",
            ]
        )
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(errors),
            {
                "issue_codes": ["AHK-TRANSITION-INITIAL"],
                "corrective_action": "publish the initial record first",
            },
        )

    def test_registration_rejects_root_already_active_with_descendants(self):
        root = self.register()
        other = replace(root.chain, scope_digests=(*root.chain.scope_digests, "b" * 64))
        with tempfile.TemporaryDirectory() as folder:
            storage = LocalLifecycleStorage(
                self.root, state_root=Path(folder) / "state"
            )
            session = storage.load_snapshot("existing").session
            storage.compare_and_swap(
                "existing",
                0,
                0,
                LifecycleMutation(
                    replace(
                        session,
                        targeted_revision=1,
                        mode=EnforcementMode.TRACKED,
                        authorization_id=other.authorization_id,
                        chain_revision=1,
                    ),
                    other,
                ),
            )
            fresh = storage.load_snapshot("fresh").session
            storage.compare_and_swap(
                "fresh",
                0,
                0,
                LifecycleMutation(
                    replace(
                        fresh,
                        targeted_revision=1,
                        current_external_user_turn_reference="user-1",
                        bootstrap_challenge="challenge-001",
                    )
                ),
            )
            before = storage.registry_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "join"):
                self.register(LifecycleService(storage, "fresh"))
            self.assertEqual(storage.registry_path.read_bytes(), before)

    def test_v1_adoption_rejects_scope_kind_not_named_by_proposal(self):
        data = make_record("continuation", record_id="unused", root="issue-new")
        data["schema_version"] = 1
        for key in (
            "record_id",
            "authorization_id",
            "authorized_root_scope_id",
            "predecessor",
            "authorization_evidence",
            "transition",
        ):
            data.pop(key)
        for scope in data["active_scopes"]:
            scope.pop("scope_definition")
            scope.pop("scope_definition_digest")
            scope["scope_kind"] = "rollout"
        text = render_record(restore_v1_sections(data))
        selected = RecordReference(
            "legacy-selected",
            (self.root / "selected.md").as_posix(),
            record_digest(text),
        )
        self.proposal(kind="v1-adoption", record=selected)
        approved = self.user_event("yes")
        before = self.storage.registry_path.read_bytes()
        with self.assertRaises(ValueError):
            self.service.adopt_v1(
                challenge=approved.session.bootstrap_challenge,
                record_path=selected.path,
                record_text=text,
                expected_session_revision=approved.session.targeted_revision,
            )
        self.assertEqual(self.storage.registry_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
