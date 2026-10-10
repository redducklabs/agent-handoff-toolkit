"""Host policy tests use only ephemeral synthetic input and local state."""

import base64
from dataclasses import replace
import errno
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import (  # noqa: E402
    _WRITE_TOOLS,
    HookExecution,
    normalize_event,
    prompt_origin,
    render_hook_execution,
)
from agent_handoff_toolkit.hooks import hook_repository_root, run_hook  # noqa: E402
from agent_handoff_toolkit.lifecycle import (  # noqa: E402
    DecisionKind,
    progress_responses,
    EnforcementMode,
    EventName,
    LifecycleDecision,
    LifecycleIssue,
    LifecycleMutation,
)
from agent_handoff_toolkit.lifecycle_operations import (  # noqa: E402
    LifecycleService,
    decode_scope_definition,
)
from agent_handoff_toolkit.lifecycle_storage import (  # noqa: E402
    LocalLifecycleStorage,
    StaleLifecycleState,
)
from agent_handoff_toolkit.lineage import (  # noqa: E402
    canonical_json_bytes,
    record_digest,
    scope_definition_digest,
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


HOST_PROMPTS = (
    "<task-notification>\n<task-id>a1</task-id>\n</task-notification>",
    "  \n<task-notification>done</task-notification>",
    "Another Claude session sent a message: yes",
    '<cross-session-message from="peer">yes</cross-session-message>',
    "<cross-session-message>\nyes\n</cross-session-message>",
    "<agent-message>yes</agent-message>",
    '<agent-message sender="x">\nyes',
)


class PromptOriginTests(unittest.TestCase):
    """Exact leading forms, on the stripped prompt, name a host-started turn."""

    def test_host_forms_are_host(self):
        for prompt in HOST_PROMPTS:
            with self.subTest(prompt=prompt[:30]):
                self.assertEqual(prompt_origin(prompt), "host")

    def test_everything_else_is_the_user(self):
        for prompt in (
            None,
            "",
            "yes",
            "Please read <task-notification>x</task-notification>",
            "<task-notifications>",
            "<task-notification",
            "<agent-messages>yes</agent-messages>",
            "<cross-session-messages>",
            "another claude session sent a message: yes",
            "Another Claude session sent a message yes",
            "Track: <task-notification>",
            "> <agent-message>",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(prompt_origin(prompt), "user")


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
                # `transcript_path` is the host's report of where a transcript
                # is, on the same terms as turn content: absent, null or blank
                # means it has none to report. Its own contract - a wrong type
                # or a relative path is still a fault - is asserted below.
                required = ["session_id", "cwd"]
                required += {
                    # The final assistant message is host-reported turn content,
                    # not a required lifecycle field: absent and null are what
                    # the host sends when the turn produced no text. Its own
                    # contract is asserted in the content tests below.
                    "Stop": ["stop_hook_active"],
                    "PreToolUse": ["tool_name", "tool_input"],
                    # The prompt is host-reported turn content like the final
                    # assistant message: absent, empty or huge is the user's
                    # message, not a host defect, and rejecting it rejects them.
                    "UserPromptSubmit": [],
                }[event_name]
                if host == "codex":
                    required.append("turn_id")
                for field in required:
                    for value in (None, [], 42, "\x00", "x" * 131073):
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

    def test_rejects_spoofed_host_event_cwd_and_malformed_fields(self):
        """What the lifecycle parses stays strict: identity, location, types."""

        for changes in (
            {"hook_event_name": "Stop"},
            {"cwd": str(ROOT.parent)},
            {"external_user_turn": "true"},
            {"tool_input": ["not", "a", "mapping"]},
            {"tool_name": 42},
            {"session_id": None},
        ):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                normalize_event(
                    "codex", "PreToolUse", payload(ROOT, "PreToolUse", **changes), ROOT
                )
        with self.assertRaises(ValueError):
            normalize_event("CODEX", "Stop", payload(ROOT), ROOT)

    def test_accepts_the_payload_the_work_actually_carries(self):
        """Size, shape and characters of tool input belong to the tool call.

        Each of these was rejected, which blocked the write rather than proving
        anything: no host content reaches hook output, and in a tracked session
        the hook never reads tool input beyond the shell command.
        """

        for label, changes in (
            ("a long array", {"tool_input": {"edits": ["x"] * 300}}),
            ("a long command", {"tool_input": {"command": "x" * 4097}}),
            ("a tab in an unrelated field", {"extra": "\t"}),
            ("a line separator", {"extra": "\u2028"}),
            ("tab-indented content", {"tool_input": {"content": "a:\n\tb\n"}}),
            ("a 200 KB file", {"tool_input": {"content": "x" * 200_000}}),
        ):
            with self.subTest(label=label):
                event = normalize_event(
                    "codex", "PreToolUse", payload(ROOT, "PreToolUse", **changes), ROOT
                )
                self.assertIsNotNone(event.tool_input)

    def test_source_line_endings_normalize_without_trimming_message(self):
        event = normalize_event(
            "codex",
            "Stop",
            payload(ROOT, last_assistant_message="First\r\nSecond"),
            ROOT,
        )
        self.assertEqual(event.latest_assistant_message, "First\nSecond")
        # Untrimmed prose is what a turn may actually end with, so it is read
        # rather than raised on, and kept verbatim so the later comparison
        # against the rendered response is made on exactly what was emitted.
        for value in (" trailing ", "\nleading", "x" * 20000, "a\tb"):
            with self.subTest(value=value[:12]):
                event = normalize_event(
                    "codex", "Stop", payload(ROOT, last_assistant_message=value), ROOT
                )
                self.assertEqual(event.latest_assistant_message, value)
        # Text carrying no content at all reads as absent.
        for value in ("", "   \n"):
            with self.subTest(value=repr(value)):
                self.assertIsNone(
                    normalize_event(
                        "codex",
                        "Stop",
                        payload(ROOT, last_assistant_message=value),
                        ROOT,
                    ).latest_assistant_message
                )
        self.assertIsNone(
            normalize_event(
                "codex", "Stop", payload(ROOT, last_assistant_message=None), ROOT
            ).latest_assistant_message
        )
        absent = payload(ROOT, "Stop")
        del absent["last_assistant_message"]
        self.assertIsNone(
            normalize_event("codex", "Stop", absent, ROOT).latest_assistant_message
        )
        # A wrong type is a host defect rather than turn content, and stays fatal.
        for value in ([], 42, {"a": 1}):
            with self.subTest(value=str(value)[:12]), self.assertRaises(ValueError):
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

    def test_no_prompt_decision_of_any_kind_reaches_a_host(self):
        """The renderer is the chokepoint, so it must hold for every kind.

        `POLICY_FAILURE` is reachable only from `Stop` today, but the renderer
        does not restrict it by event, and an invariant that depends on no
        caller ever changing is not an invariant.
        """

        issue = LifecycleIssue(
            "AHK-STOP-WORK", "Work remains.", "Continue authorized work."
        )
        for host in ("claude", "codex"):
            for kind in (DecisionKind.BLOCK, DecisionKind.POLICY_FAILURE):
                with self.subTest(host=host, kind=kind):
                    output = render_hook_execution(
                        host,
                        EventName.USER_PROMPT_SUBMIT,
                        LifecycleDecision(kind, (issue,)),
                    )
                    self.assertEqual(output.exit_code, 0)
                    self.assertEqual(output.stderr, "")
                    data = json.loads(output.stdout)
                    self.assertNotIn("decision", data)
                    self.assertNotIn("continue", data)
                    self.assertEqual(
                        data["hookSpecificOutput"]["hookEventName"],
                        "UserPromptSubmit",
                    )
                    self.assertIn(
                        "AHK-STOP-WORK",
                        data["hookSpecificOutput"]["additionalContext"],
                    )

    def test_detail_codes_never_cost_an_issue_its_line(self):
        """Detail is additional; it never displaces a code or its action."""

        codes = ("AHK-STOP-ROOT", "AHK-STOP-PREDECESSOR", "AHK-STOP-SCOPE")
        long_details = tuple(f"detail-{index}-{'d' * 100}" for index in range(8))
        issues = tuple(
            LifecycleIssue(
                code,
                "Summary.",
                "Corrective action.",
                detail_codes=long_details,
            )
            for code in codes
        )
        reason = json.loads(
            render_hook_execution(
                "codex", EventName.STOP, LifecycleDecision(DecisionKind.BLOCK, issues)
            ).stdout
        )["reason"]
        self.assertLessEqual(len(reason.encode()), 1200)
        for code in codes:
            self.assertIn(code, reason)
        self.assertNotIn("AHK-HOOK-RUNTIME", reason)

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

    def test_a_refused_pointer_names_its_root_only_as_a_bounded_path(self):
        def reason(root_path):
            issue = LifecycleIssue(
                "AHK-STOP-POINTER",
                "Summary.",
                "Corrective action.",
                detail_codes=("pointer-outside-handoffs",),
                root_path=root_path,
            )
            return json.loads(
                render_hook_execution(
                    "codex",
                    EventName.STOP,
                    LifecycleDecision(DecisionKind.BLOCK, (issue,)),
                ).stdout
            )["reason"]

        named = reason("D:/repo/handoffs")
        self.assertIn("AHK-STOP-POINTER", named)
        self.assertIn(" root=D:/repo/handoffs", named)
        self.assertIn("failed=pointer-outside-handoffs", named)
        for unsafe in ("D:/my repo/handoffs", "relative/handoffs", "/" + "x" * 300):
            with self.subTest(unsafe=unsafe[:20]):
                self.assertNotIn(" root=", reason(unsafe))

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
        # Both root resolvers walk ancestors, so an unrelated repository above
        # the platform temporary directory could otherwise claim this consumer.
        # The ceiling fences `git rev-parse`; the marker fences the installer's
        # pure-Python `.git` walk, which an empty directory satisfies. Git
        # ignores a ceiling that is not a strict ancestor of the directory being
        # resolved, and resolution here starts at the root itself, so the
        # ceiling is its parent.
        fence = patch.dict(
            os.environ, {"GIT_CEILING_DIRECTORIES": self.root.parent.as_posix()}
        )
        fence.start()
        self.addCleanup(fence.stop)
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        self.service = LifecycleService(self.storage, "session-1")
        (self.root / "handoffs").mkdir()
        (self.root / ".agent-handoff-toolkit").mkdir()
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

    def assert_runtime_notice(self, execution):
        self.assertEqual(execution.exit_code, 0)
        self.assertEqual(execution.stderr, "")
        data = json.loads(execution.stdout)
        self.assertIn("AHK-HOOK-RUNTIME", data["systemMessage"])
        self.assertNotIn("decision", data)
        self.assertNotIn("continue", data)
        self.assertNotIn("permissionDecision", data.get("hookSpecificOutput", {}))

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
        # This test asserts that mutation tools pass through untouched; the
        # first-write advisory (Task 4) is a separate, one-time concern
        # exercised in tests/test_enforcement_scope.py. Silence it here so it
        # does not shadow the assertions below or shift session revisions
        # that later assertions in this test hard-code.
        write_tools_patch = patch.dict(
            _WRITE_TOOLS, {"claude": frozenset(), "codex": frozenset()}
        )
        write_tools_patch.start()
        self.addCleanup(write_tools_patch.stop)
        for host in ("claude", "codex"):
            for tool in ("Bash", "Write", "apply_patch", "mcp__fs__read", "unknown"):
                self.assertEqual(
                    self.invoke("PreToolUse", host=host, tool_name=tool),
                    HookExecution(),
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
                command.replace(
                    f"--expected-session-revision {session.targeted_revision}",
                    "--expected-session-revision 0",
                ),
            ):
                self.assert_block(
                    self.invoke(
                        "PreToolUse", host=host, tool_input={"command": malicious}
                    ),
                    "AHK-PRE-ROOT",
                )
            feedback = json.loads(
                self.invoke(
                    "PreToolUse",
                    host=host,
                    tool_input={"command": f"python {runner} lifecycle"},
                ).stdout
            )["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("--challenge " + capability, feedback)
            self.assertIn("--expected-session-revision 1", feedback)
            self.assertEqual(feedback.count("Command: "), 1)
            self.assertIn("lifecycle register-root --session-key", feedback)
        (self.root / ".agent-handoff-toolkit" / "runner.py").unlink()
        self.assert_runtime_notice(
            self.invoke("PreToolUse", tool_input={"command": command}),
        )

    def test_the_read_only_diagnosis_is_never_intercepted(self):
        """The recovery command must not need what a broken hook cannot issue.

        The control interception exists to carry a session key, challenge and
        revision into commands that need them. `doctor` needs none: it decides
        nothing and changes nothing. Denying it would reproduce the deadlock it
        exists to break - the only path to a challenge running through the hook
        that is failing.
        """

        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        for command in (
            f"python {runner} lifecycle doctor",
            f"python {runner} lifecycle doctor --session-id abc",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.invoke("PreToolUse", tool_input={"command": command}),
                    HookExecution(),
                )
        # Everything that does take a session binding is still intercepted.
        self.assert_block(
            self.invoke(
                "PreToolUse", tool_input={"command": f"python {runner} lifecycle"}
            ),
            "AHK-PRE-ROOT",
        )
        self.assert_block(
            self.invoke(
                "PreToolUse",
                tool_input={"command": f"python {runner} lifecycle doctor-evil"},
            ),
            "AHK-PRE-ROOT",
        )

    def test_control_command_before_any_user_turn_still_requires_root(self):
        """AHK-PRE-ROOT still guards the control channel before any user turn.

        A brand-new session has never observed an external user turn, so the
        session key and challenge a control command needs have never been
        disclosed to it. Now that ordinary work is never gated, this is the
        only remaining path that yields AHK-PRE-ROOT.
        """

        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        command = f"python {runner} lifecycle inspect"
        self.assert_block(
            self.invoke("PreToolUse", tool_input={"command": command}), "AHK-PRE-ROOT"
        )

    def test_hook_roots_at_the_checkout_named_by_the_payload_cwd(self):
        """The session's directory names the checkout, not the hook process's.

        Desktop worktrees live inside the main checkout, at
        `<repo>/.claude/worktrees/<name>`, and the host does not promise to
        start the hook process in the session's directory. Rooting at the
        process's directory passed the containment check and then bound every
        command to the main checkout's runner and `handoffs/`, so worktree
        records read as outside `handoffs/` and bound commands went stale.
        """

        worktree = self.root / ".claude" / "worktrees" / "x"
        (worktree / "handoffs").mkdir(parents=True)
        (worktree / ".agent-handoff-toolkit").mkdir()
        # The layout `git worktree add` writes: a `.git` file naming a
        # per-worktree directory whose `commondir` leads back to the shared one.
        private = self.root / ".git" / "worktrees" / "x"
        private.mkdir(parents=True)
        (private / "commondir").write_text("../..\n")
        (private / "gitdir").write_text(f"{(worktree / '.git').as_posix()}\n")
        (worktree / ".git").write_text(f"gitdir: {private.as_posix()}\n")
        runner = worktree / ".agent-handoff-toolkit" / "runner.py"
        runner.write_text("# owned runner\n")

        def invoke(name, **changes):
            return run_hook(
                "claude",
                name,
                json.dumps(payload(worktree, name, **changes)),
                self.root,
                self.storage,
            )

        self.assertEqual(invoke("UserPromptSubmit"), HookExecution())
        stale = f"python {runner.as_posix()} lifecycle inspect"
        specific = json.loads(
            invoke("PreToolUse", tool_input={"command": stale}).stdout
        )["hookSpecificOutput"]
        command = specific["updatedInput"]["command"]
        self.assertEqual(command.split()[1], runner.as_posix())

    def test_hook_never_roots_at_an_unrelated_checkout(self):
        """Only a checkout of the same repository can take the root.

        The payload `cwd` is the session's directory, and a session can stand
        in any clone on the machine. Rooting there would bind commands to that
        clone's runner, so a checkout that does not share this repository's
        git common directory leaves the process-derived root in place.
        """

        with tempfile.TemporaryDirectory() as directory:
            unrelated = Path(directory).resolve()
            (unrelated / ".git").mkdir()
            nested = unrelated / "src"
            nested.mkdir()
            for cwd in (unrelated, nested):
                with self.subTest(cwd=cwd.name):
                    self.assertEqual(
                        hook_repository_root({"cwd": str(cwd)}, self.root), self.root
                    )
            (unrelated / ".git").rmdir()
            (unrelated / ".git").write_text("gitdir: elsewhere\n")
            self.assertEqual(
                hook_repository_root({"cwd": str(unrelated)}, self.root), self.root
            )

    def test_a_hand_written_git_file_is_not_a_worktree_of_this_repository(self):
        """`gitdir: ../../.git` names the common directory without being a worktree.

        Any directory can carry such a file. A `.git` file counts only when
        its private directory sits directly under `<common>/worktrees/` and
        that directory's `gitdir` leads back to this checkout's `.git`.
        """

        spoof = self.root / "a" / "b"
        spoof.mkdir(parents=True)
        (spoof / ".git").write_text("gitdir: ../../.git\n")
        self.assertEqual(
            hook_repository_root({"cwd": str(spoof)}, self.root), self.root
        )
        # A private directory in the right place whose `gitdir` names another
        # checkout is not this one's either.
        private = self.root / ".git" / "worktrees" / "other"
        private.mkdir(parents=True)
        (private / "commondir").write_text("../..\n")
        elsewhere = (self.root / "elsewhere" / ".git").as_posix()
        (private / "gitdir").write_text(elsewhere + "\n")
        (spoof / ".git").write_text(f"gitdir: {private.as_posix()}\n")
        self.assertEqual(
            hook_repository_root({"cwd": str(spoof)}, self.root), self.root
        )

    def test_a_genuine_git_worktree_takes_the_root(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory).resolve()
            main = scratch / "main"
            main.mkdir()
            environment = {**os.environ, "GIT_CEILING_DIRECTORIES": scratch.as_posix()}
            for arguments in (
                ("init", "-q"),
                ("commit", "-q", "--allow-empty", "-m", "synthetic"),
                ("worktree", "add", "-q", ".claude/worktrees/x"),
            ):
                subprocess.run(
                    [
                        "git",
                        "-c",
                        "user.name=Synthetic",
                        "-c",
                        "user.email=synthetic@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        *arguments,
                    ],
                    cwd=main,
                    env=environment,
                    capture_output=True,
                    check=True,
                )
            worktree = main / ".claude" / "worktrees" / "x"
            nested = worktree / "src"
            nested.mkdir()
            for cwd in (worktree, nested):
                with self.subTest(cwd=cwd.name):
                    self.assertEqual(
                        hook_repository_root({"cwd": str(cwd)}, main), worktree
                    )

    def stop_payload(self, drop=(), **changes):
        value = payload(self.root, "Stop", **changes)
        for key in drop:
            value.pop(key, None)
        return run_hook("codex", "Stop", json.dumps(value), self.root, self.storage)

    def test_stop_tolerates_every_ordinary_host_final_message(self):
        """The host reports what the turn produced; none of it is a runtime fault.

        A turn that ends on a denied tool call has no final assistant text at
        all, and a turn that ends on prose carries whatever spacing the model
        emitted. Treating either as a lifecycle runtime error blocks the only
        hook that can end the turn.
        """

        self.invoke("UserPromptSubmit")
        for label, message in (
            ("turn ended on a tool call", None),
            ("empty final text", ""),
            ("whitespace only", "   \n"),
            ("trailing newline", "Done.\n"),
            ("leading space", " Done."),
            ("oversized", "x" * 17000),
        ):
            with self.subTest(label=label):
                output = self.stop_payload(last_assistant_message=message)
                self.assertNotIn("AHK-HOOK-RUNTIME", output.stdout)
        self.assertNotIn(
            "AHK-HOOK-RUNTIME",
            self.stop_payload(drop=("last_assistant_message",)).stdout,
        )

    def test_unusable_final_message_still_fails_closed_once_tracked(self):
        """Tolerating the value must not turn a tracked stop into an allow."""

        self.register()
        for label, message in (
            ("absent text", None),
            ("empty text", ""),
            ("oversized text", "x" * 17000),
        ):
            with self.subTest(label=label):
                self.assert_block(
                    self.stop_payload(last_assistant_message=message), "AHK-STOP-WORK"
                )
                # A real user turn clears the correction count, so each case is
                # measured on its own rather than against the armed circuit.
                self.invoke("UserPromptSubmit", turn_id="turn-" + label.split()[0])

    def test_runtime_fault_never_blocks_a_session_that_declared_nothing(self):
        """A malfunction is not a policy decision, and must not end the session.

        A session that has registered no root is ungated by construction, so a
        toolkit fault at `Stop` has no policy to enforce and nothing to protect.
        Blocking there wedged consumer repositories: the turn could not end, and
        the correction circuit was the only way out. The fault is now reported
        on the advisory channel, which carries no decision at all.
        """

        self.invoke("UserPromptSubmit")
        broken = payload(self.root, "Stop")
        broken["stop_hook_active"] = "not-a-boolean"
        outputs = [
            run_hook("codex", "Stop", json.dumps(broken), self.root, self.storage)
            for _ in range(3)
        ]
        for output in outputs:
            self.assertEqual(output.exit_code, 0)
            self.assertEqual(output.stderr, "")
            data = json.loads(output.stdout)
            self.assertNotIn("decision", data)
            self.assertNotIn("hookSpecificOutput", data)
            self.assertNotIn("continue", data)
            self.assertIn("AHK-HOOK-RUNTIME", data["systemMessage"])
        # Nothing blocked, so nothing counted toward the circuit.
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 0
        )

    def test_runtime_fault_never_blocks_tracked_work_or_arms_the_circuit(self):
        """A malfunction cannot enforce policy or consume correction attempts."""

        self.register()
        broken = payload(self.root, "Stop")
        broken["stop_hook_active"] = "not-a-boolean"
        for attempt in (1, 2):
            output = run_hook(
                "codex", "Stop", json.dumps(broken), self.root, self.storage
            )
            data = json.loads(output.stdout)
            self.assertIn("AHK-HOOK-RUNTIME", data["systemMessage"])
            self.assertNotIn("decision", data)
            self.assertNotIn("hookSpecificOutput", data)
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.correction_cycle_count,
                0,
            )

    def test_runtime_fault_names_its_stage_and_exception(self):
        """A blocking generic string is unactionable by design.

        Every other denial names the checks that failed after `failed=`. This
        one discarded the exception entirely, so a consumer had no way to tell
        a malformed payload from unreachable state.
        """

        self.register()
        broken = payload(self.root, "Stop")
        broken["stop_hook_active"] = "not-a-boolean"
        reason = json.loads(
            run_hook(
                "codex", "Stop", json.dumps(broken), self.root, self.storage
            ).stdout
        )["systemMessage"]
        self.assertIn("failed=", reason)
        details = reason.split("failed=")[1].split()[0].split(",")
        self.assertIn("stage:normalize-event", details)
        self.assertIn("error:ValueError", details)
        self.assertLessEqual(len(reason.encode()), 1200)

    def test_tracked_evaluation_and_commit_faults_are_advisory_on_both_hosts(self):
        self.register()
        before = self.storage.load_snapshot("session-1")
        _, _, message = self.record()
        for host in ("claude", "codex"):
            with self.subTest(host=host, stage="evaluate"):
                with patch(
                    "agent_handoff_toolkit.hook_adapters.evaluate_stop",
                    side_effect=RuntimeError("sensitive synthetic fault"),
                ):
                    output = self.invoke(host=host)
                self.assert_runtime_notice(output)
                self.assertIn("stage:evaluate,error:RuntimeError", output.stdout)
                self.assertNotIn("sensitive", output.stdout)
            with self.subTest(host=host, stage="commit-state"):
                with patch.object(
                    self.storage,
                    "compare_and_swap",
                    side_effect=OSError(errno.EACCES, "sensitive synthetic fault"),
                ):
                    output = self.invoke(host=host, last_assistant_message=message)
                self.assert_runtime_notice(output)
                self.assertIn("stage:commit-state,error:PermissionError", output.stdout)
                self.assertNotIn("sensitive", output.stdout)
            after = self.storage.load_snapshot("session-1")
            self.assertEqual(after, before)

    def test_stale_republish_fault_cannot_block_tracked_work(self):
        self.register()
        with (
            patch.object(
                self.storage, "compare_and_swap", side_effect=StaleLifecycleState()
            ),
            patch(
                "agent_handoff_toolkit.hook_adapters._peer_advanced",
                side_effect=[False, RuntimeError("fault")],
            ),
        ):
            output = self.invoke()
        self.assert_runtime_notice(output)
        self.assertIn("stage:republish-stale,error:RuntimeError", output.stdout)
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 0
        )

    def test_a_held_lock_is_a_named_runtime_fault_not_a_hang(self):
        """A hung lock holder used to stall every hook until the host timeout."""

        from agent_handoff_toolkit import lifecycle_storage

        self.register()
        bounded = LocalLifecycleStorage(
            self.root, state_root=self.root / "state", lock_timeout=0.3
        )
        windows = os.name == "nt"
        held = lifecycle_storage._open_private(
            self.root / "state" / "registry.lock", os.O_RDWR | os.O_CREAT
        )
        lifecycle_storage._lock_descriptor(held, windows=windows)
        try:
            for name in ("Stop", "PreToolUse"):
                with self.subTest(event=name):
                    output = run_hook(
                        "claude",
                        name,
                        json.dumps(
                            payload(
                                self.root,
                                name,
                                tool_name="Write",
                                tool_input={"file_path": "x"},
                            )
                            if name == "PreToolUse"
                            else payload(self.root, name)
                        ),
                        self.root,
                        bounded,
                    )
                    self.assertEqual(output.exit_code, 0)
                    data = json.loads(output.stdout)
                    # The lock kept the mode from being read, so nothing is
                    # decided; the fault is named on the advisory channel.
                    self.assertNotIn("decision", data)
                    self.assertNotIn("hookSpecificOutput", data)
                    self.assertIn(
                        "failed=stage:load-state,error:LockTimeout",
                        data["systemMessage"],
                    )
        finally:
            lifecycle_storage._unlock_descriptor(held, windows=windows)
            os.close(held)

    def test_one_hook_run_shares_one_lock_deadline(self):
        """A Stop took the lock up to four times, each waiting up to 15 s.

        Every acquisition in one hook invocation now waits against the same
        absolute deadline, 15 s from the start of the run.
        """

        from agent_handoff_toolkit import lifecycle_storage

        self.register()
        original = lifecycle_storage._exclusive_lock
        deadlines = []

        def recording(path, timeout=None, deadline=None):
            deadlines.append((timeout, deadline))
            return original(path, timeout, deadline)

        started = time.monotonic()
        with (
            patch.dict(os.environ, {"AHK_STATE_ROOT": str(self.root / "state")}),
            patch.object(lifecycle_storage, "_exclusive_lock", recording),
        ):
            for name in ("Stop", "UserPromptSubmit"):
                with self.subTest(event=name):
                    deadlines.clear()
                    run_hook(
                        "codex",
                        name,
                        json.dumps(payload(self.root, name, turn_id="t-" + name)),
                        self.root,
                    )
                    self.assertGreater(len(deadlines), 1)
                    self.assertEqual(len(set(deadlines)), 1)
                    timeout, deadline = deadlines[0]
                    self.assertIsNone(timeout)
                    self.assertLessEqual(
                        deadline,
                        time.monotonic() + lifecycle_storage.LOCK_TIMEOUT_SECONDS,
                    )
                    self.assertGreaterEqual(
                        deadline, started + lifecycle_storage.LOCK_TIMEOUT_SECONDS
                    )

    def test_unreadable_state_is_reported_rather_than_enforced(self):
        """The mode is unknown when state cannot be read, so nothing is decided.

        Windows lock contention reached here as an `OSError` and was rendered as
        a block. A session that may never have declared anything must not be
        stopped by a fault in reading state that belongs to the toolkit.
        """

        with patch.object(
            LocalLifecycleStorage,
            "load_snapshot",
            side_effect=OSError(errno.EACCES, "Permission denied"),
        ):
            output = self.invoke("Stop")
        data = json.loads(output.stdout)
        self.assertEqual(output.exit_code, 0)
        self.assertNotIn("decision", data)
        self.assertIn("stage:load-state", data["systemMessage"])
        # OSError resolves to its concrete subclass, which is the useful name.
        self.assertIn("error:PermissionError", data["systemMessage"])
        self.assertIn("errno:13", data["systemMessage"])

    def test_runtime_fault_on_a_tool_call_returns_no_permission_decision(self):
        """A denied write or control command removes a session's way out.

        An ordinary call no longer reaches this path at all: nothing is
        decided for it, so no state is opened for it. The two calls that do
        reach it still report a fault with no decision attached.
        """

        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        for label, host, tool, tool_input in (
            ("write", "claude", "Write", {"file_path": str(self.root / "a.py")}),
            (
                "control",
                "claude",
                "Bash",
                {"command": f"python {runner} lifecycle inspect"},
            ),
        ):
            with self.subTest(label=label):
                with patch.object(
                    LocalLifecycleStorage,
                    "load_snapshot",
                    side_effect=OSError(errno.EACCES, "Permission denied"),
                ):
                    output = self.invoke(
                        "PreToolUse", host=host, tool_name=tool, tool_input=tool_input
                    )
                data = json.loads(output.stdout)
                self.assertEqual(output.exit_code, 0)
                self.assertNotIn("hookSpecificOutput", data)
                self.assertIn("AHK-HOOK-RUNTIME", data["systemMessage"])

    def test_absent_null_or_blank_transcript_reference_is_not_a_fault(self):
        """The host reports where the transcript is; having none is not an error.

        A `Stop` payload carrying `transcript_path: ""` blocked every turn with
        no detail. The reference is recorded metadata that no check consumes, so
        an empty report reads as absent. Only a wrong type remains a fault.
        """

        self.invoke("UserPromptSubmit")
        for label, value in (("null", None), ("empty", ""), ("blank", "   ")):
            with self.subTest(label=label):
                output = self.stop_payload(transcript_path=value)
                self.assertNotIn("AHK-HOOK-RUNTIME", output.stdout)
        self.assertNotIn(
            "AHK-HOOK-RUNTIME", self.stop_payload(drop=("transcript_path",)).stdout
        )
        for label, value in (("wrong type", 7), ("relative", "handoffs/t.jsonl")):
            with self.subTest(label=label):
                self.assertIn(
                    "AHK-HOOK-RUNTIME", self.stop_payload(transcript_path=value).stdout
                )

    def test_bootstrap_rejection_names_the_failed_check(self):
        """Every rejection cause was one indistinguishable string."""

        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        definition = {"title": "Synthetic scope", "outcome": "A synthetic outcome"}
        canonical = canonical_json_bytes(definition)
        encoded = base64.urlsafe_b64encode(canonical).decode().rstrip("=")

        def command(value, challenge=capability):
            return (
                f"python {runner} lifecycle register-root"
                f" --session-key {session.session_key} --challenge {challenge}"
                f" --scope-id issue-1 --scope-kind issue"
                f" --scope-definition-b64 {value}"
                f" --expected-session-revision {session.targeted_revision}"
            )

        # Padding is the usual first hand-encoding attempt and is rejected by the
        # command charset before any definition check, so it gets its own code.
        padded = json.loads(
            self.invoke(
                "PreToolUse",
                tool_input={"command": command(base64.b64encode(canonical).decode())},
            ).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("failed=command-charset-padding", padded)
        # An ordinary tool call is never gated, so it carries no bootstrap
        # check at all.
        self.assertEqual(
            self.invoke("PreToolUse", tool_input={"command": "git status"}),
            HookExecution(),
        )
        cases = {
            "non-canonical json": base64.urlsafe_b64encode(
                json.dumps(definition, sort_keys=True).encode()
            )
            .decode()
            .rstrip("="),
            "unexpected field": base64.urlsafe_b64encode(
                canonical_json_bytes({**definition, "extra": "x"})
            )
            .decode()
            .rstrip("="),
        }
        observed = set()
        for label, value in cases.items():
            with self.subTest(label=label):
                output = self.invoke(
                    "PreToolUse", tool_input={"command": command(value)}
                )
                reason = json.loads(output.stdout)["hookSpecificOutput"][
                    "permissionDecisionReason"
                ]
                self.assertIn("failed=", reason)
                observed.add(reason.split("failed=")[1].split()[0])
        self.assertEqual(len(observed), len(cases))
        # A cause outside the definition itself gets its own code too.
        kind = self.invoke(
            "PreToolUse",
            tool_input={
                "command": command(encoded).replace(
                    "--scope-kind issue", "--scope-kind saga"
                )
            },
        )
        kind_reason = json.loads(kind.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        self.assertIn("failed=scope-kind", kind_reason)
        self.assertNotIn("scope-kind", observed)
        # A stale challenge is repairable, so it is corrected rather than named:
        # the denial echoes the real payload back with only the nonce replaced.
        stale_reason = json.loads(
            self.invoke(
                "PreToolUse",
                tool_input={"command": command(encoded, "expired-challenge")},
            ).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertNotIn("failed=", stale_reason)
        self.assertIn("--challenge " + capability, stale_reason)
        self.assertIn("--scope-definition-b64 " + encoded, stale_reason)

    def test_pre_root_register_root_attempt_is_encoded_by_the_hook(self):
        """The bound command's only machine-computable field must have a machine.

        A pre-root session has no shell, so it cannot canonicalize and encode a
        scope definition. The denial accepts the semantic slots as plain text
        and returns the formed command; the strict parser still gates what runs.
        """

        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        attempt = (
            f"python {runner} lifecycle register-root --scope-id issue-1"
            ' --scope-kind issue --scope-title "Repair the bootstrap deadlock"'
            ' --scope-outcome "A fresh session registers its own root, unaided."'
        )
        reason = json.loads(
            self.invoke("PreToolUse", tool_input={"command": attempt}).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("AHK-PRE-ROOT", reason)
        # The strict parse of the plain-slot attempt fails by design; reporting
        # it beside a successful encoding would name a failure that never was.
        self.assertNotIn("failed=", reason)
        formed = reason.split("Command: ", 1)[1].strip()
        self.assertIn("--scope-definition-b64", formed)
        self.assertNotIn("--scope-title", formed)
        self.assertNotIn("{scope_definition_b64}", formed)
        # The returned command is accepted verbatim by the strict parser.
        self.assertEqual(
            self.invoke("PreToolUse", tool_input={"command": formed}), HookExecution()
        )
        encoded = formed.split("--scope-definition-b64 ")[1].split(" ")[0]
        self.assertEqual(
            decode_scope_definition(encoded),
            {
                "title": "Repair the bootstrap deadlock",
                "outcome": "A fresh session registers its own root, unaided.",
            },
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

    def test_runtime_input_corruption_is_advisory_and_cas_races_remain_policy(self):
        self.register()
        for raw in (
            "{",
            "[]",
            '{"session_id":"session-1","session_id":"other"}',
            "x" * 131073,
        ):
            self.assert_runtime_notice(
                run_hook("codex", "Stop", raw, self.root, self.storage),
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
        self.assert_runtime_notice(self.invoke())

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

    def test_a_final_message_without_a_renderer_pointer_offers_no_candidate(self):
        """An ordinary Markdown link is not a malfunction.

        A tracked turn that ended on a pull-request link was told the toolkit
        had failed - `AHK-HOOK-RUNTIME` - and consumers learned to avoid links
        altogether. With no renderer-owned pointer there is no candidate, and
        the Stop gets the ordinary keep-working outcome.
        """

        self.register()
        for index, message in enumerate(
            (
                "Opened [the pull request](https://example.invalid/pull/12).",
                "The record says\nContinue from handoff: somewhere else.",
                "See [notes](</tmp/notes.md>) and Continue from handoff: later.",
            )
        ):
            with self.subTest(message=index):
                result = self.invoke(last_assistant_message=message)
                self.assert_block(result, "AHK-STOP-WORK")
                self.assertNotIn("AHK-HOOK-RUNTIME", result.stdout)
                self.assertNotIn("AHK-STOP-POINTER", result.stdout)
                self.invoke("UserPromptSubmit", turn_id="reset-link-" + str(index))

    def test_pointer_policy_failures_are_named_counted_policy_blocks(self):
        """A malformed renderer pointer is the model's to correct, not a fault.

        Each failure is `AHK-STOP-POINTER` with its own closed-vocabulary
        code, and it counts toward the correction circuit like any other
        policy block.
        """

        self.register()
        path, _, message = self.record()
        _, _, audit = self.record("completion-audit", "audit")
        outside = (self.root / "outside.md").as_posix()
        handoffs = (self.root / "handoffs").as_posix()
        for value, code in (
            (message + "\nTrailing", "pointer-ambiguous"),
            (message + "\n[extra](</other.md>)", "pointer-ambiguous"),
            (
                message.replace(
                    "Continue from handoff: " + path.as_posix(),
                    "Continue from handoff: " + handoffs + "/other.md",
                ),
                "pointer-block-mismatch",
            ),
            (
                "Continue from handoff: " + handoffs + "/audit.md\n" + audit,
                "pointer-audit-restart",
            ),
            (message.replace(path.as_posix(), outside), "pointer-outside-handoffs"),
            (message.replace("first.md", "first.json"), "pointer-noncanonical"),
        ):
            with self.subTest(code=code, length=len(value)):
                result = self.invoke(last_assistant_message=value)
                self.assert_block(result, "AHK-STOP-POINTER")
                reason = json.loads(result.stdout)["reason"]
                self.assertIn("failed=" + code, reason)
                self.assertIn("render-tail", reason)
                self.assertNotIn("AHK-HOOK-RUNTIME", reason)
                if code == "pointer-outside-handoffs":
                    self.assertIn(" root=" + handoffs, reason)
                else:
                    self.assertNotIn(" root=", reason)
                self.assertEqual(
                    self.storage.load_snapshot(
                        "session-1"
                    ).session.correction_cycle_count,
                    1,
                )
                self.invoke("UserPromptSubmit", turn_id="reset-" + str(len(value)))
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )

    def test_a_pointer_to_a_missing_record_is_a_counted_policy_block(self):
        """A record that is not in this checkout is the model's to correct."""

        self.register()
        path, _, message = self.record()
        path.unlink()
        result = self.invoke(last_assistant_message=message)
        self.assert_block(result, "AHK-STOP-POINTER")
        reason = json.loads(result.stdout)["reason"]
        self.assertIn("failed=pointer-missing", reason)
        self.assertNotIn("AHK-HOOK-RUNTIME", reason)
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 1
        )

    def test_a_continuation_reference_resumes_but_a_paraphrase_cannot(self):
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
            EnforcementMode.OPEN,
        )
        output = self.invoke(
            "UserPromptSubmit",
            session_id="new-session",
            turn_id="user-2",
            prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "AHK-RESUMED",
            json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(
            self.storage.load_snapshot("new-session").session.authorization_id,
            self.storage.load_snapshot("session-1").session.authorization_id,
        )

    def test_a_trailing_newline_on_the_stop_candidate_is_accepted(self):
        """`render-tail` prints the response followed by a newline.

        A host that reported the final message with that newline used to be
        told the toolkit had malfunctioned - `AHK-HOOK-RUNTIME` - for a
        response that was byte-for-byte the one the renderer produced.
        """

        self.register()
        _, _, message = self.record()
        self.assertEqual(
            self.invoke(last_assistant_message=message + "\n"), HookExecution()
        )
        self.assertEqual(
            self.storage.load_snapshot(
                "session-1"
            ).chain.current_record_reference.record_id,
            "first",
        )

    def test_repeated_trailing_newlines_on_the_stop_candidate_are_accepted(self):
        self.register()
        _, _, message = self.record()
        self.assertEqual(
            self.invoke(last_assistant_message=message + "\n\n"), HookExecution()
        )
        self.assertIsNotNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )

    def test_text_after_the_candidate_link_is_still_ambiguous(self):
        """Only trailing newlines are tolerated; the body stays byte-exact."""

        self.register()
        _, _, message = self.record()
        result = self.invoke(last_assistant_message=message + "\nStill working.")
        self.assert_block(result, "AHK-STOP-POINTER")
        self.assertIn("failed=pointer-ambiguous", result.stdout)
        self.assertIsNone(
            self.storage.load_snapshot("session-1").chain.current_record_reference
        )

    def test_trailing_spaces_on_the_stop_candidate_are_not_tolerated(self):
        self.register()
        _, _, message = self.record()
        self.assert_block(
            self.invoke(last_assistant_message=message + " "), "AHK-STOP-RESPONSE"
        )

    def test_the_pointer_line_alone_resumes_and_later_prompt_text_is_the_work(self):
        """Lineage is the record digest and the live chain, never the prose.

        One extra space in a pasted prompt used to leave the new session
        untracked with no message at all, which is the quietest way the
        toolkit had of losing an epic.
        """

        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        prompt = (
            render_resume_prompt(path, text)
            + "\n\nAlso rerun the migration before anything else."
        )
        output = self.invoke("UserPromptSubmit", session_id="resumed", prompt=prompt)
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AHK-RESUMED", context)
        self.assertIn("issue-1", context)
        self.assertEqual(
            self.storage.load_snapshot("resumed").session.authorization_id,
            self.storage.load_snapshot("session-1").session.authorization_id,
        )

    def test_the_bare_pointer_line_is_enough_to_resume(self):
        self.register()
        path, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        output = self.invoke(
            "UserPromptSubmit",
            session_id="bare",
            prompt="Continue from handoff: " + path.as_posix(),
        )
        self.assertIn(
            "AHK-RESUMED",
            json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"],
        )
        self.assertIs(
            self.storage.load_snapshot("bare").session.mode, EnforcementMode.TRACKED
        )

    def test_an_altered_prompt_body_still_resumes_because_the_digest_decides(self):
        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        altered = render_resume_prompt(path, text).replace(
            "Exact next action:", "Next:"
        )
        self.invoke("UserPromptSubmit", session_id="altered", prompt=altered)
        self.assertIs(
            self.storage.load_snapshot("altered").session.mode, EnforcementMode.TRACKED
        )

    def test_a_pointer_the_chain_does_not_hold_reports_the_failure(self):
        """Silence was the defect: the user was never told tracking was lost."""

        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        other, _, _ = self.record(
            name="second",
            predecessor={
                "record_id": "first",
                "path": path.as_posix(),
                "sha256": record_digest(text),
            },
        )
        output = self.invoke(
            "UserPromptSubmit",
            session_id="wrong-record",
            prompt="Continue from handoff: " + other.as_posix(),
        )
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AHK-RESUME-FAILED failed=record-digest", context)
        self.assertIn("untracked", context)
        self.assertIs(
            self.storage.load_snapshot("wrong-record").session.mode,
            EnforcementMode.OPEN,
        )

    def test_a_pointer_outside_handoffs_reports_its_own_code(self):
        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        outside = self.root / "outside.md"
        outside.write_bytes(text.encode())
        output = self.invoke(
            "UserPromptSubmit",
            session_id="outside",
            prompt="Continue from handoff: " + outside.as_posix(),
        )
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AHK-RESUME-FAILED failed=candidate-outside-handoffs", context)
        self.assertIs(
            self.storage.load_snapshot("outside").session.mode, EnforcementMode.OPEN
        )

    def test_a_prompt_that_does_not_open_with_the_pointer_never_resumes(self):
        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        for index, prompt in (
            (0, "Please continue from the newest handoff."),
            (1, "Please " + render_resume_prompt(path, text)),
            (2, "Context first.\nContinue from handoff: " + path.as_posix()),
        ):
            with self.subTest(prompt=index):
                session = "no-pointer-" + str(index)
                self.assertEqual(
                    self.invoke("UserPromptSubmit", session_id=session, prompt=prompt),
                    HookExecution(),
                )
                self.assertIs(
                    self.storage.load_snapshot(session).session.mode,
                    EnforcementMode.OPEN,
                )

    def test_an_ordinary_tool_call_never_opens_lifecycle_state(self):
        """Nothing is decided for it in any mode, so nothing is loaded for it.

        Every Bash call used to open the registry, take its lock and run a git
        subprocess before discovering it had nothing to say.
        """

        class Unopenable:
            def __getattr__(self, name):
                raise AssertionError("lifecycle state opened for an ordinary call")

        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.assertEqual(
                    run_hook(
                        host,
                        "PreToolUse",
                        json.dumps(
                            payload(
                                self.root,
                                "PreToolUse",
                                tool_name="Bash",
                                tool_input={"command": "git status"},
                            )
                        ),
                        self.root,
                        Unopenable(),
                    ),
                    HookExecution(),
                )

    def test_control_commands_and_write_tools_still_load_state(self):
        """The fast path must not swallow the two calls that decide something."""

        class Unopenable:
            def __getattr__(self, name):
                raise RuntimeError("state unavailable")

        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        for host, tool, tool_input in (
            ("claude", "Bash", {"command": f"python {runner} lifecycle inspect"}),
            ("claude", "Write", {"file_path": str(self.root / "a.py")}),
            ("codex", "apply_patch", {"command": "*** Add File: a.py"}),
        ):
            with self.subTest(host=host, tool=tool):
                output = run_hook(
                    host,
                    "PreToolUse",
                    json.dumps(
                        payload(
                            self.root,
                            "PreToolUse",
                            tool_name=tool,
                            tool_input=tool_input,
                        )
                    ),
                    self.root,
                    Unopenable(),
                )
                self.assertIn("AHK-HOOK-RUNTIME", output.stdout)

    def track(self, title, session="tracked-by-user", turn="turn-track"):
        return self.invoke(
            "UserPromptSubmit",
            session_id=session,
            turn_id=turn,
            prompt=f"Track: {title}\n\nStart with the migration.",
        )

    def test_a_user_can_declare_the_tracked_goal_in_their_own_turn(self):
        """The user knows when a request is an epic; the agent was guessing.

        Binding the root to the user's own turn is also the strongest
        authorization evidence the design ever wanted.
        """

        output = self.track("Ship billing v2")
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AHK-TRACKED", context)
        self.assertIn("Ship billing v2", context)

        snapshot = self.storage.load_snapshot("tracked-by-user")
        self.assertIs(snapshot.session.mode, EnforcementMode.TRACKED)
        self.assertTrue(snapshot.chain.locked_root_id.startswith("ship-billing-v2-"))
        self.assertEqual(snapshot.chain.authorization_user_turn_reference, "turn-track")

    def test_the_declared_title_is_the_scope_definition(self):
        self.track("Ship billing v2")
        chain = self.storage.load_snapshot("tracked-by-user").chain
        definition = {"title": "Ship billing v2", "outcome": "Ship billing v2"}
        self.assertEqual(
            chain.scope_digests,
            (
                scope_definition_digest(
                    {
                        "scope_id": chain.locked_root_id,
                        "scope_kind": "epic",
                        "parent_scope_id": None,
                        "scope_definition": definition,
                    }
                ),
            ),
        )

    def test_a_second_session_with_the_same_goal_joins_the_existing_chain(self):
        self.track("Ship billing v2")
        first = self.storage.load_snapshot("tracked-by-user")
        self.track("Ship billing v2", session="second", turn="turn-second")
        second = self.storage.load_snapshot("second")
        self.assertIs(second.session.mode, EnforcementMode.TRACKED)
        self.assertEqual(
            second.session.authorization_id, first.session.authorization_id
        )

    def test_the_prefix_is_recognized_only_as_the_first_line(self):
        for index, prompt in enumerate(
            (
                "Please do this.\nTrack: Ship billing v2",
                "  Track: Ship billing v2",
                "Track:",
                "Track: ab",
            )
        ):
            with self.subTest(prompt=index):
                session = "untracked-" + str(index)
                self.assertEqual(
                    self.invoke("UserPromptSubmit", session_id=session, prompt=prompt),
                    HookExecution(),
                )
                self.assertIs(
                    self.storage.load_snapshot(session).session.mode,
                    EnforcementMode.OPEN,
                )

    def test_tracking_hands_the_session_its_bound_inspect_command(self):
        """Two tool calls and a denial used to be the only way to learn these."""

        context = json.loads(self.track("Ship billing v2").stdout)[
            "hookSpecificOutput"
        ]["additionalContext"]
        command = context.split("Command: ", 1)[1].strip()
        self.assertIn("lifecycle inspect", command)
        self.assertEqual(
            self.invoke(
                "PreToolUse",
                session_id="tracked-by-user",
                tool_input={"command": command},
            ),
            HookExecution(),
        )

    def test_a_resume_hands_the_session_its_bound_inspect_command(self):
        self.register()
        path, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        context = json.loads(
            self.invoke(
                "UserPromptSubmit",
                session_id="resumed",
                prompt="Continue from handoff: " + path.as_posix(),
            ).stdout
        )["hookSpecificOutput"]["additionalContext"]
        command = context.split("Command: ", 1)[1].strip()
        self.assertIn("lifecycle inspect", command)
        self.assertEqual(
            self.invoke(
                "PreToolUse", session_id="resumed", tool_input={"command": command}
            ),
            HookExecution(),
        )

    def relocate_chain(self, name="first", other="D:/other-checkout"):
        """Point the live chain at the same record in another checkout."""

        snapshot = self.storage.load_snapshot("session-1")
        chain = snapshot.chain
        reference = chain.current_record_reference
        moved = replace(reference, path=f"{other}/handoffs/{name}.md")
        self.storage.compare_and_swap(
            "session-1",
            chain.targeted_revision,
            snapshot.session.targeted_revision,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                    chain_revision=chain.targeted_revision + 1,
                ),
                replace(
                    chain,
                    targeted_revision=chain.targeted_revision + 1,
                    current_record_reference=moved,
                ),
            ),
        )
        return moved

    def test_a_chain_recorded_in_another_checkout_still_resumes_here(self):
        """Records chain through absolute paths; worktrees and WSL break them.

        The record's digest is the evidence and the path is only a locator, so
        the same record under this checkout's `handoffs/` resumes the chain.
        """

        self.register()
        path, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.relocate_chain()

        output = self.invoke(
            "UserPromptSubmit",
            session_id="other-checkout",
            prompt="Continue from handoff: " + path.as_posix(),
        )
        self.assertIn(
            "AHK-RESUMED",
            json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"],
        )
        self.assertIs(
            self.storage.load_snapshot("other-checkout").session.mode,
            EnforcementMode.TRACKED,
        )

    def test_a_relocated_pointer_with_a_different_digest_still_fails(self):
        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        snapshot = self.storage.load_snapshot("session-1")
        chain = snapshot.chain
        self.storage.compare_and_swap(
            "session-1",
            chain.targeted_revision,
            snapshot.session.targeted_revision,
            LifecycleMutation(
                replace(
                    snapshot.session,
                    targeted_revision=snapshot.session.targeted_revision + 1,
                    chain_revision=chain.targeted_revision + 1,
                ),
                replace(
                    chain,
                    targeted_revision=chain.targeted_revision + 1,
                    current_record_reference=replace(
                        chain.current_record_reference,
                        path="D:/other-checkout/handoffs/first.md",
                        sha256="b" * 64,
                    ),
                ),
            ),
        )
        output = self.invoke(
            "UserPromptSubmit",
            session_id="wrong-digest",
            prompt="Continue from handoff: " + path.as_posix(),
        )
        self.assertIn(
            "AHK-RESUME-FAILED failed=record-digest",
            json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"],
        )

    def test_a_successor_naming_the_other_checkout_is_accepted_by_digest(self):
        """The predecessor pointer copied from the record names the old path."""

        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        moved = self.relocate_chain()
        _, _, successor = self.record(
            name="second",
            predecessor={
                "record_id": "first",
                "path": moved.path,
                "sha256": record_digest(text),
            },
        )
        self.assertEqual(self.invoke(last_assistant_message=successor), HookExecution())
        self.assertEqual(
            self.storage.load_snapshot(
                "session-1"
            ).chain.current_record_reference.record_id,
            "second",
        )

    def test_a_relocated_successor_with_the_wrong_digest_is_blocked(self):
        self.register()
        path, text, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        moved = self.relocate_chain()
        _, _, successor = self.record(
            name="second",
            predecessor={
                "record_id": "first",
                "path": moved.path,
                "sha256": "c" * 64,
            },
        )
        self.assert_block(
            self.invoke(last_assistant_message=successor), "AHK-STOP-PREDECESSOR"
        )

    def progress_line(self, reason="reply", session="session-1"):
        chain = self.storage.load_snapshot(session).chain
        return progress_responses(chain)[reason]

    def host_prompt(self, turn_id="host-turn"):
        self.invoke(
            "UserPromptSubmit",
            turn_id=turn_id,
            prompt="<task-notification>\n<status>completed</status>\n"
            "</task-notification>",
        )

    def test_a_tracked_turn_may_end_on_the_reply_line_after_a_user_prompt(self):
        """Every turn end used to cost a full record.

        therapy-link authored nine continuations of 2,400 words for one task
        in one day, two of them 31 seconds apart, because `Stop` runs at every
        turn end rather than at the end of a session.
        """

        self.register()
        self.invoke("UserPromptSubmit", turn_id="user-2", prompt="How is it going?")
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.turn_origin, "user"
        )
        self.assertEqual(
            self.invoke(last_assistant_message=self.progress_line()), HookExecution()
        )
        session = self.storage.load_snapshot("session-1").session
        self.assertIs(session.mode, EnforcementMode.TRACKED)
        self.assertTrue(session.last_stop_was_progress)

    def test_every_wait_line_ends_a_turn_whatever_started_it(self):
        self.register()
        for index, reason in enumerate(("background-work", "ci", "other-session")):
            with self.subTest(reason=reason):
                self.host_prompt("host-" + str(index))
                self.assertEqual(
                    self.storage.load_snapshot("session-1").session.turn_origin,
                    "host",
                )
                self.assertEqual(
                    self.invoke(last_assistant_message=self.progress_line(reason)),
                    HookExecution(),
                )
                self.assertTrue(
                    self.storage.load_snapshot(
                        "session-1"
                    ).session.last_stop_was_progress
                )

    def test_a_host_started_turn_cannot_end_on_the_reply_line(self):
        """A task notification is not the user, so nothing was replied to."""

        self.register()
        self.host_prompt()
        result = self.invoke(last_assistant_message=self.progress_line())
        self.assert_block(result, "AHK-STOP-WORK")
        self.assertIn("failed=reply-host-turn", json.loads(result.stdout)["reason"])
        self.invoke("UserPromptSubmit", turn_id="user-2", prompt="Status?")
        self.assertEqual(
            self.invoke(last_assistant_message=self.progress_line()), HookExecution()
        )

    def test_an_unknown_turn_origin_accepts_the_reply_line(self):
        """State written before turn origins existed reads as unknown."""

        self.register()
        state = self.storage.load_snapshot("session-1")
        self.storage.compare_and_swap(
            "session-1",
            state.chain.targeted_revision,
            state.session.targeted_revision,
            LifecycleMutation(
                replace(
                    state.session,
                    targeted_revision=state.session.targeted_revision + 1,
                    turn_origin=None,
                ),
            ),
        )
        self.assertEqual(
            self.invoke(last_assistant_message=self.progress_line()), HookExecution()
        )

    def test_the_old_bare_progress_line_is_no_longer_an_ending(self):
        self.register()
        title = self.storage.load_snapshot("session-1").chain.root_title
        old = (
            f'In progress: "{title}". Last handoff: none yet. '
            'Say "continue" to keep going, or ask for a handoff.'
        )
        self.assert_block(self.invoke(last_assistant_message=old), "AHK-STOP-WORK")

    def test_stop_work_says_keep_working_and_names_every_legal_ending(self):
        self.register()
        reason = json.loads(self.invoke(last_assistant_message="Paused.").stdout)[
            "reason"
        ]
        for phrase in (
            "Executable work remains, so keep working.",
            "Waiting on",
            "reply line",
            "lifecycle inspect",
            "decision request",
            "continuation",
            "audit",
        ):
            self.assertIn(phrase, reason)

    def test_any_other_record_less_message_is_still_blocked(self):
        self.register()
        for message in (
            "Still working on it.",
            self.progress_line() + " Nearly done.",
            self.progress_line("ci").replace("Waiting on", "Waiting for"),
        ):
            with self.subTest(message=message[:40]):
                self.assert_block(
                    self.invoke(last_assistant_message=message), "AHK-STOP-WORK"
                )
                self.invoke("UserPromptSubmit", turn_id="reset-" + str(len(message)))

    def test_the_progress_line_names_the_last_handoff(self):
        self.register()
        self.assertIn("none yet", self.progress_line())
        path, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assertIn(path.as_posix(), self.progress_line())
        self.assertFalse(
            self.storage.load_snapshot("session-1").session.last_stop_was_progress
        )

    def test_a_record_stop_after_a_progress_stop_clears_the_flag(self):
        self.register()
        self.invoke(last_assistant_message=self.progress_line())
        self.assertTrue(
            self.storage.load_snapshot("session-1").session.last_stop_was_progress
        )
        _, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assertFalse(
            self.storage.load_snapshot("session-1").session.last_stop_was_progress
        )

    def test_the_progress_line_is_published_by_inspect_not_by_feedback(self):
        """Blocking feedback carries codes and bounded identifiers, never text.

        The line contains the declared goal in the user's own words, so it is
        published through `inspect`, which the session already runs.
        """

        self.register()
        blocked = self.invoke(last_assistant_message="Still working.")
        reason = json.loads(blocked.stdout)["reason"]
        self.assertIn("AHK-STOP-WORK", reason)
        self.assertNotIn("Replied to your message", reason)
        self.assertNotIn("Waiting on background", reason)
        published = self.service.inspect()["progress_responses"]
        self.assertEqual(
            published, progress_responses(self.storage.load_snapshot("session-1").chain)
        )
        self.assertEqual(published["reply"], self.progress_line())

    def test_session_end_reports_a_session_that_closed_on_a_progress_line(self):
        self.register()
        self.invoke(last_assistant_message=self.progress_line())
        output = run_hook(
            "claude",
            "session-end",
            json.dumps({"session_id": "session-1"}),
            self.root,
            self.storage,
        )
        message = json.loads(output.stdout)["systemMessage"]
        self.assertIn("ended without a final handoff", message)
        self.assertIn("Track: ", message)

    def test_session_end_is_silent_after_a_record_stop(self):
        self.register()
        _, _, message = self.record()
        self.assertEqual(self.invoke(last_assistant_message=message), HookExecution())
        self.assertEqual(
            run_hook(
                "claude",
                "session-end",
                json.dumps({"session_id": "session-1"}),
                self.root,
                self.storage,
            ),
            HookExecution(),
        )

    def test_session_end_is_silent_for_an_untracked_session(self):
        self.assertEqual(
            run_hook(
                "claude",
                "session-end",
                json.dumps({"session_id": "never-tracked"}),
                self.root,
                self.storage,
            ),
            HookExecution(),
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

    def test_a_host_prompt_is_never_judged_as_the_pending_answer(self):
        """Task notifications and peer messages arrive as prompts, not answers.

        One session drew thirteen `AHK-USER-CLARIFY` notices from them while
        its decision waited on the user.
        """

        self.register()
        self.assertEqual(
            self.invoke(last_assistant_message=self.decision()), HookExecution()
        )
        for index, prompt in enumerate(HOST_PROMPTS):
            with self.subTest(prompt=prompt[:30]):
                output = self.invoke(
                    "UserPromptSubmit", turn_id="host-" + str(index), prompt=prompt
                )
                self.assertNotIn("CLARIFY", output.stdout)
                session = self.storage.load_snapshot("session-1").session
                self.assertIs(session.mode, EnforcementMode.AWAITING_DECISION)
                self.assertIsNotNone(session.pending_decision_reference)
                self.assertEqual(session.turn_origin, "host")
        self.invoke("UserPromptSubmit", turn_id="user-2", prompt="yes")
        self.assertIs(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.TRACKED,
        )

    def test_a_host_prompt_keeps_a_proposal_adjacent_to_the_users_answer(self):
        """A task notification between a proposal and the user's yes.

        The host's turn used to be observed as the user's: it became the
        current user turn, so the proposal was no longer adjacent to the
        answer that followed it, and the approval drew a clarification.
        """

        self.register()
        _, _, message = self.record()
        self.invoke(last_assistant_message=message)
        self.propose()
        before = self.storage.load_snapshot("session-1").session
        self.host_prompt("host-1")
        after = self.storage.load_snapshot("session-1").session
        self.assertEqual(after.turn_origin, "host")
        self.assertEqual(
            after.current_external_user_turn_reference,
            before.current_external_user_turn_reference,
        )
        self.assertEqual(
            after.pending_transition_reference, before.pending_transition_reference
        )
        self.assertEqual(
            self.invoke("UserPromptSubmit", prompt="yes", turn_id="user-2"),
            HookExecution(),
        )
        approved = self.storage.load_snapshot("session-1")
        self.assertEqual(approved.chain.locked_root_id, "issue-2")
        self.assertEqual(approved.session.turn_origin, "user")

    def test_a_host_prompt_does_not_reset_the_correction_circuit(self):
        """Only a real user turn clears the circuit; the host is not one."""

        self.register()
        for _ in range(2):
            self.assert_block(self.invoke(), "AHK-STOP-WORK")
        self.host_prompt("host-1")
        session = self.storage.load_snapshot("session-1").session
        self.assertEqual(session.correction_cycle_count, 2)
        self.assertIsNotNone(session.last_issue_signature)
        self.assertFalse(json.loads(self.invoke().stdout)["continue"])
        self.invoke("UserPromptSubmit", prompt="Carry on.", turn_id="user-2")
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.correction_cycle_count, 0
        )

    def test_clarify_names_the_replies_it_accepts(self):
        self.register()
        self.invoke(last_assistant_message=self.decision())
        output = self.invoke(
            "UserPromptSubmit",
            turn_id="user-2",
            prompt="yes- always merge on green",
        )
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        for phrase in (
            "AHK-USER-CLARIFY",
            "Re-ask the pending question",
            "cannot record completion until the user answers yes or no",
            "yes, yeah, yep, approved, looks right, go ahead, go with <option>",
            "no, nope, reject, rejected",
            "Do not perform the blocked action on an ambiguous reply",
        ):
            self.assertIn(phrase, context)
        self.assertNotIn("always merge", output.stdout)
        self.assertIs(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.AWAITING_DECISION,
        )

    def test_clarify_names_exactly_the_classifier_replies(self):
        from agent_handoff_toolkit import hook_adapters, lifecycle

        approvals = hook_adapters._APPROVAL_FORMS.split(", ")
        rejections = hook_adapters._REJECTION_FORMS.split(", ")
        self.assertEqual(set(approvals) - {"go with <option>"}, lifecycle._APPROVALS)
        self.assertEqual(set(rejections), lifecycle._REJECTIONS)
        for form, expected in [
            *((form, lifecycle.AffirmationResult.APPROVE) for form in approvals),
            *((form, lifecycle.AffirmationResult.REJECT) for form in rejections),
        ]:
            with self.subTest(form=form):
                self.assertIs(
                    lifecycle.classify_affirmation(form.replace("<option>", "b")),
                    expected,
                )

    def test_clarify_for_an_input_decision_names_the_field_form(self):
        self.register()
        self.invoke(last_assistant_message=self.decision("missing-input"))
        output = self.invoke(
            "UserPromptSubmit", turn_id="user-2", prompt="staging please"
        )
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("target: <value>", context)
        self.assertIn("Re-ask the pending question", context)
        self.assertIn(
            "Do not perform the blocked action on an ambiguous reply", context
        )
        self.assertNotIn("staging", context)

    def test_runtime_failures_from_malformed_stop_leave_circuit_unchanged(self):
        self.register()
        # Ordinary host content is never a fault, so drive this with a genuine
        # normalization failure: the loop-evidence flag must be boolean.
        malformed = {"stop_hook_active": "not-a-boolean"}
        for attempt in (1, 2):
            self.assert_runtime_notice(self.invoke(**malformed))
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.correction_cycle_count,
                0,
            )
        self.assert_runtime_notice(self.invoke(**malformed))

    def test_large_tool_input_never_blocks_and_never_reaches_state(self):
        """The payload is the work's; it must neither fail nor be recorded."""

        self.register()
        sentinel = "sensitive-synthetic-sentinel"
        self.assertEqual(
            self.invoke(
                "PreToolUse",
                tool_name="Write",
                tool_input={"file_path": "a.py", "content": sentinel + "x" * 200_000},
            ),
            HookExecution(),
        )
        self.assertNotIn(sentinel, self.storage.registry_path.read_text())

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
                self.invoke(last_assistant_message=message), "AHK-STOP-POINTER"
            )
        link = self.root / "handoffs" / "link.md"
        try:
            link.symlink_to(path)
        except OSError:
            # Windows may lack symlink privilege; the reparse branch above runs everywhere.
            return
        self.assert_block(
            self.invoke(last_assistant_message=render_terminal_response(link, text)),
            "AHK-STOP-POINTER",
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
                "AHK-STOP-POINTER",
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

    def test_a_peer_publication_never_silences_the_joined_session(self):
        """A peer advancing the chain must not erase another session's prompt.

        Blocking a `Stop` leaves the agent running and able to act on the
        feedback. Blocking `UserPromptSubmit` destroys the user's message and
        starts no turn, so nobody is left who can act on it - and the session
        could not recover, because the reconciliation bookkeeping runs only on
        a decision carrying a mutation, and no turn ever began to produce one.
        The prompt is delivered and the session reconciles instead.
        """

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
        output = self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            turn_id="turn-2",
            prompt="What is the current status?",
        )
        self.assertEqual(output.exit_code, 0)
        self.assertEqual(output.stderr, "")
        data = json.loads(output.stdout) if output.stdout else {}
        self.assertNotIn("decision", data)
        self.assertNotIn("continue", data)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AHK-CHAIN-ADVANCED", context)
        # Reconciling worked. Telling the user the toolkit skipped bookkeeping
        # would report a malfunction on the ordinary path.
        self.assertNotIn("systemMessage", data)
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)

    def peer_publishes(self):
        """Join a second session, then let the first publish past it.

        Returns the joined session's wait line from before the publication,
        when its last handoff was still the first record.
        """

        self.register()
        path, text, message = self.record()
        self.invoke(last_assistant_message=message)
        self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
        )
        before = self.progress_line("other-session", session="joined")
        predecessor = {
            "record_id": "first",
            "path": path.as_posix(),
            "sha256": record_digest(text),
        }
        _, _, successor = self.record(name="second", predecessor=predecessor)
        self.assertEqual(self.invoke(last_assistant_message=successor), HookExecution())
        return before

    def test_a_stop_after_a_peer_publishes_names_the_new_record_uncounted(self):
        """A peer's publication is not this session's mistake.

        `Stop` used to count it: one `AHK-STOP-STALE`, then the old wait line
        drew `AHK-STOP-WORK` because it named the old record, and a third
        attempt tripped the circuit. The chain is reconciled first, the block
        names the record that is now current, and nothing is counted.
        """

        before = self.peer_publishes()
        result = self.invoke(session_id="joined", last_assistant_message=before)
        self.assert_block(result, "AHK-STOP-STALE")
        reason = json.loads(result.stdout)["reason"]
        self.assertIn("current=second.md", reason)
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.correction_cycle_count, 0)
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)
        # The accepted lines are the reconciled chain's: they name the new
        # record, and the next ending on one is accepted.
        after = self.progress_line("other-session", session="joined")
        self.assertIn("second.md", after)
        self.assertNotEqual(after, before)
        self.assertEqual(
            self.invoke(session_id="joined", last_assistant_message=after),
            HookExecution(),
        )

    def test_a_stop_after_a_peer_publishes_accepts_the_reconciled_line(self):
        self.peer_publishes()
        line = self.progress_line("background-work", session="joined")
        self.assertEqual(
            self.invoke(session_id="joined", last_assistant_message=line),
            HookExecution(),
        )
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)
        self.assertTrue(joined.session.last_stop_was_progress)

    def test_a_peer_publishing_during_a_stop_is_not_counted_either(self):
        """The commit race: the peer publishes between this Stop's read and write."""

        before = self.peer_publishes()
        self.invoke(
            session_id="joined",
            last_assistant_message=self.progress_line(
                "other-session", session="joined"
            ),
        )
        joined = self.storage.load_snapshot("joined")
        predecessor = {
            "record_id": "second",
            "path": joined.chain.current_record_reference.path,
            "sha256": joined.chain.current_record_reference.sha256,
        }
        _, _, third = self.record(name="third", predecessor=predecessor)
        original = self.storage.compare_and_swap
        raced = False

        def peer_first(raw_id, *args, **kwargs):
            nonlocal raced
            if raw_id == "joined" and not raced:
                raced = True
                self.assertEqual(
                    self.invoke(last_assistant_message=third), HookExecution()
                )
            return original(raw_id, *args, **kwargs)

        with patch.object(self.storage, "compare_and_swap", peer_first):
            result = self.invoke(session_id="joined", last_assistant_message=before)
        self.assertTrue(raced)
        self.assert_block(result, "AHK-STOP-STALE")
        self.assertIn("current=third.md", json.loads(result.stdout)["reason"])
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.correction_cycle_count, 0)
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)

    def test_a_refused_pointer_after_a_peer_publishes_is_not_counted(self):
        """A pointer refused against a chain a peer moved is the peer's doing.

        Like every other block in that situation it is the uncounted
        `AHK-STOP-STALE` naming the current record, not `AHK-STOP-POINTER`.
        """

        self.peer_publishes()
        _, _, message = self.record(name="third")
        result = self.invoke(
            session_id="joined", last_assistant_message=message + "\nTrailing"
        )
        self.assert_block(result, "AHK-STOP-STALE")
        reason = json.loads(result.stdout)["reason"]
        self.assertIn("current=second.md", reason)
        self.assertNotIn("AHK-STOP-POINTER", reason)
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.correction_cycle_count, 0)
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)

    def test_a_declaration_that_cannot_be_observed_is_never_enrolled_silently(self):
        """Enrollment is not independent of observation, and must not be.

        `register_root` binds the new authorization to whatever external turn
        reference is already stored, so enrolling after a failed observation
        would name the previous turn as the authority for this one. Skipping
        it is right; skipping it quietly is not, because the user asked for
        tracking in so many words.
        """

        for index, (prompt, code) in enumerate(
            (
                ("Track: ship the reconciliation fix", "AHK-TRACK-FAILED"),
                (
                    "Continue from handoff: D:/repo/handoffs/first.md",
                    "AHK-RESUME-FAILED",
                ),
            )
        ):
            with self.subTest(code=code):
                with patch(
                    "agent_handoff_toolkit.hook_adapters.LifecycleService"
                    ".observe_user_turn",
                    side_effect=StaleLifecycleState(
                        "session requires chain reconciliation"
                    ),
                ):
                    output = self.invoke(
                        "UserPromptSubmit",
                        turn_id=f"turn-7-{index}",
                        prompt=prompt,
                    )
                self.assertEqual(output.exit_code, 0)
                data = json.loads(output.stdout)
                self.assertNotIn("decision", data)
                context = data["hookSpecificOutput"]["additionalContext"]
                self.assertIn(code, context)
                self.assertIn("untracked", context)
                self.assertEqual(
                    self.storage.load_snapshot("session-1").session.mode,
                    EnforcementMode.OPEN,
                )

    def test_a_tracked_session_is_never_told_that_it_is_untracked(self):
        """The untracked warning has to be true when it is emitted.

        Enrollment is only ever eligible for a session that has registered no
        root. Keying the warning off the prompt text alone told a tracked
        session that it was untracked whenever a turn's bookkeeping failed and
        the message happened to start with the declaration prefix.
        """

        self.register()
        with patch(
            "agent_handoff_toolkit.hook_adapters.LifecycleService.observe_user_turn",
            side_effect=StaleLifecycleState("session requires chain reconciliation"),
        ):
            output = self.invoke(
                "UserPromptSubmit",
                turn_id="turn-8",
                prompt="Track: an unrelated second goal",
            )
        context = json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("step:observe-turn", context)
        self.assertNotIn("untracked", context)

    def test_a_failed_step_is_named_as_itself_and_not_as_a_later_one(self):
        """A report that names the wrong stage is worse than a vague one.

        Wrapping the whole sequence in one attempt labelled every failure
        `observe-turn`, including failures that happened before observation
        was reached, and skipped the independent steps that follow.
        """

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
        _, _, audit = self.record(
            kind="completion-audit", name="final", predecessor=predecessor
        )
        self.invoke(last_assistant_message=audit)
        with patch(
            "agent_handoff_toolkit.hook_adapters.evaluate_user_prompt",
            side_effect=RuntimeError("reentry unavailable"),
        ):
            output = self.invoke(
                "UserPromptSubmit",
                session_id="joined",
                turn_id="turn-4",
                prompt="Anything at all",
            )
        self.assertEqual(output.exit_code, 0)
        data = json.loads(output.stdout)
        self.assertNotIn("decision", data)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("step:chain-reentry", context)
        self.assertNotIn("step:observe-turn", context)

    def test_a_failing_reconciliation_still_delivers_the_prompt(self):
        """Reconciliation is an optimisation, not a precondition.

        If the refresh itself cannot be written, the turn proceeds with stale
        bookkeeping. That is a degraded turn; losing the input channel is not.
        """

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
        self.invoke(last_assistant_message=successor)
        with patch(
            "agent_handoff_toolkit.hook_adapters._commit",
            side_effect=StaleLifecycleState("targeted lifecycle revision changed"),
        ):
            output = self.invoke(
                "UserPromptSubmit",
                session_id="joined",
                turn_id="turn-5",
                prompt="Still here?",
            )
        self.assertEqual(output.exit_code, 0)
        data = json.loads(output.stdout)
        self.assertNotIn("decision", data)
        self.assertIn(
            "step:reconcile-chain",
            data["hookSpecificOutput"]["additionalContext"],
        )

    def test_an_unexpected_enrollment_failure_still_says_untracked(self):
        """`_resume_chain` and `_track_root` name their expected failures.

        An unexpected one reached the generic backstop and reported
        `step:prompt-path`, so a session the user explicitly asked to track
        was left untracked with nobody told.
        """

        with patch(
            "agent_handoff_toolkit.hook_adapters._track_root",
            side_effect=RuntimeError("registration unavailable"),
        ):
            output = self.invoke(
                "UserPromptSubmit",
                turn_id="turn-11",
                prompt="Track: a goal the toolkit cannot register",
            )
        self.assertEqual(output.exit_code, 0)
        data = json.loads(output.stdout)
        self.assertNotIn("decision", data)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("step:enroll", context)
        self.assertIn("AHK-TRACK-FAILED", context)
        self.assertIn("untracked", context)

    def test_a_failed_correction_classification_does_not_cost_the_turn(self):
        """Deriving the challenge encodes the user's own message.

        That can raise on input the host accepted and UTF-8 does not, and the
        classification sat outside the step, so an unrelated fault skipped
        every later step including observing the turn.
        """

        self.register()
        with patch(
            "agent_handoff_toolkit.hook_adapters._correction_hmac",
            side_effect=UnicodeEncodeError("utf-8", "", 0, 1, "surrogates"),
        ):
            output = self.invoke("UserPromptSubmit", turn_id="turn-12")
        self.assertEqual(output.exit_code, 0)
        data = json.loads(output.stdout) if output.stdout else {}
        self.assertNotIn("decision", data)
        # The later step still ran: the turn is observed despite the fault.
        session = self.storage.load_snapshot("session-1").session
        self.assertEqual(session.current_external_user_turn_reference, "turn-12")

    def test_a_failure_between_steps_still_delivers_the_prompt(self):
        """The backstop covers what the individual steps do not.

        Each step names itself, but something has to hold when the failure is
        between them - or inside the machinery that builds the notices.
        """

        with patch(
            "agent_handoff_toolkit.hook_adapters._user_prompt_steps",
            side_effect=RuntimeError("anything at all"),
        ):
            output = self.invoke("UserPromptSubmit", turn_id="turn-6")
        self.assertEqual(output.exit_code, 0)
        self.assertEqual(output.stderr, "")
        data = json.loads(output.stdout)
        self.assertNotIn("decision", data)
        self.assertNotIn("continue", data)
        self.assertIn(
            "step:prompt-path", data["hookSpecificOutput"]["additionalContext"]
        )

    def test_reconciling_does_not_let_a_stale_successor_publish(self):
        """The one test that proves liveness was not bought with lineage.

        Reconciliation refreshes what a session believes about its chain, so
        the question it raises is whether a draft written against the old
        predecessor can now land. It cannot: the revision a candidate carries
        is stamped at Stop, and the real gate is predecessor identity - record
        id, basename and digest against the chain's current record.
        """

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
        # The joined session drafts S0 against P0 while the peer publishes P1.
        _, _, stale_successor = self.record(name="stale", predecessor=predecessor)
        _, _, peer_successor = self.record(name="second", predecessor=predecessor)
        self.assertEqual(
            self.invoke(last_assistant_message=peer_successor), HookExecution()
        )
        live = self.storage.load_snapshot("session-1").chain.current_record_reference
        # The prompt reconciles the joined session onto the advanced chain.
        self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            turn_id="turn-10",
            prompt="Carry on.",
        )
        joined = self.storage.load_snapshot("joined")
        self.assertEqual(joined.session.chain_revision, joined.chain.targeted_revision)
        # S0 still names P0, which is no longer the chain's record.
        self.assert_block(
            self.invoke(session_id="joined", last_assistant_message=stale_successor),
            "AHK-STOP-PREDECESSOR",
        )
        self.assertEqual(
            self.storage.load_snapshot("joined").chain.current_record_reference, live
        )

    def test_a_peer_completion_releases_this_session_rather_than_stranding_it(self):
        """Completing a chain must not strand the peers joined to it.

        Only the publishing session is moved to `COMPLETE`; peers stay
        `TRACKED` on a finished chain, which persisted state permits and every
        later mutation then refuses as an unreleased lease. The session could
        be neither spoken to nor finished. Reconciliation releases the lease,
        and the existing reentry takes it from there.
        """

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
        _, _, audit = self.record(
            kind="completion-audit", name="final", predecessor=predecessor
        )
        self.assertEqual(self.invoke(last_assistant_message=audit), HookExecution())
        self.assertEqual(
            self.storage.load_snapshot("session-1").chain.status, "complete"
        )
        output = self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            turn_id="turn-3",
            prompt="Now what?",
        )
        self.assertEqual(output.exit_code, 0)
        data = json.loads(output.stdout) if output.stdout else {}
        self.assertNotIn("decision", data)
        self.assertNotIn("continue", data)
        # Released and reentered on this same turn, so the session is free to
        # declare new work rather than holding a lease on a finished
        # authorization. Accepting COMPLETE here would let the test pass with
        # the reentry never happening.
        session = self.storage.load_snapshot("joined").session
        self.assertEqual(session.mode, EnforcementMode.OPEN)
        self.assertIsNone(session.authorization_id)

    def test_one_failed_prompt_step_does_not_skip_the_others(self):
        """A step that fails is named; the steps after it still run.

        A single outer handler would deliver the prompt and skip everything
        after the failure, so an unrelated fault in advisory bookkeeping would
        leave the turn unobserved and the correction circuit armed - a session
        that can be spoken to but cannot end a turn. Each step stands alone.
        """

        self.register()
        with patch(
            "agent_handoff_toolkit.hook_adapters.worktree_digest",
            side_effect=OSError(errno.EACCES, "Permission denied"),
        ):
            output = self.invoke("UserPromptSubmit", turn_id="turn-9")
        self.assertEqual(output.exit_code, 0)
        self.assertEqual(output.stderr, "")
        data = json.loads(output.stdout)
        self.assertNotIn("decision", data)
        context = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("step:worktree-baseline", context)
        # The later step still ran: the turn is observed despite the failure.
        session = self.storage.load_snapshot("session-1").session
        self.assertEqual(session.current_external_user_turn_reference, "turn-9")

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

    def test_repeated_peer_advances_never_arm_the_circuit(self):
        """Only this session's own mistakes count toward the circuit."""

        self.register()
        path, text, message = self.record()
        self.invoke(last_assistant_message=message)
        self.invoke(
            "UserPromptSubmit",
            session_id="joined",
            prompt=render_resume_prompt(path, path.read_text(encoding="utf-8")),
        )
        previous_id = "first"
        for attempt in (1, 2, 3, 4):
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
            self.assert_block(result, "AHK-STOP-STALE")
            self.assertIn(
                "current=" + previous_id + ".md", json.loads(result.stdout)["reason"]
            )
            self.assertEqual(
                self.storage.load_snapshot("joined").session.correction_cycle_count,
                0,
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

    def test_block_feedback_names_the_validator_checks_that_failed(self):
        """A blocked record says which checks failed, in validator codes."""

        self.register()
        data = make_record("continuation", record_id="first", root="issue-invented")
        _, _, message = self.record(data=data)
        reason = json.loads(self.invoke(last_assistant_message=message).stdout)[
            "reason"
        ]
        self.assertIn("AHK-STOP-", reason)
        self.assertIn(" failed=", reason)
        codes = reason.split(" failed=", 1)[1].split()[0].split(",")
        self.assertTrue(codes)
        for code in codes:
            self.assertRegex(code, r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
        self.assertLessEqual(len(reason.encode()), 1200)

    def test_block_feedback_never_carries_record_or_message_content(self):
        self.register()
        secret = "Zq7PrivateObjectiveProse"
        data = make_record("continuation", record_id="first")
        data["sections"]["Objective"] = f"{secret} objective body."
        data["active_scopes"][0]["remaining_code_detail"] = f"{secret} detail."
        data["next_session_prompt"] = f"- {secret} prompt line"
        _, _, message = self.record(data=data)
        altered = f"{secret} handwritten preamble.\n\n{message}"
        reason = json.loads(self.invoke(last_assistant_message=altered).stdout)[
            "reason"
        ]
        self.assertIn("AHK-STOP-", reason)
        self.assertNotIn(secret, reason)
        self.assertLessEqual(len(reason.encode()), 1200)

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

    def test_malformed_identifiable_stop_fields_never_arm_the_circuit(self):
        self.register()
        for index, changes in enumerate(
            (
                {"stop_hook_active": "not-a-boolean"},
                {"transcript_path": 42},
                {"last_assistant_message": []},
                {"cwd": str(ROOT.parent)},
            ),
            1,
        ):
            output = self.invoke(**changes)
            self.assert_runtime_notice(output)
            self.assertIsNone(
                self.storage.load_snapshot("session-1").session.pending_correction_hmac
            )
            self.assertEqual(
                self.storage.load_snapshot("session-1").session.correction_cycle_count,
                0,
            )

    def test_the_copied_prompt_resumes_however_the_user_edits_it(self):
        """The record the pointer names is the evidence; the prose is not.

        Every one of these carries the pointer as its first line, so every
        one resumes the same chain. Only a prompt that does not open with the
        pointer leaves the session untracked, and that is the user asking for
        something else.
        """

        self.register()
        path, _, message = self.record()
        self.invoke(last_assistant_message=message)
        copied = message.split("```text\n", 1)[1].split("\n```", 1)[0]
        authorization = self.storage.load_snapshot("session-1").session.authorization_id
        for index, prompt in enumerate(
            (
                copied,
                "Continue from handoff: " + path.as_posix(),
                copied + "\nMore",
                copied.replace("Exact next action:", "Next:"),
            )
        ):
            with self.subTest(prompt=index):
                session = "other-" + str(index)
                self.invoke("UserPromptSubmit", session_id=session, prompt=prompt)
                self.assertEqual(
                    self.storage.load_snapshot(session).session.authorization_id,
                    authorization,
                )
        self.invoke(
            "UserPromptSubmit", session_id="prefixed", prompt="Please " + copied
        )
        self.assertEqual(
            self.storage.load_snapshot("prefixed").session.mode, EnforcementMode.OPEN
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
        self.assertEqual(fresh.session.mode, EnforcementMode.OPEN)
        self.assertIsNone(fresh.chain)
        self.assertIsNone(fresh.session.authorization_id)
        self.assertIsNotNone(fresh.session.bootstrap_challenge)
        self.assertEqual(self.storage.load_chain(completed.authorization_id), completed)
        # The fresh, re-entered session has no root, but ordinary work is
        # never gated regardless.
        self.assertEqual(self.invoke("PreToolUse"), HookExecution())
        self.assertEqual(self.invoke(), HookExecution())

    def test_a_host_prompt_never_reenters_a_completed_session(self):
        """A task notification after an audit is not the user starting work.

        Reentry used to take the notification's turn reference as the user's,
        which made the session eligible to enroll on the host's behalf and
        discarded the completed state the user had not left.
        """

        self.register()
        _, _, audit = self.record("completion-audit", "audit")
        self.assertEqual(self.invoke(last_assistant_message=audit), HookExecution())
        before = self.storage.load_snapshot("session-1").session
        self.assertIs(before.mode, EnforcementMode.COMPLETE)
        self.host_prompt()
        after = self.storage.load_snapshot("session-1").session
        self.assertIs(after.mode, EnforcementMode.COMPLETE)
        self.assertEqual(
            after.current_external_user_turn_reference,
            before.current_external_user_turn_reference,
        )
        self.assertEqual(after.authorization_id, before.authorization_id)
        self.assertEqual(after.turn_origin, "host")
        # The user's own next turn still reenters as before.
        self.invoke("UserPromptSubmit", turn_id="fresh-user", prompt="Next task.")
        fresh = self.storage.load_snapshot("session-1").session
        self.assertIs(fresh.mode, EnforcementMode.OPEN)
        self.assertEqual(fresh.current_external_user_turn_reference, "fresh-user")

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
        reason = json.loads(
            self.invoke(
                "PreToolUse",
                tool_input={
                    "command": f"python {(owned / 'runner.py').as_posix()} lifecycle"
                },
            ).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
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
        specific = json.loads(result.stdout)["hookSpecificOutput"]
        command = specific["updatedInput"]["command"]
        self.assertEqual(
            self.invoke("PreToolUse", tool_input={"command": command}), HookExecution()
        )
        self.invoke(
            "UserPromptSubmit", prompt="Continue this task.", turn_id="new-user"
        )
        repaired = self.invoke("PreToolUse", tool_input={"command": command})
        self.assertIn("updatedInput", repaired.stdout)

    def reason_of(self, execution):
        return json.loads(execution.stdout)["hookSpecificOutput"][
            "permissionDecisionReason"
        ]

    def test_a_lifecycle_invocation_inside_a_compound_command_is_intercepted(self):
        """`cd X && python ... lifecycle ...` used to reach the CLI unbound.

        The CLI then printed a bare `AHK-INPUT` and the agent flailed. A
        compound command is never run or rewritten; it is denied with the
        bound command for the invocation it contained, to be run alone.
        """

        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        bound = (
            f"python {runner} lifecycle one-off --session-key {session.session_key}"
            f" --challenge {capability}"
            f" --expected-session-revision {session.targeted_revision}"
        )
        for host in ("claude", "codex"):
            for command in (
                f'cd "{self.root.as_posix()}" && python {runner} lifecycle one-off',
                f"python {runner} lifecycle one-off | grep x",
                "echo x >> notes.txt; python .agent-handoff-toolkit/runner.py"
                " lifecycle one-off",
                bound + " && echo done",
                bound + " > out.txt",
                bound + " | head -n 5; rm x",
            ):
                with self.subTest(host=host, command=command):
                    output = self.invoke(
                        "PreToolUse", host=host, tool_input={"command": command}
                    )
                    reason = self.reason_of(output)
                    self.assertIn("AHK-PRE-ROOT", reason)
                    self.assertIn("alone", reason)
                    self.assertEqual(reason.split("Command: ", 1)[1], bound)
                    self.assertNotIn("updatedInput", output.stdout)
        self.assertEqual(
            self.invoke("PreToolUse", tool_input={"command": bound}), HookExecution()
        )

    def test_only_the_diagnosis_and_help_escape_the_broadened_interception(self):
        class Unopenable:
            def __getattr__(self, name):
                raise RuntimeError("state unavailable")

        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()

        def run(command):
            return run_hook(
                "claude",
                "PreToolUse",
                json.dumps(
                    payload(self.root, "PreToolUse", tool_input={"command": command})
                ),
                self.root,
                Unopenable(),
            )

        for command in (
            f"python {runner} lifecycle --help",
            f"python {runner} lifecycle -h",
            f"python {runner} lifecycle register-root --help",
            f"cd {self.root.as_posix()} && python {runner} lifecycle doctor",
            'git commit -m "python .agent-handoff-toolkit/runner.py lifecycle inspect"',
        ):
            with self.subTest(command=command):
                self.assertEqual(run(command), HookExecution())
        for command in (
            f"cd {self.root.as_posix()} && python {runner} lifecycle inspect",
            f"python {runner} lifecycle inspect 2>&1 | tail -3",
            f"echo x; python {runner} lifecycle one-off",
        ):
            with self.subTest(command=command):
                self.assertIn("AHK-HOOK-RUNTIME", run(command).stdout)

    def test_an_invocation_counts_only_at_a_command_start(self):
        """A mention of the runner inside an argument or a heredoc is not a call.

        Matching after any whitespace denied ordinary commands in every mode,
        `OPEN` included - a commit message or a notes file written through a
        heredoc that quoted the lifecycle command - and opened state for them.
        An invocation starts the command string or a line, or follows `;`,
        `&&`, `||`, `|`, `(`, `$(` or a backtick.
        """

        class Unopenable:
            def __getattr__(self, name):
                raise RuntimeError("state unavailable")

        def run(command):
            return run_hook(
                "claude",
                "PreToolUse",
                json.dumps(
                    payload(self.root, "PreToolUse", tool_input={"command": command})
                ),
                self.root,
                Unopenable(),
            )

        for command in (
            "git commit -F - <<'EOF'\nfix: bind inspect\n\n"
            "python .agent-handoff-toolkit/runner.py lifecycle inspect is now bound\n"
            "EOF",
            "cat > notes.md <<'EOF'\nRun python X/runner.py lifecycle inspect first.\n"
            "EOF\n",
            'cat <<-"END" > notes.md\n\tpython r lifecycle inspect\n\tEND',
            "cat <<END\npython r lifecycle inspect\nEND",
            'git commit -m "fix: python r lifecycle inspect is bound"',
            "echo python r lifecycle inspect",
            "grep -n python r lifecycle inspect notes.md",
            "echo a\\;python r lifecycle inspect",
            "git status # then; python r lifecycle inspect",
            "# python r lifecycle inspect",
            "ls\n  # && python r lifecycle inspect\n",
        ):
            with self.subTest(command=command):
                self.assertEqual(run(command), HookExecution())
        for command in (
            "echo `python r lifecycle inspect`",
            "echo $(python r lifecycle inspect)",
            "echo it\\'s; python r lifecycle inspect",
            "cd x\npython r lifecycle inspect",
            "cd x ||  python r lifecycle inspect",
            "cat <<'EOF' > notes.md\nbody\nEOF\npython r lifecycle inspect",
            "git status # note\npython r lifecycle inspect",
            "echo a#b; python r lifecycle inspect",
            "echo ${#x}; python r lifecycle inspect",
            "echo x\\ #; python r lifecycle inspect",
            "echo x\\\t#; python r lifecycle inspect",
            "echo x\\;#; python r lifecycle inspect",
            'echo "a"#; python r lifecycle inspect',
            "echo $(printf x)#x; python r lifecycle inspect",
            "echo `printf x`#x; python r lifecycle inspect",
            # Inside arithmetic, a parameter expansion or `[[`, a `#` after a
            # blank or `(` is not a comment, so the substitution still runs.
            "echo $((# $(python r lifecycle inspect)))",
            "(( 1 # $(python r lifecycle inspect) ))",
            "echo $[ 1 # $(python r lifecycle inspect) ]",
            "echo ${x:- #$(python r lifecycle inspect)}",
            "[[ a == # ]] ; python r lifecycle inspect",
            "(# x\npython r lifecycle inspect)",
            # After any bracket or backtick, telling a comment from a word is
            # left to the shell: the invocation is intercepted, never hidden.
            "echo $((1 + 2)) # ; python r lifecycle inspect",
            # Such a `#` may still start a real comment, whose quote must not
            # open a string that hides the next line.
            "echo [ # '\npython r lifecycle inspect",
            '(true) # it"s\npython r lifecycle inspect',
            "(true) # x <<EOF\npython r lifecycle inspect\nEOF",
            "(true) # x \\\npython r lifecycle inspect",
            "(# '\npython r lifecycle inspect\n)",
            "(# <<EOF\npython r lifecycle inspect\n)",
            "(# \\\npython r lifecycle inspect\n)",
            "(true)# '\npython r lifecycle inspect",
            "echo hi >#'\npython r lifecycle inspect",
            "echo hi <#x ; python r lifecycle inspect",
            # Quoted text after an ambiguous `#` is still masked in each
            # reading, so a quoted `--help` is not taken for a help flag.
            'echo [ # comment\npython r lifecycle inspect "x --help x"',
            'echo $((# $(python r lifecycle inspect "x -h x")))',
            # Past the fork limit the rest is left unmasked, not hidden.
            "echo [ " + "# '\n" * 8 + "python r lifecycle inspect",
            # Unmasked text proves no help flag or exempt operation.
            "echo [\n" + "# c\n" * 7 + 'python r lifecycle inspect "x --help x"',
        ):
            with self.subTest(command=command):
                self.assertIn("AHK-HOOK-RUNTIME", run(command).stdout)

    def test_a_heredoc_mention_passes_an_open_session(self):
        self.invoke("UserPromptSubmit")
        command = (
            "git commit -F - <<'EOF'\nfix: bind inspect\n\n"
            "python .agent-handoff-toolkit/runner.py lifecycle inspect is now bound\n"
            "EOF"
        )
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.assertEqual(
                    self.invoke(
                        "PreToolUse", host=host, tool_input={"command": command}
                    ),
                    HookExecution(),
                )

    def test_a_bound_invocation_with_an_output_trailer_is_admitted(self):
        """`2>&1`, `2>/dev/null` and a pipe into `head` or `tail` change nothing.

        They were classified compound and denied. The invocation alone is
        checked; any other trailer stays compound, and a trailer is never
        rewritten in place.
        """

        trailers = (
            " 2>&1",
            " 2>/dev/null",
            " | head -n 5",
            " | tail -5",
            " 2>&1 | tail -3",
            " | head -n5",
            " # record the decision",
            " 2>&1 # note; then continue",
        )
        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        one_off = (
            f"python {runner} lifecycle one-off --session-key {session.session_key}"
            f" --challenge {capability}"
            f" --expected-session-revision {session.targeted_revision}"
        )
        for trailer in trailers:
            with self.subTest(mode="pre-root", trailer=trailer):
                self.assertEqual(
                    self.invoke(
                        "PreToolUse", tool_input={"command": one_off + trailer}
                    ),
                    HookExecution(),
                )
        self.register()
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        bound = (
            f"python {runner} lifecycle inspect --session-key {session.session_key}"
            f" --challenge {capability}"
            f" --expected-session-revision {session.targeted_revision}"
        )
        for host in ("claude", "codex"):
            for trailer in trailers:
                with self.subTest(mode="tracked", host=host, trailer=trailer):
                    self.assertEqual(
                        self.invoke(
                            "PreToolUse",
                            host=host,
                            tool_input={"command": bound + trailer},
                        ),
                        HookExecution(),
                    )
            for trailer in (" | grep x", " > out.txt", " 2>&1 | tail -3 | sort"):
                with self.subTest(mode="tracked", host=host, other=trailer):
                    reason = self.reason_of(
                        self.invoke(
                            "PreToolUse",
                            host=host,
                            tool_input={"command": bound + trailer},
                        )
                    )
                    self.assertIn("alone", reason)
        stale = (
            f"python {runner} lifecycle inspect --session-key {session.session_key}"
            " --challenge spent-challenge --expected-session-revision 0"
        )
        for trailer in trailers:
            with self.subTest(stale=trailer):
                output = self.invoke(
                    "PreToolUse", host="claude", tool_input={"command": stale + trailer}
                )
                self.assertNotIn("updatedInput", output.stdout)
                reason = self.reason_of(output)
                self.assertIn("AHK-CONTROL-BINDING", reason)
                self.assertNotIn("alone", reason)
                self.assertEqual(
                    reason.split("Command: ", 1)[1].split()[:4],
                    ["python", runner, "lifecycle", "inspect"],
                )

    def test_a_stale_tracked_inspect_is_rebound_on_both_hosts(self):
        """A spent challenge cost a denial and a retry on every tracked pause.

        On Claude the hook substitutes the bound command through
        `updatedInput`, deciding nothing about permission. It does so only
        where it would otherwise deny and offer that very command.
        """

        self.register()
        session = self.storage.load_snapshot("session-1").session
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        stale = (
            f"python {runner} lifecycle inspect --session-key {session.session_key}"
            " --challenge spent-challenge --expected-session-revision 0"
        )
        tool_input = {"command": stale, "description": "Inspect lifecycle state"}
        output = self.invoke("PreToolUse", host="claude", tool_input=tool_input)
        specific = json.loads(output.stdout)["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", specific)
        self.assertNotIn("permissionDecisionReason", specific)
        updated = specific["updatedInput"]
        self.assertEqual(updated["description"], "Inspect lifecycle state")
        self.assertEqual(set(updated), {"command", "description"})
        self.assertNotEqual(updated["command"], stale)
        self.assertTrue(
            updated["command"].startswith(
                f"python {runner} lifecycle inspect --session-key {session.session_key}"
                " --challenge "
            )
        )
        # The substitute is exactly what the interception admits.
        self.assertEqual(
            self.invoke(
                "PreToolUse",
                host="claude",
                tool_input={"command": updated["command"]},
            ),
            HookExecution(),
        )
        # Codex requires an explicit allow alongside updatedInput.
        codex = self.invoke("PreToolUse", host="codex", tool_input=tool_input)
        specific = json.loads(codex.stdout)["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "allow")
        self.assertEqual(specific["updatedInput"], updated)
        # Anything beyond stale credentials is still denied, never rewritten.
        for command in (
            stale + " && echo done",
            f"cd {self.root.as_posix()} && " + stale,
            stale.replace(session.session_key, "b" * 64),
            stale.replace(runner, "/elsewhere/runner.py"),
            stale + " --extra value",
            stale.replace(" inspect ", " one-off "),
        ):
            with self.subTest(command=command):
                output = self.invoke(
                    "PreToolUse", host="claude", tool_input={"command": command}
                )
                self.assertNotIn("updatedInput", output.stdout)
                self.assertIn("AHK-CONTROL-BINDING", self.reason_of(output))

    def test_plain_inspect_recovers_without_a_denied_tool_call(self):
        for tracked in (False, True):
            if tracked:
                self.register()
            else:
                self.invoke("UserPromptSubmit")
            runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
            for host in ("claude", "codex"):
                with self.subTest(tracked=tracked, host=host):
                    output = self.invoke(
                        "PreToolUse",
                        host=host,
                        tool_input={"command": f"python {runner} lifecycle inspect"},
                    )
                    specific = json.loads(output.stdout)["hookSpecificOutput"]
                    self.assertNotEqual(specific.get("permissionDecision"), "deny")
                    command = specific["updatedInput"]["command"]
                    self.assertIn(" lifecycle inspect --session-key ", command)
                    self.assertEqual(
                        self.invoke(
                            "PreToolUse", host=host, tool_input={"command": command}
                        ),
                        HookExecution(),
                    )

    def test_resume_repairs_relative_record_and_flag_order_without_blocking(self):
        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        command = (
            f"python {runner} lifecycle resume --record handoffs/first.md"
            f" --session-key {session.session_key} --challenge old-challenge"
            " --expected-session-revision 0"
        )
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                output = self.invoke(
                    "PreToolUse", host=host, tool_input={"command": command}
                )
                specific = json.loads(output.stdout)["hookSpecificOutput"]
                self.assertNotEqual(specific.get("permissionDecision"), "deny")
                repaired = specific["updatedInput"]["command"]
                self.assertIn(" lifecycle resume ", repaired)
                self.assertIn(
                    f"--record {self.root.as_posix()}/handoffs/first.md", repaired
                )
                self.assertEqual(
                    self.invoke(
                        "PreToolUse", host=host, tool_input={"command": repaired}
                    ),
                    HookExecution(),
                )

    def test_resume_repair_never_rebinds_another_sessions_key(self):
        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        output = self.invoke(
            "PreToolUse",
            tool_input={
                "command": f"python {runner} lifecycle resume --record {self.root.as_posix()}/handoffs/first.md"
                f" --session-key {'b' * 64} --challenge old-challenge --expected-session-revision 0"
            },
        )
        self.assertNotIn("updatedInput", output.stdout)

    def test_a_plain_pre_root_one_off_is_bound_as_itself(self):
        self.invoke("UserPromptSubmit")
        session = self.storage.load_snapshot("session-1").session
        capability = self.storage.control_capability(session)
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                reason = self.reason_of(
                    self.invoke(
                        "PreToolUse",
                        host=host,
                        tool_input={"command": f"python {runner} lifecycle one-off"},
                    )
                )
                self.assertIn("AHK-PRE-ROOT", reason)
                self.assertNotIn("register-root", reason)
                self.assertNotIn("failed=", reason)
                command = reason.split("Command: ", 1)[1]
                self.assertEqual(
                    command,
                    f"python {runner} lifecycle one-off --session-key"
                    f" {session.session_key} --challenge {capability}"
                    f" --expected-session-revision {session.targeted_revision}",
                )
                self.assertEqual(
                    self.invoke(
                        "PreToolUse", host=host, tool_input={"command": command}
                    ),
                    HookExecution(),
                )

    def test_a_scope_kind_rejection_lists_the_valid_kinds(self):
        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        for kind in ("task", "ticket", "workstream"):
            with self.subTest(kind=kind):
                reason = self.reason_of(
                    self.invoke(
                        "PreToolUse",
                        tool_input={
                            "command": f"python {runner} lifecycle register-root"
                            f" --scope-id issue-1 --scope-kind {kind}"
                            ' --scope-title "A title" --scope-outcome "An outcome"'
                        },
                    )
                )
                self.assertIn("failed=scope-kind", reason)
                self.assertIn("unit, issue, phase, epic, rollout, standalone", reason)
                self.assertNotIn(kind, reason)


class SiblingWorktreeResumeTests(unittest.TestCase):
    """A record is read from the checkout of this repository that holds it.

    A session opened at a repository's main checkout was handed a pointer into
    one of its worktrees and refused it as `candidate-outside-handoffs`,
    leaving the session untracked. Another worktree of the same repository -
    the same git common directory - is the same repository; an unrelated
    clone is not.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        # Every checkout here is a child of the scratch directory, so the
        # ceiling is a strict ancestor of each and fences `git rev-parse`.
        fence = patch.dict(
            os.environ, {"GIT_CEILING_DIRECTORIES": self.scratch.as_posix()}
        )
        fence.start()
        self.addCleanup(fence.stop)
        self.main = self.scratch / "main"
        self.main.mkdir()
        self.git("init", "-q", cwd=self.main)
        self.git("commit", "-q", "--allow-empty", "-m", "synthetic", cwd=self.main)
        self.git("worktree", "add", "-q", "--detach", ".worktrees/wt", cwd=self.main)
        self.worktree = self.main / ".worktrees" / "wt"
        for checkout in (self.main, self.worktree):
            (checkout / "handoffs").mkdir()
        self.storage = LocalLifecycleStorage(
            self.main, state_root=self.scratch / "state"
        )

    @staticmethod
    def git(*arguments, cwd):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=cwd,
            capture_output=True,
            check=True,
        )

    def invoke(self, checkout, name, session, **changes):
        return run_hook(
            "codex",
            name,
            json.dumps(payload(checkout, name, session_id=session, **changes)),
            checkout,
            self.storage,
        )

    def publish_in_worktree(self):
        """Register a root in a worktree session and publish its first record."""

        self.assertEqual(
            self.invoke(self.worktree, "UserPromptSubmit", "in-worktree"),
            HookExecution(),
        )
        snapshot = self.storage.load_snapshot("in-worktree")
        scope = make_record("continuation", record_id="first")["active_scopes"][0]
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(scope["scope_definition"]))
            .decode()
            .rstrip("=")
        )
        LifecycleService(self.storage, "in-worktree").register_root(
            challenge=snapshot.session.bootstrap_challenge,
            scope_id="issue-1",
            scope_kind="issue",
            scope_definition_b64=encoded,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        path, text, message = self.record(self.worktree, "in-worktree", "first")
        self.assertEqual(
            self.invoke(
                self.worktree, "Stop", "in-worktree", last_assistant_message=message
            ),
            HookExecution(),
        )
        return path, text

    def record(self, checkout, session, name, predecessor=None):
        chain = self.storage.load_snapshot(session).chain
        data = make_record("continuation", record_id=name, predecessor=predecessor)
        data["authorization_id"] = chain.authorization_id
        data["authorization_evidence"] = {
            "kind": "initial-user-turn",
            "user_turn_ref": chain.authorization_user_turn_reference,
            "proposal_turn_ref": None,
            "evidence_hmac": chain.authorization_evidence_hmac,
        }
        text = render_record(data)
        path = checkout / "handoffs" / (name + ".md")
        path.write_bytes(text.encode())
        return path, text, render_terminal_response(path, text)

    def resume(self, session, path):
        output = self.invoke(
            self.main,
            "UserPromptSubmit",
            session,
            prompt="Continue from handoff: " + path.as_posix(),
        )
        return json.loads(output.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_a_record_in_another_worktree_of_this_repository_resumes(self):
        path, text = self.publish_in_worktree()
        context = self.resume("at-main", path)
        self.assertIn("AHK-RESUMED", context)
        resumed = self.storage.load_snapshot("at-main")
        self.assertIs(resumed.session.mode, EnforcementMode.TRACKED)
        self.assertEqual(
            resumed.session.authorization_id,
            self.storage.load_snapshot("in-worktree").session.authorization_id,
        )
        # The successor written here names its predecessor where it really
        # is, in the other worktree, and Stop reads it from there.
        _, _, successor = self.record(
            self.main,
            "at-main",
            "second",
            predecessor={
                "record_id": "first",
                "path": path.as_posix(),
                "sha256": record_digest(text),
            },
        )
        self.assertEqual(
            self.invoke(self.main, "Stop", "at-main", last_assistant_message=successor),
            HookExecution(),
        )
        self.assertEqual(
            self.storage.load_snapshot(
                "at-main"
            ).chain.current_record_reference.record_id,
            "second",
        )

    def test_a_record_outside_this_repository_is_refused_with_directions(self):
        path, text = self.publish_in_worktree()
        unrelated = self.scratch / "unrelated"
        unrelated.mkdir()
        self.git("init", "-q", cwd=unrelated)
        plain = self.scratch / "plain"
        for index, checkout in enumerate((unrelated, plain)):
            with self.subTest(checkout=checkout.name):
                (checkout / "handoffs").mkdir(parents=True)
                copy = checkout / "handoffs" / path.name
                copy.write_bytes(text.encode())
                context = self.resume("refused-" + str(index), copy)
                self.assertIn(
                    "AHK-RESUME-FAILED failed=candidate-outside-handoffs", context
                )
                self.assertIn("open the session in the checkout", context)
                self.assertIs(
                    self.storage.load_snapshot("refused-" + str(index)).session.mode,
                    EnforcementMode.OPEN,
                )

    def test_a_stop_candidate_in_another_worktree_is_still_refused(self):
        """Stop publishes only from this checkout's own `handoffs/`."""

        path, _ = self.publish_in_worktree()
        self.resume("at-main", path)
        _, _, message = self.record(self.worktree, "at-main", "second")
        result = self.invoke(
            self.main, "Stop", "at-main", last_assistant_message=message
        )
        reason = json.loads(result.stdout)["reason"]
        self.assertIn("AHK-STOP-POINTER", reason)
        self.assertIn("failed=pointer-outside-handoffs", reason)
        self.assertIn(" root=" + (self.main / "handoffs").as_posix(), reason)


if __name__ == "__main__":
    unittest.main()
