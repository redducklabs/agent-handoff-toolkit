"""Ordinary host input must never fail the hook.

The hook parses a small, known set of fields. Everything else is opaque turn
content it passes through untouched: a tool call's payload is the host's, and
its size, shape and characters belong to the work being done, not to the
lifecycle. Rejecting any of it blocks real work and proves nothing, because no
host content ever reaches hook output.
"""

import base64
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
from agent_handoff_toolkit.lifecycle_operations import LifecycleService  # noqa: E402
from agent_handoff_toolkit.lifecycle_storage import LocalLifecycleStorage  # noqa: E402
from agent_handoff_toolkit.lineage import canonical_json_bytes  # noqa: E402

from test_hook_adapters import payload  # noqa: E402
from test_lifecycle import make_record  # noqa: E402


class HostInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        self.service = LifecycleService(self.storage, "session-1")
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

    def register(self):
        self.invoke("UserPromptSubmit")
        snapshot = self.storage.load_snapshot("session-1")
        scope = make_record("continuation", record_id="first")["active_scopes"][0]
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(scope["scope_definition"]))
            .decode()
            .rstrip("=")
        )
        self.service.register_root(
            challenge=snapshot.session.bootstrap_challenge,
            scope_id="issue-1",
            scope_kind="issue",
            scope_definition_b64=encoded,
            expected_session_revision=snapshot.session.targeted_revision,
        )

    def write(self, content):
        return self.invoke(
            tool_name="Write",
            tool_input={"file_path": str(self.root / "a.txt"), "content": content},
        )

    def assert_allowed(self, execution, because):
        self.assertEqual(execution, HookExecution(), because)

    def test_a_tracked_session_can_write_a_file_of_any_ordinary_size(self):
        self.register()
        for label, size in (
            ("1 KB", 1024),
            ("just over the old tool_input bound", 4200),
            ("a normal source file", 20_000),
            ("a large source file", 120_000),
            ("a generated file", 600_000),
        ):
            with self.subTest(label=label):
                self.assert_allowed(
                    self.write("x" * size), f"a {label} write must not fail the hook"
                )

    def test_a_tracked_session_can_write_ordinary_file_characters(self):
        """A tab is category Cc. Rejecting it rejects Go, Makefiles and more."""

        self.register()
        for label, content in (
            ("tab-indented source", "func main() {\n\tprintln(1)\n}\n"),
            ("a Makefile rule", "build:\n\tgo build ./...\n"),
            ("CRLF line endings", "line one\r\nline two\r\n"),
            ("a form feed page break", "part one\n\x0cpart two\n"),
            ("an ANSI escape in captured output", "\x1b[31mred\x1b[0m\n"),
            ("a zero-width joiner emoji", "# \U0001f469\u200d\U0001f4bb\n"),
            ("a NUL byte in binary-ish content", "a\x00b"),
            ("CJK text", "# \u65e5\u672c\u8a9e\n"),
        ):
            with self.subTest(label=label):
                self.assert_allowed(
                    self.write(content), f"{label} must not fail the hook"
                )

    def test_a_tracked_session_can_send_ordinary_tool_input_shapes(self):
        self.register()
        for label, tool_input in (
            ("300 edits", {"edits": [{"old": str(i), "new": "z"} for i in range(300)]}),
            ("200 keys", {f"k{index}": "v" for index in range(200)}),
            ("20 levels deep", json.loads('{"a":' * 20 + "1" + "}" * 20)),
        ):
            with self.subTest(label=label):
                self.assert_allowed(
                    self.invoke(tool_name="mcp__x__y", tool_input=tool_input),
                    f"tool input with {label} must not fail the hook",
                )

    def test_a_user_can_paste_a_large_message(self):
        """UserPromptSubmit fails closed, so rejecting a prompt rejects the user."""

        self.register()
        for label, prompt in (
            ("a 20 KB stack trace", "Here is the failure:\n" + "E   line\n" * 2000),
            ("a 300 KB log", "log:\n" + "x" * 300_000),
            ("a tab-indented snippet", "look at this:\n\tif x:\n\t\treturn 1\n"),
        ):
            with self.subTest(label=label):
                output = self.invoke("UserPromptSubmit", prompt=prompt)
                self.assertNotIn("AHK-HOOK-RUNTIME", output.stdout, label)

    def test_the_control_command_is_still_read_and_bound(self):
        """Relaxing opaque content must not relax what the hook actually parses."""

        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        # Still denied pre-root, and still recognised as a control attempt.
        denial = json.loads(
            self.invoke(
                tool_name="Bash",
                tool_input={"command": f"python {runner} lifecycle register-root"},
            ).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("AHK-PRE-ROOT", denial)
        # An oversized command is not a usable control command and must not be
        # parsed as one, but it also must not fail the hook.
        huge = self.invoke(
            tool_name="Bash",
            tool_input={"command": "python " + "x" * 20_000},
        )
        self.assertNotIn("AHK-HOOK-RUNTIME", huge.stdout)

    def test_host_content_still_never_reaches_hook_output(self):
        """The reason why opaque content needs no policing: it is never echoed."""

        self.register()
        sentinel = "sensitive-synthetic-sentinel"
        for event, changes in (
            ("PreToolUse", {"tool_input": {"command": sentinel, "x": sentinel}}),
            ("UserPromptSubmit", {"prompt": sentinel}),
            ("Stop", {"last_assistant_message": sentinel}),
        ):
            with self.subTest(event=event):
                output = self.invoke(event, **changes)
                self.assertNotIn(sentinel, output.stdout + output.stderr)
        self.assertNotIn(sentinel, self.storage.registry_path.read_text())


if __name__ == "__main__":
    unittest.main()
