"""Acceptance harness behavior without retaining host content."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.acceptance import (  # noqa: E402
    AcceptanceResult,
    HostRun,
    _contains_sentinel,
    _is_safe_regular_file,
    _observer_script,
    _run_process,
    format_result,
    run_acceptance,
)
from agent_handoff_toolkit.cli import main  # noqa: E402


class FakeRunner:
    """Simulate only the observer's bounded trace, never host output."""

    def __init__(
        self,
        *,
        trace: list[dict[str, object]] | None = None,
        host_returncode: int = 0,
        output: str = "",
        retain_input: bool = False,
    ) -> None:
        self.trace = trace
        self.host_returncode = host_returncode
        self.output = output
        self.retain_input = retain_input
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.environments: list[dict[str, str]] = []

    def __call__(self, command, *, cwd, input_text, env) -> HostRun:
        self.calls.append((tuple(command), cwd))
        self.environments.append(dict(env))
        if command[0] == "git":
            return HostRun(0, False)
        if command[0] == sys.executable:
            target = Path(command[command.index("--target") + 1])
            hooks = {
                event: [{"hooks": [{"command": "installed-command"}]}]
                for event in ("UserPromptSubmit", "Stop")
            }
            for config in (
                target / ".claude" / "settings.json",
                target / ".codex" / "hooks.json",
            ):
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
            return HostRun(0, False)
        if self.retain_input:
            (cwd / "host-output.txt").write_text(input_text, encoding="utf-8")
        if self.trace is not None:
            trace_path = Path(env["AHK_ACCEPTANCE_TRACE"])
            run_id = env["AHK_ACCEPTANCE_RUN_ID"]
            trace_path.write_text(
                "".join(
                    json.dumps({"run_id": run_id, **entry}, separators=(",", ":"))
                    + "\n"
                    for entry in self.trace
                ),
                encoding="utf-8",
            )
        return HostRun(self.host_returncode, False)


def green_trace() -> list[dict[str, object]]:
    return [
        {"event": "user-prompt-submit", "outcome": "observed"},
        {"event": "stop", "outcome": "block", "issue_code": "AHK-STOP-WORK"},
        {"event": "stop", "outcome": "allow"},
    ]


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
            issue_codes=("AHK-STOP-WORK",),
        )

        rendered = format_result(result, sensitive)

        self.assertIn("platform=codex", rendered)
        self.assertIn("status=pass", rendered)
        self.assertIn("issue_codes=AHK-STOP-WORK", rendered)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn("prompt", rendered.lower())
        self.assertNotIn("transcript", rendered.lower())


class AcceptanceHarnessTests(unittest.TestCase):
    def test_arbitrary_host_output_cannot_create_a_pass(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(
                output='{"hook_discovered":true,"blocked":true,"issue_codes":["AHK-STOP-WORK"],"corrected":true,"block_cap_compatible":true}'
            ),
        )

        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.issue_codes, ())

    def test_full_ordered_observer_trace_is_a_passing_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "consumer"
            result = run_acceptance(
                "codex",
                scratch=scratch,
                source_root=ROOT,
                runner=FakeRunner(trace=green_trace()),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.issue_codes, ("AHK-STOP-WORK",))
        self.assertFalse(scratch.exists())

    def test_claude_uses_nonpersistent_disposable_host_locations(self) -> None:
        runner = FakeRunner(trace=green_trace())
        result = run_acceptance("claude", source_root=ROOT, runner=runner)

        host_command = runner.calls[-1][0]
        host_environment = runner.environments[-1]
        self.assertEqual(result.status, "pass")
        self.assertIn("--no-session-persistence", host_command)
        self.assertIn("--settings", host_command)
        self.assertIn("acceptance-runtime", host_environment["CLAUDE_CONFIG_DIR"])
        self.assertIn("acceptance-runtime", host_environment["CODEX_HOME"])

    def test_nonzero_host_outcome_cannot_correct_a_trace(self) -> None:
        result = run_acceptance(
            "codex",
            source_root=ROOT,
            runner=FakeRunner(trace=green_trace(), host_returncode=1),
        )

        self.assertEqual(result.status, "fail")
        self.assertFalse(result.corrected)

    def test_wrong_trace_order_is_not_a_valid_lifecycle_correction(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(trace=list(reversed(green_trace()))),
        )

        self.assertEqual(result.status, "fail")
        self.assertFalse(result.corrected)

    def test_unrecognized_trace_code_is_discarded(self) -> None:
        trace = green_trace()
        trace[1] = {
            "event": "stop",
            "outcome": "block",
            "issue_code": "AHK-MODEL-CLAIM",
        }
        result = run_acceptance(
            "claude", source_root=ROOT, runner=FakeRunner(trace=trace)
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.issue_codes, ())

    def test_retained_runtime_content_is_a_failure(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(trace=green_trace(), retain_input=True),
        )

        self.assertEqual(result.status, "fail")
        self.assertFalse(result.retained_content)

    def test_existing_scratch_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "consumer"
            scratch.mkdir()
            marker = scratch / "consumer-file.txt"
            marker.write_text("consumer-owned", encoding="utf-8")

            result = run_acceptance("claude", scratch=scratch, runner=FakeRunner())

            self.assertEqual(result.status, "unverified")
            self.assertEqual(marker.read_text(encoding="utf-8"), "consumer-owned")


class StreamingAndScannerTests(unittest.TestCase):
    def test_observer_script_is_valid_python(self) -> None:
        compile(_observer_script(Path("C:/runtime/runner.py")), "observer", "exec")

    def test_actual_timeout_terminates_the_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run_process(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=Path(directory),
                input_text="",
                env=os.environ,
                timeout_seconds=0.01,
            )

        self.assertTrue(run.timed_out)

    def test_scanner_detects_a_sentinel_split_across_chunks(self) -> None:
        sentinel = f"synthetic-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "trace.txt").write_text(
                "x" * (8192 - len(sentinel) + 1) + sentinel,
                encoding="utf-8",
            )
            self.assertTrue(_contains_sentinel(root, sentinel))

    def test_scanner_rejects_a_symlink_even_when_its_target_is_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            link = root / "outside-link.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                with patch.object(Path, "is_symlink", return_value=True):
                    self.assertTrue(_contains_sentinel(root, "absent"))
                return

            self.assertTrue(_contains_sentinel(root, "absent"))

    def test_safe_file_check_rejects_an_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")

            self.assertFalse(_is_safe_regular_file(outside, root))


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
            issue_codes=("AHK-STOP-WORK",),
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

    def test_cli_passes_an_explicit_release_source_prerequisite(self) -> None:
        result = AcceptanceResult("codex", False, False, False, False, False, False)
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            with patch(
                "agent_handoff_toolkit.cli.run_acceptance", return_value=result
            ) as run:
                with patch("sys.stdout"):
                    main(
                        [
                            "acceptance",
                            "--platform",
                            "codex",
                            "--scratch",
                            str(Path(directory) / "scratch"),
                            "--release-source",
                            str(release),
                        ]
                    )

        self.assertEqual(run.call_args.kwargs["source_root"], release)

    def test_installed_runner_requires_a_release_source_for_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            consumer = Path(directory) / "consumer"
            consumer.mkdir()
            installed = subprocess.run(
                [
                    sys.executable,
                    "distribution/runner.py",
                    "install",
                    "--target",
                    str(consumer),
                    "--release",
                    "v0.3.0",
                    "--apply",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(installed.returncode, 0)
            result = subprocess.run(
                [
                    sys.executable,
                    str(consumer / ".agent-handoff-toolkit" / "runner.py"),
                    "acceptance",
                    "--platform",
                    "codex",
                    "--scratch",
                    str(Path(directory) / "scratch"),
                ],
                cwd=consumer,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr, "error: acceptance release source unavailable\n"
        )


if __name__ == "__main__":
    unittest.main()
