"""Parsing and deterministic rendering for installed ownership state."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re

from .manifest import path_collision_key, strict_json_loads, validate_relative_path


STATE_RELATIVE_PATH = PurePosixPath(".agent-handoff-toolkit/install-state.json")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODES = frozenset({"copy", "managed-block", "merge-json"})


class StateError(ValueError):
    """Raised when an installed state file is malformed or unsafe."""


@dataclass(frozen=True)
class TargetState:
    target: str
    source: str
    mode: str
    installed_sha256: str
    owned_fragment: object | None = None


@dataclass(frozen=True)
class InstalledState:
    state_version: int
    release: str
    toolkit_version: str
    record_schema_version: int
    targets: tuple[TargetState, ...]


def _state_error(error: ValueError) -> StateError:
    return StateError(str(error))


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StateError(f"{label} must be an object")
    return value


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateError(f"{label} must be a non-empty string")
    return value


def _require_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateError(f"{label} must be an integer")
    return value


def _validate_path(value: object, *, kind: str) -> str:
    try:
        return validate_relative_path(value, kind=kind).as_posix()
    except ValueError as error:
        raise _state_error(error) from error


def _validate_target_location(target_root: Path, target: str) -> None:
    root = target_root.resolve()
    candidate = root / Path(*PurePosixPath(target).parts)
    for path in (candidate.parent, candidate):
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise StateError("target escapes target root") from error


def _target_from_mapping(value: object, target_root: Path | None) -> TargetState:
    target = _require_mapping(value, label="state target")
    allowed = {"target", "source", "mode", "installed_sha256", "owned_fragment"}
    required = {"target", "source", "mode", "installed_sha256"}
    if not required.issubset(target) or not set(target).issubset(allowed):
        raise StateError("state target has unexpected or missing fields")
    target_path = _validate_path(target["target"], kind="target")
    source_path = _validate_path(target["source"], kind="source")
    mode = _require_string(target["mode"], label="mode")
    if mode not in _MODES:
        raise StateError("unknown mode")
    digest = target["installed_sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise StateError("installed sha256 must be 64 lowercase hexadecimal characters")
    has_fragment = "owned_fragment" in target
    if mode == "merge-json" and (
        not has_fragment or not isinstance(target["owned_fragment"], dict)
    ):
        raise StateError("merge-json target requires an owned fragment object")
    if mode != "merge-json" and has_fragment:
        raise StateError("owned fragment is only valid for merge-json")
    if target_root is not None:
        _validate_target_location(target_root, target_path)
    return TargetState(
        target_path,
        source_path,
        mode,
        digest,
        target.get("owned_fragment"),
    )


def _state_from_mapping(
    payload: object, target_root: Path | None = None
) -> InstalledState:
    state = _require_mapping(payload, label="state")
    expected = {
        "state_version",
        "release",
        "toolkit_version",
        "record_schema_version",
        "targets",
    }
    if set(state) != expected:
        raise StateError("state has unexpected or missing fields")
    if _require_int(state["state_version"], label="state version") != 1:
        raise StateError("state version must be 1")
    release = _require_string(state["release"], label="release")
    toolkit_version = _require_string(state["toolkit_version"], label="toolkit version")
    if release != f"v{toolkit_version}":
        raise StateError("release does not match toolkit version")
    record_schema_version = _require_int(
        state["record_schema_version"], label="record schema version"
    )
    values = state["targets"]
    if not isinstance(values, list):
        raise StateError("targets must be an array")
    targets = tuple(_target_from_mapping(value, target_root) for value in values)
    normalized_targets = [path_collision_key(target.target) for target in targets]
    if len(set(normalized_targets)) != len(normalized_targets):
        raise StateError("target collision")
    return InstalledState(
        1,
        release,
        toolkit_version,
        record_schema_version,
        targets,
    )


def _state_path(target_root: Path) -> Path:
    root = target_root.resolve()
    state_path = root / Path(*STATE_RELATIVE_PATH.parts)
    for path in (state_path.parent, state_path):
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise StateError("state path escapes target root") from error
    return state_path


def _state_payload(state: InstalledState) -> dict[str, object]:
    return {
        "state_version": state.state_version,
        "release": state.release,
        "toolkit_version": state.toolkit_version,
        "record_schema_version": state.record_schema_version,
        "targets": [
            {
                "target": target.target,
                "source": target.source,
                "mode": target.mode,
                "installed_sha256": target.installed_sha256,
                **(
                    {"owned_fragment": target.owned_fragment}
                    if target.mode == "merge-json"
                    else {}
                ),
            }
            for target in state.targets
        ],
    }


def load_state_snapshot(
    target_root: Path,
) -> tuple[InstalledState | None, bytes | None]:
    """Load state and return the exact raw bytes used to parse it."""

    root = Path(target_root)
    state_path = _state_path(root)
    try:
        raw_state = state_path.read_bytes()
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as error:
        raise StateError("invalid state JSON") from error
    try:
        payload = strict_json_loads(raw_state.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StateError("invalid state JSON") from error
    except ValueError as error:
        raise StateError(str(error)) from error
    return _state_from_mapping(payload, root), raw_state


def load_state(target_root: Path) -> InstalledState | None:
    """Load the consumer ownership record, if it exists."""

    state, _ = load_state_snapshot(target_root)
    return state


def render_state(state: InstalledState) -> bytes:
    """Render installed state with stable target ordering and an LF terminator."""

    state = _state_from_mapping(_state_payload(state))
    targets = []
    for target in sorted(
        state.targets, key=lambda item: path_collision_key(item.target)
    ):
        item: dict[str, object] = {
            "installed_sha256": target.installed_sha256,
            "mode": target.mode,
            "source": target.source,
            "target": target.target,
        }
        if target.mode == "merge-json":
            item["owned_fragment"] = target.owned_fragment
        targets.append(item)
    payload = {
        "record_schema_version": state.record_schema_version,
        "release": state.release,
        "state_version": state.state_version,
        "targets": targets,
        "toolkit_version": state.toolkit_version,
    }
    return (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
