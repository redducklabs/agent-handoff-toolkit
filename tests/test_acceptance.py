"""Acceptance harness behavior without retaining host content."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.acceptance import (  # noqa: E402
    _HOST_CREDENTIALS,
    _HOST_TIMEOUT_SECONDS,
    AcceptanceResult,
    HostRun,
    _configure_observer,
    _contains_sentinel,
    _is_link_or_reparse,
    _is_safe_regular_file,
    _observer_script,
    _operator_credential,
    _run_process,
    _seed_host_home,
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
                    "v0.3.1",
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


class CredentialRecordingRunner(FakeRunner):
    """Record what the host could actually read from its disposable home."""

    def __init__(self, **keywords) -> None:
        super().__init__(**keywords)
        self.visible_credentials: list[tuple[str, bytes]] = []

    def __call__(self, command, *, cwd, input_text, env) -> HostRun:
        if command[0] in {"claude", "codex"}:
            variable, _, name = _HOST_CREDENTIALS[command[0]]
            candidate = Path(env[variable]) / name
            if candidate.is_file():
                self.visible_credentials.append((name, candidate.read_bytes()))
        return super().__call__(command, cwd=cwd, input_text=input_text, env=env)


class DisposableCredentialTests(unittest.TestCase):
    """The host needs its operator credential, and must not keep a copy of it."""

    SECRET = b'{"token":"synthetic-credential-value"}'

    def _operator_home(self, platform: str, directory: str) -> Path:
        variable, default, name = _HOST_CREDENTIALS[platform]
        home = Path(directory) / default
        home.mkdir(parents=True)
        (home / name).write_bytes(self.SECRET)
        return home

    def test_credential_is_readable_during_the_run_and_gone_afterwards(self) -> None:
        for platform in ("claude", "codex"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as home:
                variable, _, name = _HOST_CREDENTIALS[platform]
                self._operator_home(platform, home)
                runner = CredentialRecordingRunner(observer_events=GREEN_EVENTS)
                with patch.dict(
                    os.environ,
                    {variable: str(Path(home) / _HOST_CREDENTIALS[platform][1])},
                ):
                    result = run_acceptance(
                        platform,
                        source_root=ROOT,
                        runner=runner,
                        evidence_verifier=trusted_observer_verifier(runner),
                    )

                self.assertEqual(result.status, "pass")
                # The host saw the real credential, so it could authenticate.
                self.assertEqual(runner.visible_credentials, [(name, self.SECRET)])
                # The disposable home is inside the scratch, which is removed.
                disposable = Path(runner.environments[-1][variable])
                self.assertFalse(disposable.exists())

    def test_the_copy_is_removed_even_when_the_host_run_fails(self) -> None:
        variable, default, name = _HOST_CREDENTIALS["claude"]
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            self._operator_home("claude", home)
            scratch = Path(work) / "scratch"
            runner = FakeRunner(host_returncode=1)
            with patch.dict(os.environ, {variable: str(Path(home) / default)}):
                run_acceptance(
                    "claude", scratch=scratch, source_root=ROOT, runner=runner
                )

            self.assertFalse(scratch.exists())
            # The operator's own credential is untouched.
            self.assertEqual((Path(home) / default / name).read_bytes(), self.SECRET)

    def test_a_seeded_copy_never_outlives_the_host_run(self) -> None:
        variable, default, name = _HOST_CREDENTIALS["claude"]
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            self._operator_home("claude", home)
            runtime = Path(work) / "acceptance-runtime"
            runtime.mkdir()
            with patch.dict(os.environ, {variable: str(Path(home) / default)}):
                copied = _seed_host_home("claude", runtime)

            self.assertIsNotNone(copied)
            self.assertEqual(copied.read_bytes(), self.SECRET)

            from agent_handoff_toolkit.acceptance import _remove_credential

            _remove_credential(copied)
            self.assertFalse(copied.exists())
            self.assertEqual((Path(home) / default / name).read_bytes(), self.SECRET)

    def test_an_absent_credential_is_not_an_error(self) -> None:
        variable, _, _ = _HOST_CREDENTIALS["claude"]
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            runtime = Path(work) / "acceptance-runtime"
            runtime.mkdir()
            with patch.dict(os.environ, {variable: str(Path(home) / "absent")}):
                self.assertIsNone(_operator_credential("claude"))
                self.assertIsNone(_seed_host_home("claude", runtime))

            # The disposable home still exists for the host to write into.
            self.assertTrue((runtime / "claude-home").is_dir())

    def test_only_the_running_platform_credential_is_seeded(self) -> None:
        claude_variable, claude_default, claude_name = _HOST_CREDENTIALS["claude"]
        codex_variable, codex_default, codex_name = _HOST_CREDENTIALS["codex"]
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            self._operator_home("claude", home)
            self._operator_home("codex", home)
            runtime = Path(work) / "acceptance-runtime"
            runtime.mkdir()
            with patch.dict(
                os.environ,
                {
                    claude_variable: str(Path(home) / claude_default),
                    codex_variable: str(Path(home) / codex_default),
                },
            ):
                _seed_host_home("codex", runtime)

            self.assertTrue((runtime / "codex-home" / codex_name).is_file())
            self.assertFalse((runtime / "claude-home" / claude_name).exists())

    def test_a_linked_credential_is_refused(self) -> None:
        variable, default, name = _HOST_CREDENTIALS["claude"]
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            operator = Path(home) / default
            operator.mkdir(parents=True)
            real = Path(work) / "elsewhere.json"
            real.write_bytes(self.SECRET)
            try:
                (operator / name).symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")

            with patch.dict(os.environ, {variable: str(operator)}):
                self.assertIsNone(_operator_credential("claude"))

    def test_the_credential_value_never_reaches_the_reported_result(self) -> None:
        variable, default, _ = _HOST_CREDENTIALS["claude"]
        with tempfile.TemporaryDirectory() as home:
            self._operator_home("claude", home)
            runner = CredentialRecordingRunner(observer_events=GREEN_EVENTS)
            with patch.dict(os.environ, {variable: str(Path(home) / default)}):
                result = run_acceptance(
                    "claude",
                    source_root=ROOT,
                    runner=runner,
                    evidence_verifier=trusted_observer_verifier(runner),
                )

            rendered = format_result(result)
            self.assertNotIn("synthetic-credential-value", rendered)
            self.assertNotIn(self.SECRET.decode("utf-8"), rendered)


class ObserverCommandFormTests(unittest.TestCase):
    """Hosts run hook commands through a POSIX shell, including on Windows."""

    def _configured_commands(self, platform: str, directory: str) -> list[str]:
        scratch = Path(directory) / "scratch"
        runtime = scratch / ".agent-handoff-toolkit" / "acceptance-runtime"
        runtime.mkdir(parents=True)
        (scratch / ".agent-handoff-toolkit" / "runner.py").write_text(
            "", encoding="utf-8"
        )
        hooks = {
            event: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "installed",
                            "commandWindows": "installed",
                        }
                    ]
                }
            ]
            for event in ("UserPromptSubmit", "Stop")
        }
        config = scratch / (
            ".claude/settings.json" if platform == "claude" else ".codex/hooks.json"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        _, written = _configure_observer(platform, scratch, runtime, "run-id")
        value = json.loads(written.read_text(encoding="utf-8"))
        return [
            hook[key]
            for entries in value["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
            for key in ("command", "commandWindows")
            if key in hook
        ]

    def test_observer_command_carries_no_backslash_path(self) -> None:
        # A Windows backslash path is an escape sequence to the POSIX shell the
        # host runs hooks through, so the command silently never executes and
        # the run reports undiscovered hooks.
        for platform in ("claude", "codex"):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory() as directory,
            ):
                for command in self._configured_commands(platform, directory):
                    self.assertNotIn("\\", command)
                    self.assertIn("observe_hook.py", command)

    def test_observer_command_survives_a_path_containing_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spaced = Path(directory) / "a space"
            spaced.mkdir()
            commands = self._configured_commands("claude", str(spaced))
            for command in commands:
                # shlex must be able to recover the original arguments.
                self.assertIn("observe_hook.py", shlex.split(command)[1])
                self.assertEqual(
                    shlex.split(command)[-1] in {"userpromptsubmit", "stop"}, True
                )


class HostTimeoutTests(unittest.TestCase):
    """The cap must outlast a real agent session, not a single request."""

    def test_default_host_timeout_allows_a_multi_turn_session(self) -> None:
        # A measured real Claude run of this scenario took ~167s; a 25s cap
        # killed the host and reported undiscovered hooks instead.
        self.assertGreaterEqual(_HOST_TIMEOUT_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
