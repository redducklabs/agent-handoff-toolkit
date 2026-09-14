"""Ungated sessions run their work without interference."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import (  # noqa: E402
    HookExecution,
    run_lifecycle_hook,
)
from agent_handoff_toolkit.lifecycle import EnforcementMode  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
