"""Ungated sessions run their work without interference."""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import (  # noqa: E402
    HookExecution,
    run_lifecycle_hook,
)
from agent_handoff_toolkit.lifecycle import (  # noqa: E402
    ChainState,
    EnforcementMode,
    LifecycleMutation,
    RecordReference,
)
from agent_handoff_toolkit.lifecycle_operations import LifecycleService  # noqa: E402
from agent_handoff_toolkit.lifecycle_storage import LocalLifecycleStorage  # noqa: E402

from test_hook_adapters import payload  # noqa: E402


class UngatedSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        for name in ("handoffs", ".agent-handoff-toolkit", ".git"):
            (self.root / name).mkdir()
        (self.root / ".agent-handoff-toolkit" / "runner.py").write_text(
            "# owned runner\n"
        )

    def invoke(self, event="PreToolUse", **changes):
        return run_lifecycle_hook(
            "claude",
            event,
            json.dumps(payload(self.root, event, **changes)),
            self.root,
            self.storage,
        )

    def test_a_session_with_no_root_is_never_gated(self):
        """Every one of these was denied with AHK-PRE-ROOT before v0.4.0."""

        self.invoke("UserPromptSubmit")
        for label, tool, tool_input in (
            ("git status", "Bash", {"command": "git status"}),
            ("create a ticket", "Bash", {"command": "gh issue create -t x"}),
            ("run the tests", "Bash", {"command": "pytest -q"}),
            ("read-only MCP query", "mcp__db__query", {"sql": "select 1"}),
            ("fetch a page", "WebFetch", {"url": "https://example.invalid"}),
            ("search the web", "WebSearch", {"query": "x"}),
            ("launch a subagent", "Task", {"prompt": "x"}),
            ("write a todo list", "TodoWrite", {"todos": []}),
            ("read a file", "Read", {"file_path": "a"}),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    self.invoke(tool_name=tool, tool_input=tool_input),
                    HookExecution(),
                    f"{label} must not be gated",
                )

    def test_a_tracked_session_still_enforces_and_repairs_control_commands(self):
        import base64

        from agent_handoff_toolkit.lifecycle import EnforcementMode
        from agent_handoff_toolkit.lifecycle_operations import LifecycleService
        from agent_handoff_toolkit.lineage import canonical_json_bytes

        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        # The plain-slot form added in v0.3.2 still round-trips through the hook.
        attempt = (
            f"python {runner} lifecycle register-root --scope-id issue-1"
            ' --scope-kind issue --scope-title "A tracked scope"'
            ' --scope-outcome "The tracked scope is complete."'
        )
        reason = json.loads(
            self.invoke(tool_name="Bash", tool_input={"command": attempt}).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        formed = reason.split("Command: ", 1)[1].strip()
        self.assertIn("--scope-definition-b64", formed)
        self.assertEqual(
            self.invoke(tool_name="Bash", tool_input={"command": formed}),
            HookExecution(),
        )
        # Register for real, then confirm a tracked stop still blocks.
        service = LifecycleService(self.storage, "session-1")
        snapshot = self.storage.load_snapshot("session-1")
        definition = {
            "title": "A tracked scope",
            "outcome": "The tracked scope is complete.",
        }
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(definition))
            .decode()
            .rstrip("=")
        )
        service.register_root(
            challenge=snapshot.session.bootstrap_challenge,
            scope_id="issue-1",
            scope_kind="issue",
            scope_definition_b64=encoded,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertIs(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.TRACKED,
        )
        blocked = self.invoke("Stop", last_assistant_message="I am done.")
        self.assertIn("AHK-STOP-WORK", json.loads(blocked.stdout)["reason"])

    def test_one_off_records_the_declaration_and_grants_nothing(self):
        self.invoke("UserPromptSubmit")
        service = LifecycleService(self.storage, "session-1")
        snapshot = self.storage.load_snapshot("session-1")
        result = service.one_off(
            challenge=snapshot.session.bootstrap_challenge,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertEqual(result, {"mode": "one-off"})
        session = self.storage.load_snapshot("session-1").session
        self.assertIs(session.mode, EnforcementMode.ONE_OFF)
        self.assertIsNone(session.authorization_id)
        self.assertIsNone(session.chain_revision)

    def write(self, name="a.py"):
        return self.invoke(
            tool_name="Write",
            tool_input={"file_path": str(self.root / name), "content": "x = 1\n"},
        )

    def test_the_first_repository_write_advises_once_and_decides_nothing(self):
        """The notice must not carry a permission decision of any kind.

        `permissionDecision: "allow"` does not merely decline to block: on a
        real host it also satisfies the permission gate, so the write would
        proceed without the approval the user would otherwise be asked for.
        An advisory may not grant an approval nobody gave it, so the payload
        is a bare `systemMessage` and the host's permission flow runs
        untouched.
        """

        self.invoke("UserPromptSubmit")
        first = self.write()
        self.assertEqual(first.exit_code, 0)
        payload_out = json.loads(first.stdout)
        self.assertEqual(set(payload_out), {"systemMessage"})
        self.assertNotIn("hookSpecificOutput", payload_out)
        message = payload_out["systemMessage"]
        self.assertIn("lifecycle one-off", message)
        self.assertIn("lifecycle register-root", message)
        # It fires once, then never again.
        self.assertEqual(self.write("b.py"), HookExecution())

    def test_the_advisory_survives_a_runner_path_longer_than_the_feedback_bound(
        self,
    ):
        """A long repository root must not silence the notice.

        The message runs roughly 885 bytes plus twice the runner path, so
        MAX_REASON_BYTES - the bound on blocking *feedback* - used to drop it
        entirely past a repository root of about 145 characters, and rebuild
        it on every write call thereafter because the once-per-session flag
        was committed only after the length check.
        """

        from agent_handoff_toolkit import hook_adapters

        deep = self.root
        for _ in range(6):
            deep = deep / ("d" * 30)
        deep.mkdir(parents=True)
        long_runner = deep / "runner.py"
        long_runner.write_text("# owned runner\n")
        self.assertGreater(len(long_runner.as_posix()), 184)

        original = hook_adapters._control_command

        def with_long_runner(runner, *args, **kwargs):
            return original(long_runner, *args, **kwargs)

        self.invoke("UserPromptSubmit")
        with unittest.mock.patch.object(
            hook_adapters, "_control_command", with_long_runner
        ):
            first = self.write()
        message = json.loads(first.stdout)["systemMessage"]
        self.assertGreater(len(message.encode()), hook_adapters.MAX_REASON_BYTES)
        self.assertIn("lifecycle one-off", message)
        # The flag was still committed, so it never rebuilds.
        self.assertTrue(
            self.storage.load_snapshot("session-1").session.write_advisory_emitted
        )
        self.assertEqual(self.write("b.py"), HookExecution())

    def test_bash_never_triggers_the_advisory(self):
        self.invoke("UserPromptSubmit")
        self.assertEqual(
            self.invoke(tool_name="Bash", tool_input={"command": "git commit -m x"}),
            HookExecution(),
        )

    def test_stop_notes_unfinished_work_and_never_blocks(self):
        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        # Nothing changed: silent.
        self.assertEqual(self.invoke("Stop"), HookExecution())
        # The session dirties the tree.
        (self.root / "changed.py").write_text("x = 1\n")
        output = self.invoke("Stop")
        self.assertEqual(output.exit_code, 0)
        body = json.loads(output.stdout)
        message = body["systemMessage"]
        self.assertIn("AHK-NO-HANDOFF", message)
        # The mechanism sees a changed tree, not who changed it.
        self.assertIn("The repository changed during this session", message)
        self.assertNotIn("This session changed the repository", message)
        self.assertNotIn("you changed", message)
        self.assertNotIn("decision", body)
        self.assertNotIn("continue", body)

    def test_reentry_after_a_completed_chain_keeps_the_stop_backstop(self):
        """A COMPLETE -> OPEN reentry must not blind the backstop for a turn.

        The reentry mutation builds a fresh SessionState. It used to set only
        the four chain-identity fields, so the worktree baseline and the
        once-per-session advisory flag - which describe the host session, not
        the chain - silently fell back to their defaults.
        """

        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        self.write()
        opened = self.storage.load_snapshot("session-1").session
        self.assertIsNotNone(opened.worktree_baseline)
        self.assertTrue(opened.write_advisory_emitted)

        # Land the session where a published completion audit leaves it: a
        # tracked chain registered, then completed. The reentry path the fix
        # covers is only reachable from a COMPLETE session over a complete
        # chain, and a chain has to start active at revision one.
        record = RecordReference(
            "record-1", (self.root / "record-1.md").as_posix(), "a" * 64
        )
        active = ChainState("auth-a", "root-a", ("a" * 64,), 1, "active", record)
        tracked = self.storage.compare_and_swap(
            "session-1",
            0,
            opened.targeted_revision,
            LifecycleMutation(
                replace(
                    opened,
                    targeted_revision=opened.targeted_revision + 1,
                    mode=EnforcementMode.TRACKED,
                    authorization_id="auth-a",
                    chain_revision=1,
                ),
                active,
            ),
        ).session
        self.storage.compare_and_swap(
            "session-1",
            1,
            tracked.targeted_revision,
            LifecycleMutation(
                replace(
                    tracked,
                    targeted_revision=tracked.targeted_revision + 1,
                    mode=EnforcementMode.COMPLETE,
                    chain_revision=2,
                ),
                replace(active, status="complete", targeted_revision=2),
            ),
        )

        # Re-enter with a fresh external user turn.
        self.invoke("UserPromptSubmit", turn_id="turn-2")
        reentered = self.storage.load_snapshot("session-1").session
        self.assertIs(reentered.mode, EnforcementMode.OPEN)
        self.assertEqual(reentered.worktree_baseline, opened.worktree_baseline)
        self.assertTrue(reentered.write_advisory_emitted)

        # The backstop still sees the change the reentry turn makes.
        (self.root / "changed.py").write_text("x = 1\n")
        output = self.invoke("Stop")
        self.assertEqual(output.exit_code, 0)
        self.assertIn("AHK-NO-HANDOFF", json.loads(output.stdout)["systemMessage"])

    def test_stop_reads_the_worktree_with_exactly_one_git_status(self):
        """The note costs one subprocess per Stop, not one per fact."""

        import shutil
        import subprocess
        from unittest.mock import patch

        from agent_handoff_toolkit import repository_state

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        (self.root / "changed.py").write_text("x = 1\n")

        real = repository_state.subprocess.run
        calls = []

        def counted(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        with patch.object(repository_state.subprocess, "run", counted):
            output = self.invoke("Stop")
        self.assertIn("AHK-NO-HANDOFF", json.loads(output.stdout)["systemMessage"])
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0], ["git", "status", "--porcelain"])

    def test_the_unfinished_work_note_fires_once_for_the_whole_session(self):
        """It used to fire at every turn end while the tree stayed dirty.

        The design spec placed this note at the end of a session. Firing it
        per turn cost roughly fifty tokens of user-facing noise every time
        the agent stopped talking, and said nothing new the second time.
        """

        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        (self.root / "changed.py").write_text("x = 1")
        first = self.invoke("Stop")
        self.assertIn("AHK-NO-HANDOFF", json.loads(first.stdout)["systemMessage"])
        self.assertEqual(self.invoke("Stop"), HookExecution())
        self.assertEqual(self.invoke("Stop"), HookExecution())
        self.assertTrue(
            self.storage.load_snapshot("session-1").session.no_handoff_note_emitted
        )

    def test_a_later_user_turn_does_not_rearm_the_unfinished_work_note(self):
        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        (self.root / "changed.py").write_text("x = 1")
        self.assertIn(
            "AHK-NO-HANDOFF", json.loads(self.invoke("Stop").stdout)["systemMessage"]
        )
        self.invoke("UserPromptSubmit", turn_id="turn-2")
        (self.root / "changed-again.py").write_text("x = 2")
        self.assertEqual(self.invoke("Stop"), HookExecution())

    def test_a_stop_after_the_note_costs_no_git_subprocess(self):
        """Once the note has fired there is nothing left for git status to decide."""

        import shutil
        import subprocess
        from unittest.mock import patch

        from agent_handoff_toolkit import repository_state

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        (self.root / "changed.py").write_text("x = 1")
        self.invoke("Stop")
        real = repository_state.subprocess.run
        calls = []

        def counted(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        with patch.object(repository_state.subprocess, "run", counted):
            self.assertEqual(self.invoke("Stop"), HookExecution())
        self.assertEqual(calls, [])

    def test_the_unfinished_work_note_reads_as_plain_english(self):
        """It is user-facing text, so it asks the user for something concrete."""

        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        (self.root / "changed.py").write_text("x = 1")
        message = json.loads(self.invoke("Stop").stdout)["systemMessage"]
        self.assertIn("ask me to create a handoff", message)
        self.assertNotIn("register a root", message)
        self.assertNotIn("render a continuation", message)

    def test_stop_is_silent_when_the_tree_was_already_dirty(self):
        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        (self.root / "preexisting.py").write_text("x = 1\n")
        self.invoke("UserPromptSubmit")
        self.assertEqual(self.invoke("Stop"), HookExecution())

    def test_stop_is_silent_when_a_dirty_baseline_is_committed_clean(self):
        """The digest differs from baseline, but the tree is clean: silent.

        This is the mirror of test_stop_notes_unfinished_work_and_never_blocks:
        there the digest differs *and* the tree is dirty, so the note fires
        without ever needing to consult is_dirty (a dirty git-status output is
        never empty). Here the baseline itself was dirty (a preexisting
        untracked file), the session commits it, and the tree goes clean. The
        digest still differs from the baseline (an empty "?? file" status is
        not the same string as an empty status), so only is_dirty(root)
        returning False keeps this silent.
        """

        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.root, check=True
        )
        (self.root / "preexisting.py").write_text("x = 1\n")
        self.invoke("UserPromptSubmit")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit preexisting work", "--quiet"],
            cwd=self.root,
            check=True,
        )
        self.assertEqual(self.invoke("Stop"), HookExecution())


if __name__ == "__main__":
    unittest.main()
