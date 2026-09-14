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

from agent_handoff_toolkit.repository_state import worktree_digest  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
