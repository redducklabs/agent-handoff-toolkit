"""Opt-in, content-redacting lifecycle host acceptance checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Mapping, Sequence
import uuid


_EXPECTED_ISSUES = frozenset({"AHK-STOP-WORK"})
_REQUIRED_PROPERTIES = (
    "discovered",
    "blocked",
    "issue_received",
    "corrected",
    "retained_content",
    "block_cap_compatible",
)
_MAX_HOST_OUTPUT_BYTES = 65536
_MAX_TRACE_BYTES = 8192
_HOST_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class HostRun:
    """Only process outcome and bounded stream-reduction facts are retained."""

    returncode: int
    timed_out: bool
    jsonl_events: int = 0


@dataclass(frozen=True)
class AcceptanceResult:
    """Reduced, content-free evidence from one host lifecycle attempt."""

    platform: str
    discovered: bool
    blocked: bool
    issue_received: bool
    corrected: bool
    retained_content: bool
    block_cap_compatible: bool
    issue_codes: tuple[str, ...] = ()
    observed: bool = True

    @property
    def status(self) -> str:
        if not self.observed:
            return "unverified"
        return (
            "pass"
            if all(getattr(self, item) for item in _REQUIRED_PROPERTIES)
            else "fail"
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "pass" else 1


class AcceptancePrerequisiteError(ValueError):
    """A release checkout is required to make an installed disposable consumer."""


Runner = Callable[..., HostRun]
EvidenceVerifier = Callable[[Path, str], bool]


class _JsonlReducer:
    """Drain host streams without retaining text or trusting model events."""

    def __init__(self) -> None:
        self._tail = b""
        self._bytes_seen = 0
        self.jsonl_events = 0

    def feed(self, chunk: bytes) -> None:
        self._bytes_seen += len(chunk)
        if self._bytes_seen > _MAX_HOST_OUTPUT_BYTES:
            return
        data = self._tail + chunk
        lines = data.split(b"\n")
        self._tail = lines.pop()[-4096:]
        for line in lines:
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                self.jsonl_events += 1


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/t", "/f"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str,
    env: Mapping[str, str],
    timeout_seconds: float = _HOST_TIMEOUT_SECONDS,
) -> HostRun:
    """Run a host while incrementally discarding stdout/stderr content."""

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(env),
        creationflags=creationflags if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(input_text.encode("utf-8"))
    process.stdin.close()

    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=16)
    stopped = threading.Event()

    def drain() -> None:
        try:
            while chunk := process.stdout.read(4096):
                while not stopped.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            process.stdout.close()
            if not stopped.is_set():
                while not stopped.is_set():
                    try:
                        chunks.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    reducer = _JsonlReducer()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_tree(process)
            break
        try:
            chunk = chunks.get(timeout=remaining)
        except queue.Empty:
            timed_out = True
            _terminate_process_tree(process)
            break
        if chunk is None:
            break
        reducer.feed(chunk)
    stopped.set()
    process.wait(timeout=2)
    reader.join(timeout=2)
    return HostRun(process.returncode or 0, timed_out, reducer.jsonl_events)


def _host_command(platform: str, settings: Path) -> tuple[str, ...]:
    if platform == "claude":
        return (
            "claude",
            "-p",
            "--model",
            "haiku",
            "--no-session-persistence",
            "--settings",
            str(settings),
        )
    if platform == "codex":
        return (
            "codex",
            "exec",
            "--ephemeral",
            "--model",
            "gpt-5.6-luna",
            "--json",
            "--dangerously-bypass-hook-trust",
        )
    raise ValueError("platform must be claude or codex")


def _scenario_input(sentinel: str) -> str:
    """Build disposable synthetic instructions without persisting their text."""

    return (
        "Run the installed lifecycle acceptance scenario for identifier "
        f"{sentinel}. Attempt one invalid tracked stop, receive its lifecycle "
        "feedback, then submit a corrected stop."
    )


def _is_link_or_reparse(candidate: Path) -> bool:
    if candidate.is_symlink():
        return True
    is_junction = getattr(candidate, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attributes = candidate.stat(follow_symlinks=False).st_file_attributes
    except AttributeError:
        return False
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _is_safe_regular_file(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        if _is_link_or_reparse(candidate):
            return False
        return stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode)
    except (OSError, ValueError):
        return False


def _contains_sentinel(root: Path, sentinel: str) -> bool:
    """Treat unreadable, escaping, or linked runtime entries as unsafe retention."""

    if _is_link_or_reparse(root):
        return True
    root = root.resolve()
    try:
        candidates = tuple(root.rglob("*"))
    except OSError:
        return True
    overlap = ""
    for candidate in candidates:
        if _is_link_or_reparse(candidate):
            return True
        if candidate.is_dir():
            continue
        if not _is_safe_regular_file(candidate, root):
            return True
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as source:
                while chunk := source.read(8192):
                    if sentinel in overlap + chunk:
                        return True
                    overlap = (overlap + chunk)[-(len(sentinel) - 1) :]
        except OSError:
            return True
    return False


def _release_source(root: Path | None) -> Path:
    candidate = (root or Path(__file__).resolve().parents[2]).resolve()
    runner = candidate / "distribution" / "runner.py"
    manifest = candidate / "distribution" / "manifest.json"
    if not _is_safe_regular_file(runner, candidate) or not _is_safe_regular_file(
        manifest, candidate
    ):
        raise AcceptancePrerequisiteError("acceptance release source unavailable")
    return candidate


def _observer_script(runner: Path, trace: Path, run_id: str) -> str:
    return f"""import json
import os
from pathlib import Path
import subprocess
import sys

runner = {str(runner)!r}
trace = {str(trace)!r}
run_id = {run_id!r}
platform, event = sys.argv[1:3]
raw = sys.stdin.buffer.read()
completed = subprocess.run([sys.executable, runner, "hook", "--platform", platform, "--event", event], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
combined = completed.stdout + completed.stderr
allowed = "AHK-STOP-WORK"
entry = {{"run_id": run_id, "event": event, "outcome": "other"}}
if event.replace("-", "") == "userpromptsubmit" and completed.returncode == 0:
    entry["outcome"] = "observed"
elif event == "stop":
    if b'"decision":"block"' in combined and allowed.encode() in combined:
        entry.update(outcome="block", issue_code=allowed)
    elif completed.returncode == 0 and not combined:
        entry["outcome"] = "allow"
Path(trace).open("a", encoding="utf-8", newline="\\n").write(json.dumps(entry, separators=(",", ":")) + "\\n")
sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
sys.exit(completed.returncode)
"""


def _configure_observer(
    platform: str, scratch: Path, runtime: Path, run_id: str
) -> tuple[Path, Path]:
    """Install a disposable hook observer that records no model content."""

    trace = runtime / "lifecycle-trace.jsonl"
    observer = runtime / "observe_hook.py"
    runner = scratch / ".agent-handoff-toolkit" / "runner.py"
    observer.write_text(
        _observer_script(runner, trace, run_id), encoding="utf-8", newline="\n"
    )
    config = scratch / (
        ".claude/settings.json" if platform == "claude" else ".codex/hooks.json"
    )
    value = json.loads(config.read_text(encoding="utf-8"))
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("installed hook configuration is invalid")
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            raise ValueError("installed hook configuration is invalid")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ValueError("installed hook configuration is invalid")
            for hook in entry["hooks"]:
                if not isinstance(hook, dict) or not isinstance(
                    hook.get("command"), str
                ):
                    raise ValueError("installed hook configuration is invalid")
                command = subprocess.list2cmdline(
                    [sys.executable, str(observer), platform, event.lower()]
                )
                hook["command"] = command
                if "commandWindows" in hook:
                    hook["commandWindows"] = command
    config.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    trace.touch()
    return trace, config


def _read_evidence(
    trace: Path, run_id: str
) -> tuple[bool, bool, bool, bool, tuple[str, ...]]:
    if not _is_safe_regular_file(trace, trace.parent):
        return False, False, False, False, ()
    try:
        raw = trace.read_bytes()
    except OSError:
        return False, False, False, False, ()
    if len(raw) > _MAX_TRACE_BYTES:
        return False, False, False, False, ()
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, False, False, False, ()
        if not (
            isinstance(value, dict)
            and set(value).issubset({"run_id", "event", "outcome", "issue_code"})
            and value.get("run_id") == run_id
            and isinstance(value.get("event"), str)
            and isinstance(value.get("outcome"), str)
        ):
            return False, False, False, False, ()
        entries.append(value)
    expected_issue = next(iter(_EXPECTED_ISSUES))
    expected = [
        ("userpromptsubmit", "observed", None),
        ("stop", "block", expected_issue),
        ("stop", "allow", None),
    ]
    normalized = [
        (
            entry["event"].replace("-", "").lower(),
            entry["outcome"],
            entry.get("issue_code"),
        )
        for entry in entries
    ]
    if normalized != expected:
        return bool(entries), False, False, False, ()
    return True, True, True, True, (expected_issue,)


def _empty_result(platform: str) -> AcceptanceResult:
    return AcceptanceResult(
        platform, False, False, False, False, False, False, (), False
    )


def run_acceptance(
    platform: str,
    *,
    scratch: Path | None = None,
    runner: Runner = _run_process,
    source_root: Path | None = None,
    evidence_verifier: EvidenceVerifier | None = None,
) -> AcceptanceResult:
    """Exercise a disposable installed host integration using observer evidence only."""

    root = _release_source(source_root)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if scratch is None:
        temporary = tempfile.TemporaryDirectory(prefix="handoff-acceptance-")
        scratch_path = Path(temporary.name) / "consumer"
    else:
        scratch_path = scratch.resolve()
    sentinel = f"synthetic-{uuid.uuid4().hex}"
    run_id = uuid.uuid4().hex
    result = _empty_result(platform)
    owns_scratch = False

    try:
        if scratch_path.exists():
            raise ValueError("scratch path must not already exist")
        scratch_path.mkdir(parents=True)
        owns_scratch = True
        runtime = scratch_path / ".agent-handoff-toolkit" / "acceptance-runtime"
        runtime.mkdir(parents=True)
        git = runner(
            ("git", "init", "--quiet"), cwd=scratch_path, input_text="", env=os.environ
        )
        install = runner(
            (
                sys.executable,
                str(root / "distribution" / "runner.py"),
                "install",
                "--target",
                str(scratch_path),
                "--release",
                "v0.3.0",
                "--apply",
            ),
            cwd=root,
            input_text="",
            env=os.environ,
        )
        if (
            git.returncode != 0
            or install.returncode != 0
            or git.timed_out
            or install.timed_out
        ):
            return result
        trace, settings = _configure_observer(platform, scratch_path, runtime, run_id)
        host = runner(
            _host_command(platform, settings),
            cwd=scratch_path,
            input_text=_scenario_input(sentinel),
            env={
                **os.environ,
                "NO_COLOR": "1",
                "CLAUDE_CONFIG_DIR": str(runtime / "claude-home"),
                "CODEX_HOME": str(runtime / "codex-home"),
            },
        )
        discovered, blocked, issue_received, corrected, codes = _read_evidence(
            trace, run_id
        )
        trusted = False
        if evidence_verifier is not None:
            try:
                trusted = evidence_verifier(trace, run_id)
            except Exception:
                trusted = False
        corrected = corrected and host.returncode == 0 and not host.timed_out
        retained_content = not _contains_sentinel(scratch_path, sentinel)
        result = AcceptanceResult(
            platform=platform,
            discovered=discovered,
            blocked=blocked,
            issue_received=issue_received,
            corrected=corrected,
            retained_content=retained_content,
            block_cap_compatible=blocked and corrected,
            issue_codes=codes,
            observed=discovered and trusted,
        )
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return result
    finally:
        if owns_scratch and scratch_path.exists():
            shutil.rmtree(scratch_path)
        if temporary is not None:
            temporary.cleanup()


def format_result(result: AcceptanceResult, _content: str = "") -> str:
    """Render only reduced properties; caller content is deliberately ignored."""

    lines = [f"platform={result.platform}", f"status={result.status}"]
    lines.extend(
        f"{name}={'pass' if getattr(result, name) else 'fail'}"
        for name in _REQUIRED_PROPERTIES
    )
    lines.append("issue_codes=" + ",".join(result.issue_codes))
    return "\n".join(lines)
