"""A consumer's own project status line is chained behind the managed relay.

Claude Code has one project `statusLine`. A consumer that already uses it keeps
it: install and sync move it, unchanged, to a consumer-owned chain file that
the relay runs in place of the user's own status line.
"""

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

from agent_handoff_toolkit.context_window import (  # noqa: E402
    CHAIN_RELATIVE_PATH,
    RUNNER_RELATIVE_PATH,
    relay_status_line,
    reported_window,
)
from agent_handoff_toolkit.installer import (  # noqa: E402
    apply_plan,
    build_plan,
    render_plan,
)
from agent_handoff_toolkit.manifest import load_manifest  # noqa: E402
from agent_handoff_toolkit.state import load_state  # noqa: E402

RELEASE = load_manifest(ROOT).release
FRAGMENT = json.loads(
    (ROOT / "adapters" / "claude" / "settings.fragment.json").read_text(
        encoding="utf-8"
    )
)
PYTHON = Path(sys.executable).as_posix()


def echo(label):
    return (
        f'"{PYTHON}" -c "import sys; data = sys.stdin.read(); '
        f"sys.stdout.write('{label}:' + str(len(data)))\""
    )


class StatusLineChainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(
            os.environ,
            {
                "GIT_CEILING_DIRECTORIES": self.root.as_posix(),
                "LOCALAPPDATA": str(self.root / "local"),
                "XDG_STATE_HOME": str(self.root / "local"),
                "CLAUDE_CONFIG_DIR": str(self.root / "config"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AHK_STATUSLINE_RELAY", None)
        (self.root / "config").mkdir()
        self.consumer = self.root / "consumer"
        (self.consumer / ".git").mkdir(parents=True)
        (self.consumer / ".claude").mkdir()
        self.settings = self.consumer / ".claude" / "settings.json"
        self.chain = self.consumer / Path(*CHAIN_RELATIVE_PATH.split("/"))
        self.own = {"type": "command", "command": echo("project"), "padding": 0}

    def write_settings(self, value):
        self.settings.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def install(self, operation="install"):
        plan = build_plan(ROOT, self.consumer, RELEASE, operation)
        self.assertEqual(plan.conflicts, (), render_plan(plan))
        apply_plan(plan)
        return plan

    def settings_json(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def place_runner(self, root):
        """Mark `root` as an install: the runner the managed command walks to."""

        runner = root / Path(*RUNNER_RELATIVE_PATH.split("/"))
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("", encoding="utf-8")

    def user_status_line(self):
        (self.root / "config" / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": echo("user")}}),
            encoding="utf-8",
        )

    def hostile_chain(self, directory):
        """Ship a chain file in `directory`; return the file its command makes."""

        proof = self.root / f"ran-{directory.name}"
        command = (
            f'"{PYTHON}" -c "import pathlib; '
            f"pathlib.Path('{proof.as_posix()}').write_text('ran')\""
        )
        chain = directory / Path(*CHAIN_RELATIVE_PATH.split("/"))
        chain.parent.mkdir(parents=True, exist_ok=True)
        chain.write_text(
            json.dumps({"statusLine": {"type": "command", "command": command}}),
            encoding="utf-8",
        )
        return proof

    def test_an_existing_project_status_line_is_chained_not_a_conflict(self):
        self.write_settings({"statusLine": self.own, "model": "opus"})
        plan = self.install()
        self.assertIn(CHAIN_RELATIVE_PATH, render_plan(plan))
        settings = self.settings_json()
        self.assertEqual(settings["statusLine"], FRAGMENT["statusLine"])
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(
            json.loads(self.chain.read_text(encoding="utf-8")),
            {"statusLine": self.own},
        )
        # The chain file is the consumer's: the toolkit never owns it.
        targets = {target.target for target in load_state(self.consumer).targets}
        self.assertNotIn(CHAIN_RELATIVE_PATH, targets)
        current = build_plan(ROOT, self.consumer, RELEASE, "sync")
        self.assertEqual((current.conflicts, current.changes), ((), ()))

    def test_no_project_status_line_writes_no_chain_file(self):
        self.write_settings({"model": "opus"})
        self.install()
        self.assertEqual(self.settings_json()["statusLine"], FRAGMENT["statusLine"])
        self.assertFalse(self.chain.exists())

    def test_sync_from_a_release_without_the_relay_chains_too(self):
        """A prior release owned the hooks but never the status line."""

        hooks_only = {"hooks": FRAGMENT["hooks"]}
        self.write_settings({"statusLine": self.own})
        self.install()
        state = load_state(self.consumer)
        # Rewind to a consumer whose settings hold only the old fragment.
        self.chain.unlink()
        self.write_settings({**hooks_only, "statusLine": self.own})
        path = self.consumer / ".agent-handoff-toolkit" / "install-state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for target in data["targets"]:
            if target["target"] == ".claude/settings.json":
                target["owned_fragment"] = hooks_only
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.assertTrue(state.targets)
        self.install("sync")
        self.assertEqual(self.settings_json()["statusLine"], FRAGMENT["statusLine"])
        self.assertEqual(
            json.loads(self.chain.read_text(encoding="utf-8")),
            {"statusLine": self.own},
        )

    def test_a_different_existing_chain_file_is_a_conflict(self):
        self.write_settings({"statusLine": self.own})
        self.chain.write_text(
            json.dumps({"statusLine": {"type": "command", "command": "other"}}),
            encoding="utf-8",
        )
        plan = build_plan(ROOT, self.consumer, RELEASE, "install")
        self.assertEqual(
            [conflict.code for conflict in plan.conflicts],
            ["statusline-chain-conflict"],
        )
        self.assertEqual(plan.changes, ())

    def test_an_identical_existing_chain_file_is_accepted(self):
        self.write_settings({"statusLine": self.own})
        self.chain.write_text(json.dumps({"statusLine": self.own}), encoding="utf-8")
        self.install()
        self.assertEqual(self.settings_json()["statusLine"], FRAGMENT["statusLine"])

    def test_a_non_object_project_status_line_is_a_conflict(self):
        self.write_settings({"statusLine": "echo hi"})
        plan = build_plan(ROOT, self.consumer, RELEASE, "install")
        self.assertEqual(
            [conflict.code for conflict in plan.conflicts],
            ["statusline-chain-conflict"],
        )

    def test_the_relay_runs_the_chained_command_before_the_users(self):
        self.user_status_line()
        self.place_runner(self.consumer)
        self.chain.write_text(json.dumps({"statusLine": self.own}), encoding="utf-8")
        transcript = str(self.root / "transcript.jsonl")
        nested = self.consumer / "app" / "src"
        nested.mkdir(parents=True)
        raw = json.dumps(
            {
                "transcript_path": transcript,
                "cwd": str(nested),
                "workspace": {"project_dir": str(self.consumer)},
                "context_window": {"context_window_size": 1_000_000},
            }
        ).encode()
        self.assertEqual(relay_status_line(raw), f"project:{len(raw)}".encode())
        self.assertEqual(reported_window(transcript), 1_000_000)
        # Found from a project directory beneath the install.
        raw = json.dumps({"workspace": {"project_dir": str(nested)}}).encode()
        self.assertEqual(relay_status_line(raw), f"project:{len(raw)}".encode())
        # Without a chain file the user's own status line runs.
        self.chain.unlink()
        self.assertEqual(relay_status_line(raw), f"user:{len(raw)}".encode())

    def test_a_malformed_chain_file_falls_back_to_the_users(self):
        self.user_status_line()
        self.place_runner(self.consumer)
        for content in ("not json", "[]", '{"statusLine": {"command": ""}}'):
            with self.subTest(content=content):
                self.chain.write_text(content, encoding="utf-8")
                raw = json.dumps(
                    {"workspace": {"project_dir": str(self.consumer)}}
                ).encode()
                self.assertEqual(relay_status_line(raw), f"user:{len(raw)}".encode())

    def test_an_unreadable_payload_still_finds_the_chain_from_its_directory(self):
        """Without a readable payload the relay looks from where it runs."""

        self.place_runner(self.consumer)
        self.chain.write_text(json.dumps({"statusLine": self.own}), encoding="utf-8")
        nested = self.consumer / "app"
        nested.mkdir()
        previous = os.getcwd()
        os.chdir(nested)
        self.addCleanup(os.chdir, previous)
        for raw in (b"not json", b"[1]", b"{}"):
            with self.subTest(raw=raw):
                self.assertEqual(relay_status_line(raw), f"project:{len(raw)}".encode())

    def test_an_install_puts_the_chain_file_where_the_relay_reads_it(self):
        self.write_settings({"statusLine": self.own})
        self.install()
        raw = json.dumps({"workspace": {"project_dir": str(self.consumer)}}).encode()
        self.assertEqual(relay_status_line(raw), f"project:{len(raw)}".encode())

    def test_a_chain_file_where_the_session_moved_never_runs(self):
        """Only the launch directory was trusted; `cwd` follows the session."""

        self.place_runner(self.consumer)
        self.user_status_line()
        clone = self.root / "clone"
        (clone / ".git").mkdir(parents=True)
        proof = self.hostile_chain(clone)
        raw = json.dumps(
            {
                "cwd": str(clone),
                "workspace": {
                    "current_dir": str(clone),
                    "project_dir": str(self.consumer),
                },
            }
        ).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_the_process_directory_does_not_stand_in_for_the_project(self):
        """A project with no chain file looks no further than itself."""

        self.place_runner(self.consumer)
        self.user_status_line()
        clone = self.consumer / "vendor" / "clone"
        (clone / ".git").mkdir(parents=True)
        proof = self.hostile_chain(clone)
        previous = os.getcwd()
        os.chdir(clone)
        self.addCleanup(os.chdir, previous)
        raw = json.dumps({"workspace": {"project_dir": str(self.consumer)}}).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_an_unreadable_payload_trusts_only_the_runners_directory(self):
        """A clone inside the project holds no runner, so the walk passes it.

        The managed command runs the first runner above the process's
        directory. A chain file beside that runner gives nothing it does not
        already have; one anywhere below it is not the install's.
        """

        self.place_runner(self.consumer)
        self.user_status_line()
        clone = self.consumer / "vendor" / "clone"
        (clone / ".git").mkdir(parents=True)
        proof = self.hostile_chain(clone)
        previous = os.getcwd()
        os.chdir(clone)
        self.addCleanup(os.chdir, previous)
        for raw in (b"not json", b"[1]", b"{}", b'{"cwd": "."}'):
            with self.subTest(raw=raw):
                output = relay_status_line(raw)
                self.assertFalse(proof.exists())
                self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_a_directory_above_a_project_outside_git_is_not_searched(self):
        """No `.git` stops the walk here, and the runner must."""

        self.user_status_line()
        shared = self.root / "shared"
        project = shared / "project"
        project.mkdir(parents=True)
        self.place_runner(project)
        self.place_runner(shared)
        proof = self.hostile_chain(shared)
        raw = json.dumps({"workspace": {"project_dir": str(project)}}).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_a_repository_root_stops_the_walk_to_an_outer_install(self):
        """Trusting a repository does not extend to the install around it."""

        self.user_status_line()
        outer = self.root / "outer"
        repository = outer / "repository"
        (repository / ".git").mkdir(parents=True)
        self.place_runner(outer)
        proof = self.hostile_chain(outer)
        raw = json.dumps({"workspace": {"project_dir": str(repository)}}).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_the_nearest_install_answers_for_a_nested_one(self):
        """The chain file belongs to the runner the walk reaches first."""

        self.user_status_line()
        self.place_runner(self.consumer)
        proof = self.hostile_chain(self.consumer)
        package = self.consumer / "package"
        package.mkdir()
        self.place_runner(package)
        raw = json.dumps({"workspace": {"project_dir": str(package)}}).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    @unittest.skipUnless(os.name == "nt", "Only Windows keeps a link in the cwd")
    def test_the_fallback_walks_the_same_path_as_the_managed_command(self):
        """A junction must not lead the relay past the runner that ran."""

        self.user_status_line()
        self.place_runner(self.consumer)
        hostile = self.root / "hostile"
        (hostile / "sub").mkdir(parents=True)
        self.place_runner(hostile)
        proof = self.hostile_chain(hostile)
        link = self.consumer / "link"
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(hostile / "sub")],
            check=True,
            capture_output=True,
        )
        self.addCleanup(os.rmdir, link)
        previous = os.getcwd()
        os.chdir(link)
        self.addCleanup(os.chdir, previous)
        raw = b"not json"
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    def test_a_project_directory_that_cannot_be_used_is_not_replaced(self):
        """A named project is not swapped for `cwd` or the process's directory."""

        self.user_status_line()
        proof = self.hostile_chain(self.consumer)
        self.place_runner(self.consumer)
        previous = os.getcwd()
        os.chdir(self.consumer)
        self.addCleanup(os.chdir, previous)
        missing = str(self.root / "gone")
        raw = json.dumps(
            {"cwd": str(self.consumer), "workspace": {"project_dir": missing}}
        ).encode()
        output = relay_status_line(raw)
        self.assertFalse(proof.exists())
        self.assertEqual(output, f"user:{len(raw)}".encode())

    @unittest.skipUnless(os.name == "nt", "Git Bash paths exist only on Windows")
    def test_a_git_bash_path_finds_the_chain(self):
        self.place_runner(self.consumer)
        self.chain.write_text(json.dumps({"statusLine": self.own}), encoding="utf-8")
        drive, rest = self.consumer.as_posix().split(":", 1)
        project = f"/{drive.lower()}{rest}"
        raw = json.dumps({"workspace": {"project_dir": project}}).encode()
        self.assertEqual(relay_status_line(raw), f"project:{len(raw)}".encode())


if __name__ == "__main__":
    unittest.main()
