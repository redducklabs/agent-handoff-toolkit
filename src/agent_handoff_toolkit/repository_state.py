"""Read the repository's working-tree state for advisory purposes only.

Nothing here gates anything, so nothing here may raise. Every failure - no git
binary, not a repository, a timeout, unreadable output - is reported as "cannot
tell", and the caller stays silent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

WORKTREE_TIMEOUT_SECONDS = 5


def _porcelain_status(root: Path) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            timeout=WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def worktree_state(root: Path) -> tuple[str, bool] | None:
    """Digest and dirtiness from one status read, or None when unreadable.

    Both facts come from the same porcelain output, so a caller that needs
    them together spawns one `git status` rather than two, and never compares
    a digest against a dirtiness reading taken a moment later.
    """

    status = _porcelain_status(root)
    if status is None:
        return None
    return hashlib.sha256(status).hexdigest(), bool(status.strip())


def worktree_digest(root: Path) -> str | None:
    """Digest the porcelain status, or None when it cannot be determined."""

    state = worktree_state(root)
    if state is None:
        return None
    return state[0]


def is_dirty(root: Path) -> bool | None:
    """Whether the tree has any change, or None when it cannot be determined."""

    state = worktree_state(root)
    if state is None:
        return None
    return state[1]
