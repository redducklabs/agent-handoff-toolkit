"""Verify the interpreter that installed hook commands launch.

A hook whose interpreter cannot start fails non-blockingly, so the whole
lifecycle goes dead without a visible error. Install and sync therefore refuse
to proceed until the manifest's command runs and meets the minimum version.
The check stays out of ``build_plan`` so a plan remains independent of the
machine that computes it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Callable

_VERSION = re.compile(r"Python (?P<number>\d+(?:\.\d+){1,2})")
_TIMEOUT_SECONDS = 10


def launcher_problem(
    command: str,
    minimum: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Return why ``command`` cannot launch the hooks, or ``None`` if it can."""

    requirement = (
        f"hook commands run `{command}`, which must be Python {minimum} or newer"
    )
    resolved = which(command)
    if resolved is None:
        return f"{requirement}; `{command}` was not found on PATH"
    try:
        # Run the program PATH names. Windows looks beside the running
        # interpreter before PATH for a bare name, which could vouch for an
        # interpreter the hooks never reach.
        result = run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return (
            f"{requirement}; `{command} --version` could not be run "
            f"({type(error).__name__})"
        )
    if result.returncode != 0:
        return f"{requirement}; `{command} --version` exited {result.returncode}"
    match = _VERSION.search(f"{result.stdout}\n{result.stderr}")
    if match is None:
        return f"{requirement}; `{command} --version` did not report a Python version"
    reported = match["number"]
    required = tuple(int(part) for part in minimum.split("."))
    actual = tuple(int(part) for part in reported.split(".")) + (0,) * len(required)
    if actual[: len(required)] < required:
        return f"{requirement}; `{command}` reports Python {reported}"
    return None
