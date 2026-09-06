from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hooks import (  # noqa: E402
    observe_context,
    run_hook,
    select_milestone,
)


def hook_context(output: str) -> str:
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


class ContextHealthTests(unittest.TestCase):
    def test_selects_highest_crossed_milestone(self) -> None:
        cases = (
            (49.9, None),
            (50, 50),
            (59.9, 50),
            (60, 60),
            (69.9, 60),
            (70, 70),
            (100, 70),
        )
        for percentage, expected in cases:
            with self.subTest(percentage=percentage):
                self.assertEqual(select_milestone(percentage), expected)

    def test_rejects_invalid_explicit_percentages(self) -> None:
        for percentage in (-0.1, 100.1, math.nan, math.inf, True):
            with self.subTest(percentage=percentage), self.assertRaises(ValueError):
                select_milestone(percentage)

    def test_notifies_once_per_crossed_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            self.assertIn("50%", observe_context("session-a", 52, state_dir) or "")
            self.assertIsNone(observe_context("session-a", 58, state_dir))
            self.assertIn("60%", observe_context("session-a", 62, state_dir) or "")
            self.assertIsNone(observe_context("session-a", 69, state_dir))
            self.assertIn("70%", observe_context("session-a", 71, state_dir) or "")

    def test_compaction_rearms_lower_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            self.assertIn("70%", observe_context("session-a", 72, state_dir) or "")
            self.assertIsNone(observe_context("session-a", 20, state_dir))
            self.assertIn("50%", observe_context("session-a", 51, state_dir) or "")

    def test_state_is_hashed_session_keyed_and_leaves_no_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            observe_context("private-session-a", 51, state_dir)
            observe_context("private-session-b", 61, state_dir)

            files = sorted(state_dir.iterdir())
            expected_names = sorted(
                f"{hashlib.sha256(session.encode('utf-8')).hexdigest()}.json"
                for session in ("private-session-a", "private-session-b")
            )
            self.assertEqual([path.name for path in files], expected_names)
            self.assertNotIn(
                "private-session", "".join(path.read_text() for path in files)
            )
            self.assertEqual(
                {
                    json.loads(path.read_text(encoding="utf-8"))["milestone"]
                    for path in files
                },
                {50, 60},
            )


class HostHookTests(unittest.TestCase):
    def test_session_start_injects_contract_and_continuation_reminder(self) -> None:
        for platform in ("claude", "codex"):
            with self.subTest(platform=platform):
                output = run_hook(platform, "session-start", "{}", ROOT)
                context = hook_context(output)
                self.assertIn("docs/contract.md", context)
                self.assertIn("continuation", context.lower())
                self.assertEqual(
                    json.loads(output)["hookSpecificOutput"]["hookEventName"],
                    "SessionStart",
                )

    def test_claude_post_tool_use_recognizes_handoff_file_paths(self) -> None:
        raw = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "handoffs/current.md"},
            }
        )
        output = run_hook("claude", "post-tool-use", raw, ROOT)
        context = hook_context(output)
        self.assertIn("handoffs/current.md", context.replace("\\", "/"))
        self.assertIn("validate", context.lower())

    def test_claude_post_tool_use_ignores_non_handoff_files(self) -> None:
        raw = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "docs/notes.md"},
            }
        )
        self.assertEqual(run_hook("claude", "post-tool-use", raw, ROOT), "")

    def test_codex_apply_patch_recognizes_add_update_and_delete_directives(
        self,
    ) -> None:
        patch = """*** Begin Patch
*** Add File: handoffs/new.md
+new
*** Update File: handoffs/current.md
@@
-old
+new
*** Delete File: handoffs/obsolete.md
*** End Patch
"""
        raw = json.dumps({"tool_name": "apply_patch", "tool_input": {"command": patch}})
        context = hook_context(run_hook("codex", "post-tool-use", raw, ROOT))
        for path in (
            "handoffs/new.md",
            "handoffs/current.md",
            "handoffs/obsolete.md",
        ):
            with self.subTest(path=path):
                self.assertIn(path, context.replace("\\", "/"))

    def test_codex_patch_parsing_is_bounded(self) -> None:
        oversized = (
            "*** Begin Patch\n*** Update File: handoffs/current.md\n"
            + ("x" * (64 * 1024))
            + "\n*** End Patch\n"
        )
        raw = json.dumps(
            {"tool_name": "apply_patch", "tool_input": {"command": oversized}}
        )
        self.assertEqual(run_hook("codex", "post-tool-use", raw, ROOT), "")

    def test_automatic_hooks_fail_open_for_malformed_inputs(self) -> None:
        malformed_inputs = ("", "{", "[]", '"text"')
        for platform in ("claude", "codex"):
            for raw in malformed_inputs:
                with self.subTest(platform=platform, raw=raw):
                    self.assertEqual(
                        run_hook(platform, "post-tool-use", raw, ROOT),
                        "",
                    )

    def test_hook_does_not_use_undocumented_context_telemetry(self) -> None:
        raw = json.dumps(
            {
                "session_id": "session-a",
                "context_window": {"used_percentage": 71},
            }
        )
        context = hook_context(run_hook("codex", "session-start", raw, ROOT))
        self.assertNotIn("71", context)
        self.assertNotIn("70%", context)


if __name__ == "__main__":
    unittest.main()
