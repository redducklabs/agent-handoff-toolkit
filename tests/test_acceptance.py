"""Acceptance harness behavior without retaining host content."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
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
    _configure_observer,
    _contains_sentinel,
    _is_link_or_reparse,
    _is_safe_regular_file,
    _observer_script,
    _run_process,
    format_result,
    run_acceptance,
)
from agent_handoff_toolkit.cli import main  # noqa: E402


class FakeRunner:
    """Simulate a host by executing the generated observer, never writing traces."""

    def __init__(
        self,
        *,
        observer_events: tuple[str, ...] | None = None,
        host_returncode: int = 0,
        issue_code: str = "AHK-STOP-WORK",
        retain_input: bool = False,
        forge_trace: bool = False,
    ) -> None:
        self.observer_events = observer_events
        self.host_returncode = host_returncode
        self.issue_code = issue_code
        self.retain_input = retain_input
        self.forge_trace = forge_trace
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.environments: list[dict[str, str]] = []
        self.observer_invocations: list[str] = []

    def __call__(self, command, *, cwd, input_text, env) -> HostRun:
        self.calls.append((tuple(command), cwd))
        self.environments.append(dict(env))
        if command[0] == "git":
            return HostRun(0, False)
        if command[0] == sys.executable:
            target = Path(command[command.index("--target") + 1])
            hooks = {
                event: [{"hooks": [{"command": "installed-command"}]}]
                for event in (
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                )
            }
            for config in (
                target / ".claude" / "settings.json",
                target / ".codex" / "hooks.json",
            ):
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
            runner = target / ".agent-handoff-toolkit" / "runner.py"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_text(
                f'''import pathlib
import sys

event = sys.argv[sys.argv.index("--event") + 1]
counter = pathlib.Path(__file__).with_name("observer-count.txt")
count = int(counter.read_text() if counter.exists() else "0")
if event.replace("-", "") == "stop":
    counter.write_text(str(count + 1))
    if count == 0:
        sys.stdout.write('{{"decision":"block","reason":"{self.issue_code}"}}')
''',
                encoding="utf-8",
            )
            return HostRun(0, False)
        if self.retain_input:
            (cwd / "host-output.txt").write_text(input_text, encoding="utf-8")
        if self.observer_events is not None:
            observer = (
                cwd
                / ".agent-handoff-toolkit"
                / "acceptance-runtime"
                / "observe_hook.py"
            )
            for event in self.observer_events:
                self.observer_invocations.append(event)
                subprocess.run(
                    [sys.executable, str(observer), command[0], event],
                    cwd=cwd,
                    input=b"{}",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    check=False,
                )
        if self.forge_trace:
            observer = (
                cwd
                / ".agent-handoff-toolkit"
                / "acceptance-runtime"
                / "observe_hook.py"
            )
            run_id = re.search(
                r"^run_id = ('[^']+')$",
                observer.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if run_id is None:
                raise AssertionError("observer did not contain a run identifier")
            trace = observer.with_name("lifecycle-trace.jsonl")
            trace.write_text(
                "\n".join(
                    json.dumps(
                        {"run_id": ast.literal_eval(run_id.group(1)), **entry},
                        separators=(",", ":"),
                    )
                    for entry in (
                        {"event": "userpromptsubmit", "outcome": "observed"},
                        {
                            "event": "stop",
                            "outcome": "block",
                            "issue_code": "AHK-STOP-WORK",
                        },
                        {"event": "stop", "outcome": "allow"},
                    )
                )
                + "\n",
                encoding="utf-8",
            )
        return HostRun(self.host_returncode, False)


GREEN_EVENTS = ("userpromptsubmit", "stop", "stop")


def trusted_observer_verifier(runner: FakeRunner):
    return lambda trace, run_id: runner.observer_invocations == list(GREEN_EVENTS)


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
            runner=FakeRunner(),
        )

        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.issue_codes, ())

    def test_full_ordered_observer_trace_is_a_passing_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "consumer"
            runner = FakeRunner(observer_events=GREEN_EVENTS)
            result = run_acceptance(
                "codex",
                scratch=scratch,
                source_root=ROOT,
                runner=runner,
                evidence_verifier=trusted_observer_verifier(runner),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.issue_codes, ("AHK-STOP-WORK",))
        self.assertFalse(scratch.exists())

    def test_host_forged_trace_cannot_create_a_passing_result(self) -> None:
        result = run_acceptance(
            "codex", source_root=ROOT, runner=FakeRunner(forge_trace=True)
        )

        self.assertEqual(result.status, "unverified")

    def test_claude_uses_nonpersistent_disposable_host_locations(self) -> None:
        runner = FakeRunner(observer_events=GREEN_EVENTS)
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=runner,
            evidence_verifier=trusted_observer_verifier(runner),
        )

        host_command = runner.calls[-1][0]
        host_environment = runner.environments[-1]
        self.assertEqual(result.status, "pass")
        self.assertIn("--no-session-persistence", host_command)
        self.assertIn("--settings", host_command)
        self.assertIn("acceptance-runtime", host_environment["CLAUDE_CONFIG_DIR"])
        self.assertIn("acceptance-runtime", host_environment["CODEX_HOME"])
        self.assertNotIn("AHK_ACCEPTANCE_TRACE", host_environment)
        self.assertNotIn("AHK_ACCEPTANCE_RUN_ID", host_environment)

    def test_nonzero_host_outcome_cannot_correct_a_trace(self) -> None:
        runner = FakeRunner(observer_events=GREEN_EVENTS, host_returncode=1)
        result = run_acceptance(
            "codex",
            source_root=ROOT,
            runner=runner,
            evidence_verifier=trusted_observer_verifier(runner),
        )

        self.assertEqual(result.status, "fail")
        self.assertFalse(result.corrected)

    def test_wrong_trace_order_is_not_a_valid_lifecycle_correction(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(observer_events=tuple(reversed(GREEN_EVENTS))),
            evidence_verifier=lambda trace, run_id: True,
        )

        self.assertEqual(result.status, "fail")
        self.assertFalse(result.corrected)

    def test_unrecognized_trace_code_is_discarded(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(
                observer_events=GREEN_EVENTS, issue_code="AHK-MODEL-CLAIM"
            ),
            evidence_verifier=lambda trace, run_id: True,
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.issue_codes, ())

    def test_retained_runtime_content_is_a_failure(self) -> None:
        result = run_acceptance(
            "claude",
            source_root=ROOT,
            runner=FakeRunner(observer_events=GREEN_EVENTS, retain_input=True),
            evidence_verifier=lambda trace, run_id: True,
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

    def test_observer_leaves_unrelated_installed_hooks_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            runtime = scratch / "runtime"
            runtime.mkdir()
            runner = scratch / ".agent-handoff-toolkit" / "runner.py"
            runner.parent.mkdir()
            runner.write_text("", encoding="utf-8")
            original = "installed-command"
            hooks = {
                event: [{"hooks": [{"command": original}]}]
                for event in (
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                )
            }
            config = scratch / ".claude" / "settings.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

            _configure_observer("claude", scratch, runtime, "runtime-only")

            configured = json.loads(config.read_text(encoding="utf-8"))["hooks"]
            for event in ("SessionStart", "PreToolUse", "PostToolUse"):
                self.assertEqual(configured[event][0]["hooks"][0]["command"], original)
            for event in ("UserPromptSubmit", "Stop"):
                self.assertNotEqual(
                    configured[event][0]["hooks"][0]["command"], original
                )


class StreamingAndScannerTests(unittest.TestCase):
    def test_observer_script_is_valid_python(self) -> None:
        compile(
            _observer_script(
                Path("C:/runtime/runner.py"), Path("C:/runtime/trace.jsonl"), "run"
            ),
            "observer",
            "exec",
        )

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

    def test_timeout_terminates_a_descendant_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            child = (
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}]); "
                "time.sleep(30)"
            )
            run = _run_process(
                (sys.executable, "-c", parent),
                cwd=Path(directory),
                input_text="",
                env=os.environ,
                timeout_seconds=0.2,
            )
            child_pid = int(marker.read_text(encoding="utf-8"))
            with self.assertRaises(OSError):
                os.kill(child_pid, 0)

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

    def test_reparse_detection_works_without_path_is_junction(self) -> None:
        class LowerBoundPath:
            def is_symlink(self) -> bool:
                return False

            def stat(self, *, follow_symlinks: bool):
                return type("Metadata", (), {"st_file_attributes": 1024})()

        self.assertTrue(_is_link_or_reparse(LowerBoundPath()))


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
