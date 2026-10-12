import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_handoff_toolkit.cli import main, _parser  # noqa: E402
from agent_handoff_toolkit import render_record  # noqa: E402
from test_records import make_v2_audit  # noqa: E402


class ReviewCliTests(unittest.TestCase):
    def test_help_and_status_mapping(self):
        self.assertIn("review", _parser().format_help())
        for status, expected in (
            ("pass", 0),
            ("disabled", 0),
            ("fail", 1),
            ("unavailable", 1),
            ("error", 2),
        ):
            output = io.StringIO()
            with (
                patch(
                    "agent_handoff_toolkit.review.review_record",
                    return_value={"status": status},
                ) as review,
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "review",
                        "candidate.md",
                        "--evidence",
                        "evidence.json",
                        "--project-root",
                        ".",
                    ]
                )
            self.assertEqual(code, expected)
            self.assertEqual(output.getvalue(), json.dumps({"status": status}) + "\n")
            review.assert_called_once_with(
                Path("candidate.md"), Path("evidence.json"), Path(".")
            )

    def test_real_disabled_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.md"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["review", str(candidate), "--project-root", directory])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "disabled")
            (root / ".agent-handoff-toolkit").mkdir()
            (root / ".agent-handoff-toolkit" / "jev.json").write_text(
                '{"enabled":true}'
            )
            candidate.write_text(render_record(make_v2_audit()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["review", str(candidate), "--project-root", directory])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "fail")

    def test_provider_import_blocker_leaves_existing_commands_and_hooks_operational(
        self,
    ):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            candidate = root / "candidate.md"
            candidate.write_text(render_record(make_v2_audit()), encoding="utf-8")
            script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('agent_handoff_toolkit.jev', 'agent_handoff_toolkit.review', 'agent_handoff_toolkit.review_config', 'agent_handoff_toolkit.review_rubrics'):
            raise ImportError('blocked optional review')
sys.meta_path.insert(0, Block())
from agent_handoff_toolkit.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
            env = dict(
                os.environ,
                PYTHONPATH=str(ROOT / "src"),
                GIT_CEILING_DIRECTORIES=str(root.parent),
            )
            for arguments, input_text in (
                (["validate", str(candidate)], ""),
                (["render-tail", str(candidate)], ""),
                (["hook", "--platform", "codex", "--event", "Stop"], "{}"),
                (["hook", "--platform", "claude", "--event", "PreToolUse"], "{}"),
            ):
                result = subprocess.run(
                    [sys.executable, "-c", script, *arguments],
                    cwd=root,
                    env=env,
                    input=input_text,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(
                    "blocked optional review", result.stdout + result.stderr
                )

    def test_candidate_checkout_selected_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            consumer = root / "consumer"
            consumer.mkdir()
            (consumer / ".git").mkdir()
            other = root / "other"
            other.mkdir()
            (other / ".agent-handoff-toolkit").mkdir()
            (other / ".agent-handoff-toolkit" / "jev.json").write_text("invalid")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_handoff_toolkit",
                    "review",
                    str(consumer / "absent.md"),
                ],
                cwd=other,
                env=dict(
                    os.environ,
                    PYTHONPATH=str(ROOT / "src"),
                    GIT_CEILING_DIRECTORIES=str(root),
                ),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "disabled")
