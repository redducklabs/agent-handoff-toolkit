from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_handoff_toolkit.launcher import launcher_problem  # noqa: E402


def found(command: str) -> str:
    return f"/usr/bin/{command}"


def missing(command: str) -> None:
    return None


def reports(returncode: int = 0, stdout: str = "", stderr: str = ""):
    calls: list[list[str]] = []

    def run(arguments, **kwargs):
        calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)

    run.calls = calls
    return run


class LauncherProblemTests(unittest.TestCase):
    """The hooks launch the manifest's command, so install must prove it runs."""

    def test_a_qualifying_interpreter_has_no_problem(self) -> None:
        for output in ("Python 3.11.0\n", "Python 3.13.9\n", "Python 3.14.7\n"):
            with self.subTest(output=output):
                run = reports(stdout=output)
                self.assertIsNone(
                    launcher_problem("python", "3.11", which=found, run=run)
                )
                self.assertEqual(run.calls, [["/usr/bin/python", "--version"]])

    def test_the_program_found_on_path_is_the_one_that_runs(self) -> None:
        """Windows resolves a bare name beside the running interpreter first.

        Running the bare name could vouch for an interpreter next to the
        installer while the hooks, resolved through PATH, reach the Microsoft
        Store alias stub.
        """

        run = reports(stdout="Python 3.13.9\n")

        def which(command: str) -> str:
            return r"C:\Users\dev\AppData\Local\Microsoft\WindowsApps\python.exe"

        launcher_problem("python", "3.11", which=which, run=run)

        self.assertEqual(
            run.calls,
            [
                [
                    r"C:\Users\dev\AppData\Local\Microsoft\WindowsApps\python.exe",
                    "--version",
                ]
            ],
        )

    def test_a_three_part_minimum_accepts_its_own_release(self) -> None:
        self.assertIsNone(
            launcher_problem(
                "python", "3.11.0", which=found, run=reports(stdout="Python 3.11.0\n")
            )
        )
        self.assertIsNone(
            launcher_problem(
                "python", "3.11.0", which=found, run=reports(stdout="Python 3.11\n")
            )
        )
        self.assertIsNotNone(
            launcher_problem(
                "python", "3.11.4", which=found, run=reports(stdout="Python 3.11.2\n")
            )
        )

    def test_a_command_missing_from_path_is_a_problem(self) -> None:
        problem = launcher_problem(
            "python", "3.11", which=missing, run=reports(stdout="Python 3.13.9")
        )

        self.assertIsNotNone(problem)
        self.assertIn("`python` was not found on PATH", problem)
        self.assertIn("Python 3.11 or newer", problem)

    def test_a_command_that_exits_non_zero_is_a_problem(self) -> None:
        """The Windows App Execution Alias stub resolves on PATH but never runs."""

        problem = launcher_problem(
            "python", "3.11", which=found, run=reports(returncode=9009)
        )

        self.assertIsNotNone(problem)
        self.assertIn("exited 9009", problem)

    def test_a_command_that_cannot_start_is_a_problem(self) -> None:
        for error in (
            OSError("exec format error"),
            subprocess.TimeoutExpired(["python", "--version"], 10),
        ):
            with self.subTest(error=type(error).__name__):

                def run(arguments, **kwargs):
                    raise error

                problem = launcher_problem("python", "3.11", which=found, run=run)

                self.assertIsNotNone(problem)
                self.assertIn("could not be run", problem)

    def test_output_without_a_python_version_is_a_problem(self) -> None:
        problem = launcher_problem(
            "python", "3.11", which=found, run=reports(stdout="not an interpreter\n")
        )

        self.assertIsNotNone(problem)
        self.assertIn("did not report a Python version", problem)

    def test_an_interpreter_below_the_minimum_is_a_problem(self) -> None:
        for kwargs, reported in (
            ({"stdout": "Python 3.9.6\n"}, "3.9.6"),
            ({"stderr": "Python 2.7.18\n"}, "2.7.18"),
        ):
            with self.subTest(reported=reported):
                problem = launcher_problem(
                    "python", "3.11", which=found, run=reports(**kwargs)
                )

                self.assertIsNotNone(problem)
                self.assertIn(f"reports Python {reported}", problem)


if __name__ == "__main__":
    unittest.main()
