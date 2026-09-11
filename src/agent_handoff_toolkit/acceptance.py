"""Opt-in, content-redacting lifecycle host acceptance checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence
import uuid


_ISSUE_CODE = re.compile(r"\bAHK-[A-Z0-9-]+\b")
_REQUIRED_PROPERTIES = (
    "discovered",
    "blocked",
    "issue_received",
    "corrected",
    "retained_content",
    "block_cap_compatible",
)


@dataclass(frozen=True)
class HostRun:
    """Captured process streams that must remain in memory only."""

    returncode: int
    stdout: str
    stderr: str


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


Runner = Callable[..., HostRun]


def _run_process(
    command: Sequence[str], *, cwd: Path, input_text: str, env: Mapping[str, str]
) -> HostRun:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        input=input_text,
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=25,
    )
    return HostRun(completed.returncode, completed.stdout, completed.stderr)


def _host_command(platform: str) -> tuple[str, ...]:
    if platform == "claude":
        return ("claude", "-p", "--model", "haiku")
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
        f"{sentinel}. Attempt one invalid tracked stop, receive its AHK issue, "
        "then submit a corrected stop. Emit lifecycle booleans and issue codes only."
    )


def _json_objects(raw: str) -> list[Mapping[str, object]]:
    objects: list[Mapping[str, object]] = []
    for line in raw.splitlines() or (raw,):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _event_value(objects: Sequence[Mapping[str, object]], name: str) -> bool:
    for value in objects:
        candidate = value.get(name)
        if isinstance(candidate, bool) and candidate:
            return True
    return False


def _event_codes(raw: str, objects: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    codes = set(_ISSUE_CODE.findall(raw))
    for value in objects:
        candidate = value.get("issue_codes")
        if isinstance(candidate, list):
            codes.update(
                item
                for item in candidate
                if isinstance(item, str) and _ISSUE_CODE.fullmatch(item)
            )
    return tuple(sorted(codes))


def _contains_sentinel(root: Path, sentinel: str) -> bool:
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as source:
                while chunk := source.read(8192):
                    if sentinel in chunk:
                        return True
        except OSError:
            return True
    return False


def _empty_result(platform: str, issue_codes: tuple[str, ...] = ()) -> AcceptanceResult:
    return AcceptanceResult(
        platform, False, False, False, False, False, False, issue_codes, False
    )


def run_acceptance(
    platform: str,
    *,
    scratch: Path | None = None,
    runner: Runner = _run_process,
    source_root: Path | None = None,
) -> AcceptanceResult:
    """Exercise one installed host integration and discard all raw process content."""

    _host_command(platform)
    root = (source_root or Path(__file__).resolve().parents[2]).resolve()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if scratch is None:
        temporary = tempfile.TemporaryDirectory(prefix="handoff-acceptance-")
        scratch_path = Path(temporary.name) / "consumer"
    else:
        scratch_path = scratch.resolve()
    sentinel = f"synthetic-{uuid.uuid4().hex}"
    result = _empty_result(platform)
    owns_scratch = False

    try:
        if scratch_path.exists():
            raise ValueError("scratch path must not already exist")
        scratch_path.mkdir(parents=True)
        owns_scratch = True
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
        if git.returncode != 0 or install.returncode != 0:
            return result

        host = runner(
            _host_command(platform),
            cwd=scratch_path,
            input_text=_scenario_input(sentinel),
            env={**os.environ, "NO_COLOR": "1"},
        )
        raw = host.stdout + host.stderr
        objects = _json_objects(raw)
        codes = _event_codes(raw, objects)
        discovered = _event_value(objects, "hook_discovered")
        result = AcceptanceResult(
            platform=platform,
            discovered=discovered,
            blocked=_event_value(objects, "blocked"),
            issue_received=bool(codes),
            corrected=_event_value(objects, "corrected"),
            retained_content=not _contains_sentinel(scratch_path, sentinel),
            block_cap_compatible=_event_value(objects, "block_cap_compatible"),
            issue_codes=codes,
            observed=host.returncode != 127 and discovered,
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
