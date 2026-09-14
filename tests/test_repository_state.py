"""Reading the worktree must never raise and never hang."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.repository_state import (  # noqa: E402
    is_dirty,
    worktree_digest,
    worktree_state,
)


class WorktreeDigestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def test_clean_and_dirty_trees_differ(self):
        clean = worktree_digest(self.root)
        self.assertIsNotNone(clean)
        (self.root / "a.txt").write_text("x")
        self.assertNotEqual(worktree_digest(self.root), clean)

    def test_returns_none_outside_a_repository(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        os.environ["GIT_CEILING_DIRECTORIES"] = other.as_posix()
        self.assertIsNone(worktree_digest(other))

    def test_never_raises_when_git_is_unusable(self):
        for failure in (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired("git", 5),
            OSError("boom"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "agent_handoff_toolkit.repository_state.subprocess.run",
                    side_effect=failure,
                ):
                    self.assertIsNone(worktree_digest(self.root))


class WorktreeStateTests(unittest.TestCase):
    """Both facts come from one status read, so callers spawn one process."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def test_reports_digest_and_dirtiness_from_a_single_status_read(self):
        real = subprocess.run
        calls = []

        def counted(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        with patch("agent_handoff_toolkit.repository_state.subprocess.run", counted):
            clean = worktree_state(self.root)
            self.assertEqual(len(calls), 1)
            (self.root / "a.txt").write_text("x")
            dirty = worktree_state(self.root)
            self.assertEqual(len(calls), 2)
        self.assertEqual(clean[1], False)
        self.assertEqual(dirty[1], True)
        self.assertNotEqual(clean[0], dirty[0])
        # The two public readers stay consistent with the combined one.
        self.assertEqual(worktree_digest(self.root), dirty[0])
        self.assertIs(is_dirty(self.root), dirty[1])

    def test_returns_none_when_the_status_cannot_be_read(self):
        with patch(
            "agent_handoff_toolkit.repository_state.subprocess.run",
            side_effect=OSError("boom"),
        ):
            self.assertIsNone(worktree_state(self.root))


class IsDirtyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def test_clean_tree_is_not_dirty(self):
        self.assertIs(is_dirty(self.root), False)

    def test_untracked_file_makes_the_tree_dirty(self):
        (self.root / "a.txt").write_text("x")
        self.assertIs(is_dirty(self.root), True)

    def test_returns_none_outside_a_repository(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        os.environ["GIT_CEILING_DIRECTORIES"] = other.as_posix()
        self.assertIsNone(is_dirty(other))

    def test_never_raises_when_git_is_unusable(self):
        for failure in (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired("git", 5),
            OSError("boom"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "agent_handoff_toolkit.repository_state.subprocess.run",
                    side_effect=failure,
                ):
                    self.assertIsNone(is_dirty(self.root))


if __name__ == "__main__":
    unittest.main()
