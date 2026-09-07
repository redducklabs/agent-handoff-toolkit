"""Build immutable, zero-write plans for managed repository installation."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import difflib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Literal
import uuid

from .manifest import (
    Artifact,
    load_manifest,
    normalize_text,
    path_collision_key,
    strict_json_loads,
    text_sha256,
)
from .operations import (
    OperationConflict,
    merge_copy,
    merge_json_fragment,
    merge_managed_block,
)
from .state import (
    STATE_RELATIVE_PATH,
    InstalledState,
    StateError,
    TargetState,
    load_state_snapshot,
    render_state,
)


@dataclass(frozen=True)
class FileChange:
    target: Path
    relative_target: str
    before: bytes | None
    after: bytes
    mode: str


@dataclass(frozen=True)
class Conflict:
    target: str
    code: str
    message: str


@dataclass(frozen=True)
class InstallPlan:
    operation: str
    release: str
    target_root: Path
    changes: tuple[FileChange, ...]
    conflicts: tuple[Conflict, ...]
    state: InstalledState | None


class ApplyError(RuntimeError):
    """Raised when an immutable installation plan cannot be applied safely."""


class _AtomicOperationError(ApplyError):
    """Preserve every stage of a failed atomic operation and its target state."""

    def __init__(
        self,
        primary_error: Exception | None,
        *,
        recovery_errors: tuple[Exception, ...] = (),
        release_errors: tuple[Exception, ...] = (),
        target_changed: bool,
    ) -> None:
        self.primary_error = primary_error
        self.recovery_errors = recovery_errors
        self.release_errors = release_errors
        self.target_changed = target_changed
        parts: list[str] = []
        if primary_error is not None:
            parts.append(str(primary_error))
        if recovery_errors:
            details = "; ".join(str(error) for error in recovery_errors)
            parts.append(f"recovery failed: {details}")
        if release_errors:
            details = "; ".join(str(error) for error in release_errors)
            parts.append(f"release failed: {details}")
        super().__init__("; ".join(parts))

    def with_release_error(self, error: Exception) -> _AtomicOperationError:
        return _AtomicOperationError(
            self.primary_error,
            recovery_errors=self.recovery_errors,
            release_errors=(*self.release_errors, error),
            target_changed=self.target_changed,
        )


def _target_path(target_root: Path, relative_target: PurePosixPath) -> Path:
    target = target_root / Path(*relative_target.parts)
    current = target_root
    for component in relative_target.parts[:-1]:
        current /= component
        if os.path.lexists(current) and not current.is_dir():
            raise OperationConflict(
                "target-parent-not-directory",
                "an existing target parent is not a directory",
            )
    for path in (target.parent, target):
        try:
            path.resolve(strict=False).relative_to(target_root)
        except ValueError as error:
            raise OperationConflict(
                "target-escape", "target resolves outside the target repository"
            ) from error
    return target


def _read_target(target: Path) -> bytes | None:
    if not target.exists():
        return None
    try:
        return target.read_bytes()
    except OSError as error:
        raise OperationConflict("target-unreadable", "target cannot be read") from error


def _parse_json(data: bytes, *, label: str) -> dict[str, object]:
    try:
        value = strict_json_loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OperationConflict("invalid-json", f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise OperationConflict("invalid-json", f"{label} must be a JSON object")
    return value


def _newline_for(data: bytes) -> bytes:
    for index, value in enumerate(data):
        if value == 13:
            return b"\r\n" if data[index : index + 2] == b"\r\n" else b"\r"
        if value == 10:
            return b"\n"
    return b"\n"


def _render_json(value: object, newline: bytes) -> bytes:
    rendered = (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    return rendered if newline == b"\n" else rendered.replace(b"\n", newline)


def _precondition_conflict(
    operation: str,
    release: str,
    target_root: Path,
    code: str,
    message: str,
) -> InstallPlan:
    return InstallPlan(
        operation,
        release,
        target_root,
        (),
        (Conflict(STATE_RELATIVE_PATH.as_posix(), code, message),),
        None,
    )


def build_plan(
    source_root: Path,
    target_root: Path,
    release: str,
    operation: Literal["install", "sync"],
) -> InstallPlan:
    """Build a complete installation plan without modifying the target."""

    if operation not in {"install", "sync"}:
        raise ValueError("operation must be install or sync")
    manifest = load_manifest(Path(source_root))
    source = Path(source_root).resolve()
    target = Path(target_root).resolve()
    if release != manifest.release:
        return _precondition_conflict(
            operation,
            release,
            target,
            "release-mismatch",
            "requested release does not match the source manifest",
        )
    if source == target:
        return _precondition_conflict(
            operation,
            release,
            target,
            "self-install",
            "source and target repositories must be different",
        )
    if not target.is_dir():
        return _precondition_conflict(
            operation,
            release,
            target,
            "invalid-target-root",
            "target repository is not a directory",
        )

    state_relative = STATE_RELATIVE_PATH.as_posix()
    if any(
        path_collision_key(artifact_target) == path_collision_key(state_relative)
        for artifact in manifest.artifacts
        for artifact_target in artifact.targets
    ):
        return _precondition_conflict(
            operation,
            release,
            target,
            "reserved-state-target",
            "source manifest cannot install an artifact over installed state",
        )

    try:
        state_target = _target_path(target, STATE_RELATIVE_PATH)
        installed_state, state_before = load_state_snapshot(target)
    except StateError as error:
        if str(error) != "state path escapes target root":
            raise
        return _precondition_conflict(
            operation,
            release,
            target,
            "state-path-escape",
            "installed state path resolves outside the target repository",
        )
    except OperationConflict as error:
        if error.code == "target-escape":
            code = "state-path-escape"
            message = "installed state path resolves outside the target repository"
        else:
            code = error.code
            message = str(error)
        return _precondition_conflict(
            operation,
            release,
            target,
            code,
            message,
        )
    if operation == "install" and installed_state is not None:
        return _precondition_conflict(
            operation,
            release,
            target,
            "already-installed",
            "installed state already exists",
        )
    if operation == "sync" and installed_state is None:
        return _precondition_conflict(
            operation,
            release,
            target,
            "not-installed",
            "installed state is required for synchronization",
        )

    previous = (
        {path_collision_key(item.target): item for item in installed_state.targets}
        if installed_state is not None
        else {}
    )
    manifest_targets = {
        path_collision_key(relative): relative.as_posix()
        for artifact in manifest.artifacts
        for relative in artifact.targets
    }
    if operation == "sync" and installed_state is not None:
        omitted = tuple(
            item
            for item in installed_state.targets
            if path_collision_key(item.target) not in manifest_targets
        )
        if omitted:
            return InstallPlan(
                operation,
                release,
                target,
                (),
                tuple(
                    Conflict(
                        item.target,
                        "managed-target-omitted",
                        "previously managed target is absent from the source manifest; explicit migration is required",
                    )
                    for item in sorted(
                        omitted, key=lambda value: path_collision_key(value.target)
                    )
                ),
                None,
            )
    changes: list[FileChange] = []
    conflicts: list[Conflict] = []
    next_targets: list[TargetState] = []
    planned_targets: list[tuple[str, Path, Artifact, bytes, TargetState | None]] = []
    for artifact in manifest.artifacts:
        source_path = source / Path(*artifact.source.parts)
        source_bytes = source_path.read_bytes()
        if text_sha256(source_bytes) != artifact.sha256:
            conflicts.append(
                Conflict(
                    artifact.source.as_posix(),
                    "source-changed",
                    "source content changed while the plan was built",
                )
            )
            continue
        for relative in artifact.targets:
            relative_name = relative.as_posix()
            try:
                path = _target_path(target, relative)
            except OperationConflict as error:
                conflicts.append(Conflict(relative_name, error.code, str(error)))
                continue
            old = previous.get(path_collision_key(relative_name))
            if old is not None and (
                old.target != relative_name
                or old.source != artifact.source.as_posix()
                or old.mode != artifact.mode
            ):
                conflicts.append(
                    Conflict(
                        relative_name,
                        "state-target-mismatch",
                        "installed state does not match the source manifest",
                    )
                )
                continue
            planned_targets.append(
                (
                    relative_name,
                    path,
                    artifact,
                    source_bytes,
                    old,
                )
            )

    for relative_name, path, artifact, source_bytes, old in sorted(
        planned_targets, key=lambda item: path_collision_key(item[0])
    ):
        try:
            before = _read_target(path)
            owned_fragment: object | None = None
            installed_sha256 = artifact.sha256
            if artifact.mode == "copy":
                after = merge_copy(
                    before,
                    source_bytes,
                    old.installed_sha256 if old is not None else None,
                )
            elif artifact.mode == "managed-block":
                if artifact.block_id is None:
                    raise OperationConflict(
                        "invalid-managed-block", "managed block id is missing"
                    )
                after = merge_managed_block(
                    before,
                    source_bytes,
                    artifact.block_id,
                    old.installed_sha256 if old is not None else None,
                )
                installed_sha256 = text_sha256(
                    merge_managed_block(None, source_bytes, artifact.block_id)
                )
            elif artifact.mode == "merge-json":
                desired_json = _parse_json(source_bytes, label="source JSON fragment")
                current_json = (
                    {} if before is None else _parse_json(before, label="target JSON")
                )
                if old is not None and not isinstance(old.owned_fragment, dict):
                    raise OperationConflict(
                        "invalid-owned-json",
                        "installed JSON ownership fragment must be an object",
                    )
                identities = {
                    identity.pointer: identity.fields
                    for identity in artifact.array_identities
                }
                merged = merge_json_fragment(
                    current_json,
                    desired_json,
                    identities,
                    old.owned_fragment if old is not None else None,
                )
                after = _render_json(merged, _newline_for(before or b""))
                owned_fragment = desired_json
            else:
                raise OperationConflict(
                    "unsupported-mode",
                    f"planning for {artifact.mode} is not implemented",
                )
        except OperationConflict as error:
            conflicts.append(Conflict(relative_name, error.code, str(error)))
            continue
        if before != after:
            changes.append(
                FileChange(path, relative_name, before, after, artifact.mode)
            )
        next_targets.append(
            TargetState(
                relative_name,
                artifact.source.as_posix(),
                artifact.mode,
                installed_sha256,
                owned_fragment,
            )
        )

    if conflicts:
        return InstallPlan(
            operation,
            release,
            target,
            (),
            tuple(sorted(conflicts, key=lambda item: path_collision_key(item.target))),
            None,
        )

    next_state = InstalledState(
        state_version=1,
        release=release,
        toolkit_version=manifest.toolkit_version,
        record_schema_version=manifest.record_schema_version,
        targets=tuple(
            sorted(next_targets, key=lambda item: path_collision_key(item.target))
        ),
    )
    state_after = render_state(next_state)
    if state_before != state_after:
        changes.append(
            FileChange(state_target, state_relative, state_before, state_after, "state")
        )
    return InstallPlan(
        operation,
        release,
        target,
        tuple(changes),
        (),
        next_state,
    )


_OMIT = object()


def _json_diff_views(before: object, after: object) -> tuple[object, object]:
    if before == after:
        return _OMIT, _OMIT
    if isinstance(before, dict) and isinstance(after, dict):
        before_view: dict[str, object] = {}
        after_view: dict[str, object] = {}
        for key, before_value in before.items():
            if key not in after:
                before_view[key] = before_value
                continue
            child_before, child_after = _json_diff_views(before_value, after[key])
            if child_before is not _OMIT:
                before_view[key] = child_before
                after_view[key] = child_after
        for key, after_value in after.items():
            if key not in before:
                after_view[key] = after_value
        return before_view, after_view
    if isinstance(before, list) and isinstance(after, list):
        used_after: set[int] = set()
        before_view = []
        for before_value in before:
            match = next(
                (
                    index
                    for index, after_value in enumerate(after)
                    if index not in used_after and after_value == before_value
                ),
                None,
            )
            if match is None:
                before_view.append(before_value)
            else:
                used_after.add(match)
        after_view = [
            value for index, value in enumerate(after) if index not in used_after
        ]
        if not before_view and not after_view:
            return before, after
        return before_view, after_view
    return before, after


def _diff_text(change: FileChange) -> tuple[list[str], list[str]]:
    if change.mode != "merge-json":
        return (
            normalize_text(change.before or b"").decode("utf-8").splitlines(),
            normalize_text(change.after).decode("utf-8").splitlines(),
        )
    before_json = _parse_json(change.before or b"{}", label="planned JSON before")
    after_json = _parse_json(change.after, label="planned JSON after")
    before_view, after_view = _json_diff_views(before_json, after_json)
    if before_view is _OMIT:
        return (
            ["<unchanged consumer JSON formatting>"],
            ["<normalized consumer JSON formatting>"],
        )
    return (
        _render_json(before_view, b"\n").decode("utf-8").splitlines(),
        _render_json(after_view, b"\n").decode("utf-8").splitlines(),
    )


def _unified_diff(change: FileChange) -> tuple[str, ...]:
    before, after = _diff_text(change)
    return tuple(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{change.relative_target}",
            tofile=f"b/{change.relative_target}",
            n=0,
            lineterm="",
        )
    )


def render_plan(plan: InstallPlan) -> str:
    """Render concise statuses and repository-relative unified diffs."""

    lines = [f"{plan.operation}: {plan.release}"]
    for conflict in plan.conflicts:
        lines.append(f"CONFLICT {conflict.target}: {conflict.code}: {conflict.message}")
    for change in plan.changes:
        lines.append(f"CHANGE {change.relative_target}")
        lines.extend(_unified_diff(change))
    if not plan.conflicts and not plan.changes:
        lines.append("CURRENT")
    return "\n".join(lines) + "\n"


def _current_bytes(target: Path) -> bytes | None:
    if not target.exists():
        return None
    return target.read_bytes()


def _verify_target_containment(target: Path, target_root: Path) -> None:
    try:
        root = target_root.resolve(strict=True)
        target.relative_to(target_root)
        current = target
        while True:
            current.resolve(strict=False).relative_to(root)
            if current == target_root:
                return
            current = current.parent
    except (OSError, RuntimeError, ValueError) as error:
        raise ApplyError("target path resolves outside the target root") from error


def _verify_change_precondition(change: FileChange, target_root: Path) -> None:
    try:
        _verify_target_containment(change.target, target_root)
        current = _current_bytes(change.target)
    except OSError as error:
        raise ApplyError(
            f"{change.relative_target}: unable to read target before applying"
        ) from error
    if current != change.before:
        raise ApplyError(f"{change.relative_target}: changed after planning")


def _verify_preconditions(plan: InstallPlan) -> None:
    for change in plan.changes:
        _verify_change_precondition(change, plan.target_root)


@dataclass
class _BoundParent:
    target: Path
    target_root: Path
    name: str
    parent: Path
    directory_fd: int | None = None
    posix_directory_fds: tuple[int, ...] = ()
    posix_components: tuple[str, ...] = ()
    posix_restore_mode: int | None = None
    windows_handles: tuple[int, ...] = ()
    windows_guards: tuple[Path, ...] = ()
    windows_directories: tuple[tuple[int, Path], ...] = ()

    def close(self) -> None:
        errors: list[Exception] = []
        if self.posix_directory_fds:
            for descriptor in reversed(self.posix_directory_fds):
                try:
                    os.close(descriptor)
                except OSError as error:
                    errors.append(error)
        elif self.directory_fd is not None:
            try:
                os.close(self.directory_fd)
            except OSError as error:
                errors.append(error)
        if os.name == "nt":
            try:
                _windows_release_resources(self.windows_handles, self.windows_guards)
            except (ApplyError, OSError) as error:
                errors.append(error)
        if errors:
            details = "; ".join(str(error) for error in errors)
            raise ApplyError(f"unable to release bound parent: {details}")


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _verify_posix_bound_parent(bound: _BoundParent) -> None:
    if not bound.posix_directory_fds:
        return
    try:
        root_stat = os.stat(bound.target_root, follow_symlinks=False)
        if not _same_directory(root_stat, os.fstat(bound.posix_directory_fds[0])):
            raise ApplyError("target root changed while applying")
        for index, component in enumerate(bound.posix_components):
            parent_fd = bound.posix_directory_fds[index]
            child_fd = bound.posix_directory_fds[index + 1]
            component_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_directory(component_stat, os.fstat(child_fd)):
                raise ApplyError("target parent changed while applying")
    except OSError as error:
        raise ApplyError("target parent changed while applying") from error


def _posix_operation_parent_fd(bound: _BoundParent) -> int:
    """Resolve the parent again from the held root descriptor without symlinks."""

    assert bound.posix_directory_fds
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.dup(bound.posix_directory_fds[0])
    try:
        for index, component in enumerate(bound.posix_components):
            child = os.open(component, flags, dir_fd=descriptor)
            if not _same_directory(
                os.fstat(child), os.fstat(bound.posix_directory_fds[index + 1])
            ):
                os.close(child)
                raise ApplyError("target parent changed while applying")
            os.close(descriptor)
            descriptor = child
        _verify_posix_bound_parent(bound)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_bound_parent(bound: _BoundParent) -> None:
    if bound.directory_fd is not None:
        _verify_posix_bound_parent(bound)
    elif os.name == "nt":
        _windows_verify_bound_parent(bound)
    _verify_target_containment(bound.target, bound.target_root)


def _posix_open_parent(target: Path, target_root: Path) -> _BoundParent:
    _verify_target_containment(target, target_root)
    relative = target.relative_to(target_root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(target_root, flags)
    descriptors = [directory_fd]
    try:
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o777, dir_fd=directory_fd)
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            descriptors.append(child_fd)
            directory_fd = child_fd
        _verify_target_containment(target, target_root)
        bound = _BoundParent(
            target,
            target_root,
            relative.name,
            target.parent,
            directory_fd=directory_fd,
            posix_directory_fds=tuple(descriptors),
            posix_components=tuple(relative.parts[:-1]),
        )
        _verify_posix_bound_parent(bound)
        return bound
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    )
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _windows_close_handle(handle: int) -> None:
    if not _kernel32().CloseHandle(ctypes.c_void_p(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_release_resources(
    handles: tuple[int, ...], guards: tuple[Path, ...]
) -> None:
    errors: list[OSError] = []
    for handle in reversed(handles):
        try:
            _windows_close_handle(handle)
        except OSError as error:
            errors.append(error)
    for guard in reversed(guards):
        try:
            guard.unlink(missing_ok=True)
        except OSError as error:
            errors.append(error)
    if errors:
        details = "; ".join(str(error) for error in errors)
        raise ApplyError(f"unable to release Windows install lock: {details}")


def _windows_final_path(handle: int) -> Path:
    kernel32 = _kernel32()
    needed = kernel32.GetFinalPathNameByHandleW(ctypes.c_void_p(handle), None, 0, 0)
    if not needed:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = kernel32.GetFinalPathNameByHandleW(
        ctypes.c_void_p(handle), buffer, len(buffer), 0
    )
    if not written or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_verify_bound_parent(bound: _BoundParent) -> None:
    try:
        for handle, expected in bound.windows_directories:
            actual = _windows_final_path(handle)
            if os.path.normcase(str(actual)) != os.path.normcase(
                str(expected.resolve(strict=True))
            ):
                raise ApplyError("target parent changed while applying")
    except OSError as error:
        raise ApplyError("target parent changed while applying") from error


def _windows_open_directory(path: Path) -> int:
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        0x80,
        0x00000001 | 0x00000002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _windows_open_guard(path: Path) -> int:
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        str(path), 0x80000000, 0x00000001 | 0x00000002, None, 3, 0, None
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _windows_lock_directory(path: Path) -> tuple[int, Path]:
    guard = path / f".agent-handoff-lock-{uuid.uuid4().hex}"
    descriptor = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        return _windows_open_guard(guard), guard
    except BaseException:
        guard.unlink(missing_ok=True)
        raise


def _windows_open_parent(target: Path, target_root: Path) -> _BoundParent:
    _verify_target_containment(target, target_root)
    relative = target.relative_to(target_root)
    current = target_root
    handles: list[int] = []
    guards: list[Path] = []
    directories: list[tuple[int, Path]] = []
    try:
        directory_handle = _windows_open_directory(current)
        handles.append(directory_handle)
        directories.append((directory_handle, current))
        guard_handle, guard = _windows_lock_directory(current)
        handles.append(guard_handle)
        guards.append(guard)
        for component in relative.parts[:-1]:
            child = current / component
            child.mkdir(exist_ok=True)
            _verify_target_containment(child, target_root)
            directory_handle = _windows_open_directory(child)
            handles.append(directory_handle)
            directories.append((directory_handle, child))
            guard_handle, guard = _windows_lock_directory(child)
            handles.append(guard_handle)
            guards.append(guard)
            _verify_target_containment(child, target_root)
            current = child
        _verify_target_containment(target, target_root)
        bound = _BoundParent(
            target,
            target_root,
            relative.name,
            current,
            windows_handles=tuple(handles),
            windows_guards=tuple(guards),
            windows_directories=tuple(directories),
        )
        _windows_verify_bound_parent(bound)
        return bound
    except BaseException:
        _windows_release_resources(tuple(handles), tuple(guards))
        raise


def _open_bound_parent(target: Path, target_root: Path) -> _BoundParent:
    if os.name == "nt":
        return _windows_open_parent(target, target_root)
    return _posix_open_parent(target, target_root)


def _temporary_name(target_name: str) -> str:
    return f".{target_name}.agent-handoff-tmp-{uuid.uuid4().hex}"


def _temporary_sibling_windows(bound: _BoundParent) -> tuple[int, Path]:
    _verify_bound_parent(bound)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{bound.name}.agent-handoff-tmp-", dir=bound.parent
    )
    temporary = Path(name)
    try:
        _verify_bound_parent(bound)
        _verify_target_containment(temporary, bound.target_root)
    except ApplyError:
        os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return descriptor, temporary


def _read_posix_bound_file(bound: _BoundParent, directory_fd: int) -> bytes | None:
    assert bound.directory_fd is not None
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(bound.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _posix_bound_file_mode(bound: _BoundParent, directory_fd: int) -> int | None:
    try:
        result = os.stat(bound.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(result.st_mode)


def _replace_posix_bound_file(
    bound: _BoundParent, content: bytes, directory_fd: int
) -> None:
    """Restore bytes through the held directory descriptor without path traversal."""

    assert bound.directory_fd is not None
    temporary = _temporary_name(bound.name)
    existing_mode = _posix_bound_file_mode(bound, directory_fd)
    desired_mode = (
        existing_mode if existing_mode is not None else bound.posix_restore_mode
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o666 if desired_mode is None else 0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            if desired_mode is not None:
                os.fchmod(stream.fileno(), desired_mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            bound.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except OSError as error:
        cleanup_errors: list[Exception] = []
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise _AtomicOperationError(
                error,
                recovery_errors=tuple(cleanup_errors),
                target_changed=False,
            ) from error
        raise


def _restore_posix_bound_file(
    bound: _BoundParent, before: bytes | None, directory_fd: int
) -> None:
    assert bound.directory_fd is not None
    if before is None:
        try:
            os.unlink(bound.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        return
    _replace_posix_bound_file(bound, before, directory_fd)


def _atomic_replace_posix(bound: _BoundParent, content: bytes) -> None:
    assert bound.directory_fd is not None
    _verify_bound_parent(bound)
    directory_fd = _posix_operation_parent_fd(bound)
    failure: _AtomicOperationError | None = None
    cause: Exception | None = None
    target_changed = False
    try:
        before = _read_posix_bound_file(bound, directory_fd)
        before_mode = _posix_bound_file_mode(bound, directory_fd)
        bound.posix_restore_mode = before_mode
        temporary = _temporary_name(bound.name)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o666 if before_mode is None else 0o600,
            dir_fd=directory_fd,
        )
        replaced = False
        try:
            _verify_bound_parent(bound)
        except (ApplyError, OSError) as error:
            cleanup_errors: list[Exception] = []
            try:
                os.close(descriptor)
            except OSError as close_error:
                cleanup_errors.append(close_error)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise _AtomicOperationError(
                    error,
                    recovery_errors=tuple(cleanup_errors),
                    target_changed=False,
                ) from error
            raise
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                if before_mode is not None:
                    os.fchmod(stream.fileno(), before_mode)
                stream.flush()
                os.fsync(stream.fileno())
            _verify_bound_parent(bound)
            os.replace(
                temporary,
                bound.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            replaced = True
            _verify_bound_parent(bound)
        except (ApplyError, OSError) as error:
            recovery_errors: list[Exception] = []
            try:
                if replaced:
                    _restore_posix_bound_file(bound, before, directory_fd)
                else:
                    os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                if replaced:
                    recovery_errors.append(
                        OSError("replacement recovery target disappeared")
                    )
            except (ApplyError, OSError) as recovery_error:
                recovery_errors.append(recovery_error)
            if recovery_errors:
                raise _AtomicOperationError(
                    error,
                    recovery_errors=tuple(recovery_errors),
                    target_changed=replaced,
                ) from error
            raise
        target_changed = True
    except _AtomicOperationError as error:
        failure = error
        cause = error.primary_error or error
        target_changed = error.target_changed
    except (ApplyError, OSError) as error:
        failure = _AtomicOperationError(error, target_changed=False)
        cause = error
    release_error: OSError | None = None
    try:
        os.close(directory_fd)
    except OSError as error:
        release_error = error
    if release_error is not None:
        if failure is None:
            failure = _AtomicOperationError(
                None,
                release_errors=(release_error,),
                target_changed=target_changed,
            )
            cause = release_error
        else:
            failure = failure.with_release_error(release_error)
    if failure is not None:
        raise failure from cause


def _atomic_replace_windows(bound: _BoundParent, content: bytes) -> None:
    descriptor, temporary = _temporary_sibling_windows(bound)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _verify_bound_parent(bound)
        _verify_target_containment(temporary, bound.target_root)
        os.replace(temporary, bound.target)
        replaced = True
        _verify_bound_parent(bound)
    except (ApplyError, OSError):
        try:
            (bound.target if replaced else temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_replace(target: Path, content: bytes, target_root: Path) -> None:
    bound = _open_bound_parent(target, target_root)
    failure: _AtomicOperationError | None = None
    cause: Exception | None = None
    target_changed = False
    try:
        try:
            if bound.directory_fd is not None:
                _atomic_replace_posix(bound, content)
            else:
                _atomic_replace_windows(bound, content)
            target_changed = True
        except _AtomicOperationError as error:
            failure = error
            cause = error.primary_error or error
            target_changed = error.target_changed
        except (ApplyError, OSError) as error:
            failure = _AtomicOperationError(error, target_changed=False)
            cause = error
    finally:
        release_error: Exception | None = None
        try:
            bound.close()
        except (ApplyError, OSError) as error:
            release_error = error
        if release_error is not None:
            if failure is None:
                failure = _AtomicOperationError(
                    None,
                    release_errors=(release_error,),
                    target_changed=target_changed,
                )
                cause = release_error
            else:
                failure = failure.with_release_error(release_error)
        if failure is not None:
            raise failure from cause


def _atomic_remove_posix(bound: _BoundParent) -> None:
    assert bound.directory_fd is not None
    _verify_bound_parent(bound)
    directory_fd = _posix_operation_parent_fd(bound)
    failure: _AtomicOperationError | None = None
    cause: Exception | None = None
    target_changed = False
    try:
        before = _read_posix_bound_file(bound, directory_fd)
        bound.posix_restore_mode = _posix_bound_file_mode(bound, directory_fd)
        if before is not None:
            temporary = _temporary_name(bound.name)
            moved = False
            try:
                _verify_bound_parent(bound)
                os.replace(
                    bound.name,
                    temporary,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                moved = True
                _verify_bound_parent(bound)
                os.unlink(temporary, dir_fd=directory_fd)
                _verify_bound_parent(bound)
            except (ApplyError, OSError) as error:
                recovery_errors: list[Exception] = []
                target_restored = not moved
                if moved:
                    try:
                        os.replace(
                            temporary,
                            bound.name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        try:
                            _restore_posix_bound_file(bound, before, directory_fd)
                        except (ApplyError, OSError) as recovery_error:
                            recovery_errors.append(recovery_error)
                        else:
                            target_restored = True
                    except (ApplyError, OSError) as recovery_error:
                        recovery_errors.append(recovery_error)
                        try:
                            _restore_posix_bound_file(bound, before, directory_fd)
                        except (ApplyError, OSError) as fallback_error:
                            recovery_errors.append(fallback_error)
                        else:
                            target_restored = True
                            try:
                                os.unlink(temporary, dir_fd=directory_fd)
                            except FileNotFoundError:
                                pass
                            except OSError as cleanup_error:
                                recovery_errors.append(cleanup_error)
                    else:
                        target_restored = True
                else:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup_error:
                        recovery_errors.append(cleanup_error)
                if recovery_errors:
                    raise _AtomicOperationError(
                        error,
                        recovery_errors=tuple(recovery_errors),
                        target_changed=not target_restored,
                    ) from error
                raise
            target_changed = True
    except _AtomicOperationError as error:
        failure = error
        cause = error.primary_error or error
        target_changed = error.target_changed
    except (ApplyError, OSError) as error:
        failure = _AtomicOperationError(error, target_changed=False)
        cause = error
    release_error: OSError | None = None
    try:
        os.close(directory_fd)
    except OSError as error:
        release_error = error
    if release_error is not None:
        if failure is None:
            failure = _AtomicOperationError(
                None,
                release_errors=(release_error,),
                target_changed=target_changed,
            )
            cause = release_error
        else:
            failure = failure.with_release_error(release_error)
    if failure is not None:
        raise failure from cause


def _atomic_remove_windows(bound: _BoundParent) -> None:
    if not bound.target.exists():
        return
    temporary = bound.parent / _temporary_name(bound.name)
    try:
        _verify_bound_parent(bound)
        os.replace(bound.target, temporary)
        temporary.unlink()
        _verify_bound_parent(bound)
    except (ApplyError, OSError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_remove(target: Path, target_root: Path) -> None:
    bound = _open_bound_parent(target, target_root)
    failure: _AtomicOperationError | None = None
    cause: Exception | None = None
    target_changed = False
    try:
        try:
            if bound.directory_fd is not None:
                _atomic_remove_posix(bound)
            else:
                _atomic_remove_windows(bound)
            target_changed = True
        except _AtomicOperationError as error:
            failure = error
            cause = error.primary_error or error
            target_changed = error.target_changed
        except (ApplyError, OSError) as error:
            failure = _AtomicOperationError(error, target_changed=False)
            cause = error
    finally:
        release_error: Exception | None = None
        try:
            bound.close()
        except (ApplyError, OSError) as error:
            release_error = error
        if release_error is not None:
            if failure is None:
                failure = _AtomicOperationError(
                    None,
                    release_errors=(release_error,),
                    target_changed=target_changed,
                )
                cause = release_error
            else:
                failure = failure.with_release_error(release_error)
        if failure is not None:
            raise failure from cause


def _restore(change: FileChange, target_root: Path) -> None:
    if change.before is None:
        _atomic_remove(change.target, target_root)
    else:
        _atomic_replace(change.target, change.before, target_root)


def _rollback(
    applied: list[FileChange], target_root: Path
) -> list[tuple[FileChange, Exception]]:
    failures: list[tuple[FileChange, Exception]] = []
    for change in reversed(applied):
        try:
            _verify_target_containment(change.target, target_root)
            if _current_bytes(change.target) != change.after:
                failures.append(
                    (
                        change,
                        ApplyError(
                            "current bytes no longer match this run; concurrent change preserved"
                        ),
                    )
                )
                continue
            _restore(change, target_root)
        except (ApplyError, OSError) as error:
            failures.append((change, error))
    return failures


def _format_apply_failure(
    error: Exception, rollback_errors: list[tuple[FileChange, Exception]]
) -> str:
    message = f"apply failed: {error}"
    if rollback_errors:
        details = "; ".join(
            f"{change.relative_target}: {rollback_error}"
            for change, rollback_error in rollback_errors
        )
        message += f"; rollback failed: {details}"
    return message


def apply_plan(plan: InstallPlan) -> None:
    """Apply a conflict-free plan with stale-plan protection and rollback."""

    if plan.conflicts:
        raise ApplyError("cannot apply a plan with conflicts")
    _verify_preconditions(plan)
    applied: list[FileChange] = []
    try:
        for change in plan.changes:
            _verify_change_precondition(change, plan.target_root)
            _atomic_replace(change.target, change.after, plan.target_root)
            applied.append(change)
    except (ApplyError, OSError) as error:
        if isinstance(error, _AtomicOperationError) and error.target_changed:
            applied.append(change)
        rollback_errors = _rollback(applied, plan.target_root)
        raise ApplyError(_format_apply_failure(error, rollback_errors)) from error
