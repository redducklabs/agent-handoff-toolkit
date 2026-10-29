"""The context window a host reports, kept where a hook can read it cheaply.

Claude Code reports a session's window size to the status line command and to
nothing a hook receives. The managed status line is this relay: it records the
reported size for the session's transcript, then runs the user's own status
line with the same input and returns its output unchanged.

The cache holds one number per transcript, under a file name that is a digest
of the transcript path. No path, prompt, reply or other payload content is
stored. Every failure here records nothing and decides nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

# Set for the user's own status line, so a relay reached again through it
# neither records nor runs anything a second time.
RELAY_MARKER = "AHK_STATUSLINE_RELAY"
MAX_WINDOW = 10**9
_MAX_INPUT = 1024 * 1024
_INNER_TIMEOUT_SECONDS = 10
# Entries are a few bytes each, one per session; old ones are dropped when a
# new one is written.
_RETENTION_SECONDS = 30 * 24 * 3600


def valid_window(value):
    return value if type(value) is int and 0 < value <= MAX_WINDOW else None


def _cache_directory():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return None
        directory = Path(base) / "agent-handoff-toolkit" / "context-window"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        # Not beneath `agent-handoff-toolkit`: on POSIX that directory is the
        # lifecycle state fallback, which admits only its own entries.
        directory = (
            Path(base) if base else Path.home() / ".local" / "state"
        ) / "agent-handoff-toolkit-context-window"
    return directory if directory.is_absolute() else None


def _cache_file(transcript_path):
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return None
    if not Path(transcript_path).is_absolute():
        return None
    directory = _cache_directory()
    if directory is None:
        return None
    key = os.path.normcase(os.path.abspath(transcript_path)).encode("utf-8")
    return directory / (hashlib.sha256(key).hexdigest() + ".json")


def reported_window(transcript_path):
    """Return the window recorded for this transcript, or None."""

    try:
        path = _cache_file(transcript_path)
        if path is None:
            return None
        with open(path, "rb") as handle:
            data = json.loads(handle.read(256))
        return valid_window(data.get("window")) if isinstance(data, dict) else None
    except Exception:
        return None


def _prune(directory, keep):
    cutoff = time.time() - _RETENTION_SECONDS
    for entry in directory.glob("*.json"):
        try:
            if entry != keep and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def record_window(transcript_path, window):
    """Record the reported window for a transcript; None forgets it."""

    path = _cache_file(transcript_path)
    if path is None:
        return
    window = valid_window(window)
    if window is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if reported_window(transcript_path) == window:
        return
    fresh = not path.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    temporary.write_bytes(json.dumps({"window": window}).encode())
    os.replace(temporary, path)
    if fresh:
        _prune(path.parent, path)


def _config_directory():
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def _user_command():
    """Return the user's own status line command, or None."""

    try:
        settings = json.loads(
            (_config_directory() / "settings.json").read_text(encoding="utf-8")
        )
        status_line = settings.get("statusLine")
        command = status_line.get("command")
        if status_line.get("type", "command") != "command":
            return None
        return command if isinstance(command, str) and command.strip() else None
    except Exception:
        return None


def _shell(command):
    """Run the command the way Claude Code runs it: through a POSIX shell."""

    if os.name == "nt":
        bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash")
        if bash and "system32" not in bash.lower():
            return [bash, "-c", command]
        return command
    return [os.environ.get("SHELL") or "/bin/sh", "-c", command]


def _run_user(command, raw, cwd):
    environment = dict(os.environ)
    environment[RELAY_MARKER] = "1"
    argv = _shell(command)
    try:
        result = subprocess.run(
            argv,
            input=raw,
            capture_output=True,
            cwd=cwd,
            env=environment,
            shell=isinstance(argv, str),
            timeout=_INNER_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return b""
    return result.stdout


def relay_status_line(raw):
    """Record the reported window, then return the user's status line output."""

    if os.environ.get(RELAY_MARKER):
        return b""
    cwd = None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            directory = payload.get("cwd")
            if isinstance(directory, str) and Path(directory).is_dir():
                cwd = directory
            context = payload.get("context_window")
            size = (
                context.get("context_window_size")
                if isinstance(context, dict)
                else None
            )
            if valid_window(size) is not None:
                record_window(payload.get("transcript_path"), size)
    except Exception:
        pass
    command = _user_command()
    return b"" if command is None else _run_user(command, raw, cwd)


def read_input(stream):
    return stream.read(_MAX_INPUT)
