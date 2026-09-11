"""Acceptance harness behavior without retaining host content."""

from __future__ import annotations

import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.acceptance import (  # noqa: E402
    AcceptanceResult,
    HostRun,
    format_result,
    run_acceptance,
)
from agent_handoff_toolkit.cli import main  # noqa: E402


class FakeRunner:
    """Return only ephemeral host output selected by the test case."""

    def __init__(
        self, output: str = "", returncode: int = 0, *, retain_input: bool = False
    ) -> None:
        self.output = output
        self.returncode = returncode
        self.retain_input = retain_input
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, command, *, cwd, input_text, env) -> HostRun:
        self.calls.append((tuple(command), cwd))
        if command[0] == sys.executable:
            return HostRun(0, "", "")
        if self.retain_input:
            (cwd / "host-output.txt").write_text(input_text, encoding="utf-8")
        return HostRun(self.returncode, self.output, "")


class AcceptanceResultTests(unittest.TestCase):
    def test_all_required_properties_must_pass_for_a_passing_result(self) -> None:
        result = AcceptanceResult(
            platform="claude",
            discovered=True,
            blocked=True,
            issue_received=True,
            corrected=True,
            retained_content=True,
            block_cap_compatible=True,
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.exit_code, 0)

        failed = AcceptanceResult(
            platform="claude",
            discovered=True,
            blocked=True,
            issue_received=True,
            corrected=False,
            retained_content=True,
            block_cap_compatible=True,
        )
        self.assertEqual(failed.status, "fail")
        self.assertNotEqual(failed.exit_code, 0)

    def test_unavailable_host_is_unverified_not_pass(self) -> None:
        result = run_acceptance("codex", runner=FakeRunner(returncode=127))

        self.assertEqual(result.status, "unverified")
        self.assertNotEqual(result.exit_code, 0)

    def test_timed_out_host_is_unverified_not_pass(self) -> None:
        def timed_out(*args, **kwargs) -> HostRun:
            raise subprocess.TimeoutExpired("codex", 20)

        result = run_acceptance("codex", runner=timed_out)

        self.assertEqual(result.status, "unverified")
        self.assertNotEqual(result.exit_code, 0)

    def test_formatted_result_redacts_ephemeral_host_content(self) -> None:
        sensitive = f"synthetic-{uuid.uuid4().hex}"
        result = AcceptanceResult(
            platform="codex",
            discovered=True,
            blocked=True,
            issue_received=True,
            corrected=True,
            retained_content=True,
            block_cap_compatible=True,
            issue_codes=("AHK-STOP-REQUIRED",),
        )

        rendered = format_result(result, sensitive)

        self.assertIn("platform=codex", rendered)
        self.assertIn("status=pass", rendered)
        self.assertIn("issue_codes=AHK-STOP-REQUIRED", rendered)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn("prompt", rendered.lower())
        self.assertNotIn("transcript", rendered.lower())


class AcceptanceHarnessTests(unittest.TestCase):
    def assert_scenario(self, output: str, **expected: bool) -> None:
        result = run_acceptance("claude", runner=FakeRunner(output))
        for name, value in expected.items():
            self.assertEqual(getattr(result, name), value, name)

    def test_fake_host_marks_undiscovered_hook_as_failure(self) -> None:
        self.assert_scenario(
            '{"blocked":true,"issue_codes":["AHK-STOP-REQUIRED"],"corrected":true}',
            discovered=False,
        )

    def test_fake_host_marks_unblocked_violation_as_failure(self) -> None:
        self.assert_scenario(
            '{"hook_discovered":true,"issue_codes":["AHK-STOP-REQUIRED"],"corrected":true}',
            blocked=False,
        )

    def test_fake_host_marks_missing_issue_as_failure(self) -> None:
        self.assert_scenario(
            '{"hook_discovered":true,"blocked":true,"corrected":true}',
            issue_received=False,
        )

    def test_fake_host_marks_rejected_correction_as_failure(self) -> None:
        self.assert_scenario(
            '{"hook_discovered":true,"blocked":true,"issue_codes":["AHK-STOP-REQUIRED"]}',
            corrected=False,
        )

    def test_fake_host_marks_retained_content_as_failure(self) -> None:
        result = run_acceptance(
            "claude",
            runner=FakeRunner(
                '{"hook_discovered":true,"blocked":true,"issue_codes":["AHK-STOP-REQUIRED"],"corrected":true}',
                retain_input=True,
            ),
        )
        self.assertFalse(result.retained_content)

    def test_fake_host_marks_incompatible_block_cap_as_failure(self) -> None:
        self.assert_scenario(
            '{"hook_discovered":true,"blocked":true,"issue_codes":["AHK-STOP-REQUIRED"],"corrected":true,"block_cap_compatible":false}',
            block_cap_compatible=False,
        )

    def test_fake_host_full_lifecycle_path_is_green_and_cleans_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "consumer"
            result = run_acceptance(
                "codex",
                scratch=scratch,
                runner=FakeRunner(
                    '{"hook_discovered":true,"blocked":true,"issue_codes":["AHK-STOP-REQUIRED"],"corrected":true,"block_cap_compatible":true}'
                ),
            )

            self.assertEqual(result.status, "pass")
            self.assertFalse(scratch.exists())

    def test_existing_scratch_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "consumer"
            scratch.mkdir()
            marker = scratch / "consumer-file.txt"
            marker.write_text("consumer-owned", encoding="utf-8")

            result = run_acceptance("claude", scratch=scratch, runner=FakeRunner())

            self.assertEqual(result.status, "unverified")
            self.assertEqual(marker.read_text(encoding="utf-8"), "consumer-owned")


class AcceptanceCliTests(unittest.TestCase):
    def test_opt_in_cli_prints_reduced_result_and_returns_its_status(self) -> None:
        result = AcceptanceResult(
            platform="codex",
            discovered=True,
            blocked=True,
            issue_received=True,
            corrected=True,
            retained_content=True,
            block_cap_compatible=True,
            issue_codes=("AHK-STOP-REQUIRED",),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("agent_handoff_toolkit.cli.run_acceptance", return_value=result):
                with patch("sys.stdout") as output:
                    status = main(
                        ["acceptance", "--platform", "codex", "--scratch", directory]
                    )

        self.assertEqual(status, 0)
        rendered = "".join(call.args[0] for call in output.write.call_args_list)
        self.assertIn("platform=codex", rendered)
        self.assertNotIn("prompt", rendered.lower())


if __name__ == "__main__":
    unittest.main()
