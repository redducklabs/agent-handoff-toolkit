"""The context-pressure advisory: advisory only, never a decision.

Transcripts here are synthetic and hold structure and numbers only. No prompt,
reply or tool content appears in any fixture or generated line.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import HookExecution  # noqa: E402
from agent_handoff_toolkit.hooks import run_hook  # noqa: E402
from agent_handoff_toolkit.lifecycle import EnforcementMode  # noqa: E402
from agent_handoff_toolkit.lifecycle_operations import LifecycleService  # noqa: E402
from agent_handoff_toolkit.lifecycle_storage import LocalLifecycleStorage  # noqa: E402
from agent_handoff_toolkit.lineage import canonical_json_bytes  # noqa: E402

from test_hook_adapters import payload  # noqa: E402
from test_lifecycle import make_record  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "context-transcript.jsonl"


def usage(count, *, model="claude-opus-5-5", sidechain=False):
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": count - 1002,
                "cache_creation_input_tokens": 1000,
            },
        },
    }


def boundary(pre_tokens):
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"preTokens": pre_tokens},
    }


class ContextAdvisoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        fence = patch.dict(
            os.environ, {"GIT_CEILING_DIRECTORIES": self.root.parent.as_posix()}
        )
        fence.start()
        self.addCleanup(fence.stop)
        clear = patch.dict(os.environ)
        clear.start()
        self.addCleanup(clear.stop)
        os.environ.pop("AHK_CONTEXT_WINDOW_TOKENS", None)
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        for name in ("handoffs", ".agent-handoff-toolkit", ".git"):
            (self.root / name).mkdir()
        (self.root / ".agent-handoff-toolkit" / "runner.py").write_text(
            "# owned runner\n"
        )
        self.transcript = self.root / "transcript.jsonl"

    def invoke(self, name="PreToolUse", host="claude", storage=None, **changes):
        return run_hook(
            host,
            name,
            json.dumps(payload(self.root, name, **changes)),
            self.root,
            storage or self.storage,
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
        LifecycleService(self.storage, "session-1").register_root(
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

    def write(self, *entries, append=False):
        with self.transcript.open("a" if append else "w", newline="\n") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def advisory(self, output):
        if output == HookExecution():
            return None
        data = json.loads(output.stdout)
        self.assertEqual(set(data), {"hookSpecificOutput"})
        specific = data["hookSpecificOutput"]
        self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        return specific["additionalContext"]

    def test_the_fixture_holds_structure_and_numbers_only(self):
        allowed = {"type", "subtype", "model"}

        def walk(value, key=None):
            if isinstance(value, dict):
                for name, item in value.items():
                    walk(item, name)
            elif isinstance(value, list):
                for item in value:
                    walk(item, key)
            elif isinstance(value, str):
                self.assertIn(key, allowed)
            else:
                self.assertIsInstance(value, (int, bool))

        for line in FIXTURE.read_text().splitlines():
            walk(json.loads(line))

    def test_the_field_shape_fires_once_in_a_tracked_session(self):
        """A 1M session that compacted at 967k, now at 85%."""

        self.register()
        shutil.copyfile(FIXTURE, self.transcript)
        message = self.advisory(self.invoke())
        self.assertIsNotNone(message)
        self.assertTrue(message.startswith("AHK-CONTEXT-HIGH:"), message)
        self.assertIn("80%", message)
        self.assertIn("handoff", message)
        self.assertNotIn(self.root.as_posix(), message)
        self.assertEqual(self.invoke(), HookExecution())
        self.assertEqual(
            self.storage.load_snapshot("session-1").session.context_advisory_offset,
            self.transcript.stat().st_size,
        )

    def test_a_write_tool_call_also_carries_it(self):
        self.register()
        self.write(usage(170_000))
        message = self.advisory(
            self.invoke(tool_name="Write", tool_input={"file_path": "a.py"})
        )
        self.assertIn("AHK-CONTEXT-HIGH", message)

    def test_below_the_threshold_it_is_silent(self):
        self.register()
        self.write(usage(150_000))
        self.assertEqual(self.invoke(), HookExecution())
        self.assertIsNone(
            self.storage.load_snapshot("session-1").session.context_advisory_offset
        )

    def test_the_default_window_is_200k(self):
        self.register()
        self.write(usage(170_000))
        self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))

    def test_a_1m_model_marker_selects_the_1m_window(self):
        self.register()
        self.write(usage(170_000, model="claude-opus-5-5[1m]"))
        self.assertEqual(self.invoke(), HookExecution())
        self.write(usage(810_000, model="claude-opus-5-5[1m]"))
        self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))

    def test_any_large_reading_in_the_tail_selects_the_1m_window(self):
        self.register()
        self.write(usage(250_000), usage(170_000))
        self.assertEqual(self.invoke(), HookExecution())

    def test_sidechain_and_synthetic_entries_are_skipped(self):
        self.register()
        self.write(
            usage(170_000),
            usage(10, sidechain=True),
            {
                "type": "assistant",
                "isSidechain": False,
                "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}},
            },
        )
        self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))
        # A subagent's large context is not this session's.
        self.write(usage(50_000), usage(190_000, sidechain=True))
        self.assertEqual(self.invoke(), HookExecution())

    def test_the_environment_names_the_window(self):
        self.register()
        self.write(usage(85_000))
        with patch.dict(os.environ, {"AHK_CONTEXT_WINDOW_TOKENS": "100000"}):
            self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))

    def test_an_invalid_environment_window_is_ignored(self):
        self.register()
        self.write(usage(85_000))
        for value in ("abc", "0", "-5", "1e6", ""):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"AHK_CONTEXT_WINDOW_TOKENS": value}):
                    self.assertEqual(self.invoke(), HookExecution())

    def test_it_rearms_once_context_falls_below_half(self):
        self.register()
        self.write(usage(170_000))
        self.assertIsNotNone(self.advisory(self.invoke()))
        self.write(usage(175_000), append=True)
        self.assertEqual(self.invoke(), HookExecution())
        # Not below half: still silent.
        self.write(usage(120_000), usage(180_000), append=True)
        self.assertEqual(self.invoke(), HookExecution())
        # Compaction drops it below half; climbing back fires again.
        self.write(boundary(185_000), usage(40_000), usage(170_000), append=True)
        self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))
        self.assertEqual(self.invoke(), HookExecution())

    def test_a_replaced_transcript_rearms(self):
        self.register()
        self.write(*[usage(100_000 + n) for n in range(50)], usage(170_000))
        self.assertIsNotNone(self.advisory(self.invoke()))
        self.write(usage(170_000))
        self.assertIsNotNone(self.advisory(self.invoke()))

    def test_an_untracked_session_never_sees_it(self):
        self.assertEqual(self.invoke("UserPromptSubmit"), HookExecution())
        self.write(usage(190_000))
        self.assertEqual(self.invoke(), HookExecution())

    def test_low_context_never_opens_lifecycle_state(self):
        class Unopenable:
            def __getattr__(self, name):
                raise AssertionError("lifecycle state opened below the threshold")

        self.write(*[usage(100_000) for _ in range(20)])
        self.assertEqual(self.invoke(storage=Unopenable()), HookExecution())

    def test_any_failure_produces_no_message_and_no_decision(self):
        class Unopenable:
            def __getattr__(self, name):
                raise RuntimeError("state unavailable")

        self.register()
        self.write(usage(190_000))
        # State that cannot be opened: the call proceeds exactly as it did
        # before the advisory existed, with no runtime report and no denial.
        self.assertEqual(self.invoke(storage=Unopenable()), HookExecution())
        for content in (
            b"\x00\xff\xfe not json\n",
            b'{"type":"assistant","isSidechain":false,"message":{"usage":{"input_tokens":"x"}}}\n',
            b'{"type":"assistant","isSidechain":false,"message":{"model":"m","usage":{"input_tokens":true}}}\n',
            b"[1, 2, 3]\n",
        ):
            with self.subTest(content=content):
                self.transcript.write_bytes(content)
                self.assertEqual(self.invoke(), HookExecution())
        self.transcript.unlink()
        self.transcript.mkdir()
        self.assertEqual(self.invoke(), HookExecution())

    def test_an_ordinary_call_opens_state_only_around_the_crossing(self):
        """Opening state costs a git subprocess and a lock on every call.

        Once the last few main-thread readings are all past the threshold, an
        ordinary call returns before state is opened, as it does below it.
        """

        class Unopenable:
            opened = False

            def __getattr__(self, name):
                Unopenable.opened = True
                raise RuntimeError("state unavailable")

        self.register()
        self.write(*[usage(170_000 + n) for n in range(5)])
        self.assertEqual(self.invoke(storage=Unopenable()), HookExecution())
        self.assertFalse(Unopenable.opened)
        # Still within the crossing: state is opened (and here fails silently).
        self.write(usage(150_000), *[usage(170_000 + n) for n in range(4)])
        self.assertEqual(self.invoke(storage=Unopenable()), HookExecution())
        self.assertTrue(Unopenable.opened)

    def test_a_write_still_carries_it_past_the_crossing(self):
        """A writing tool opens state anyway, so a late check costs nothing."""

        self.register()
        self.write(*[usage(170_000 + n) for n in range(6)])
        self.assertEqual(self.invoke(), HookExecution())
        message = self.advisory(
            self.invoke(tool_name="Edit", tool_input={"file_path": "a.py"})
        )
        self.assertIn("AHK-CONTEXT-HIGH", message)

    def test_stop_never_carries_it(self):
        """Stop has no non-blocking channel the model reads; a block is not one."""

        self.register()
        self.write(usage(190_000))
        output = self.invoke("Stop")
        self.assertNotIn("AHK-CONTEXT-HIGH", output.stdout)
        self.assertIsNone(
            self.storage.load_snapshot("session-1").session.context_advisory_offset
        )

    def test_codex_never_reads_the_transcript(self):
        self.register()
        self.write(usage(190_000))
        self.assertEqual(self.invoke(host="codex"), HookExecution())

    def test_the_read_is_bounded_to_the_tail(self):
        """Evidence of a 1M window older than the bounded tail is not seen."""

        self.register()
        padding = {"type": "user", "isSidechain": False, "pad": [0] * 2000}
        self.write(boundary(967_000), *[padding for _ in range(160)], usage(170_000))
        self.assertGreater(self.transcript.stat().st_size, 600 * 1024)
        self.assertIn("AHK-CONTEXT-HIGH", self.advisory(self.invoke()))


if __name__ == "__main__":
    unittest.main()
