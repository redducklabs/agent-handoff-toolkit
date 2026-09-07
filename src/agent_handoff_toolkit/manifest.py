"""Validation and loading for managed-install source manifests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODES = frozenset({"copy", "managed-block", "merge-json"})
_TEXT_HASH = "utf8-lf-sha256-v1"
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)


class ManifestError(ValueError):
    """Raised when a source manifest is malformed or unsafe."""


@dataclass(frozen=True)
class JsonArrayIdentity:
    pointer: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Artifact:
    source: PurePosixPath
    sha256: str
    mode: str
    targets: tuple[PurePosixPath, ...]
    block_id: str | None = None
    array_identities: tuple[JsonArrayIdentity, ...] = ()


@dataclass(frozen=True)
class Manifest:
    toolkit_version: str
    record_schema_version: int
    python_command: str
    minimum_python: str
    artifacts: tuple[Artifact, ...]

    @property
    def release(self) -> str:
        """Return the release identifier derived from the toolkit version."""

        return f"v{self.toolkit_version}"


def normalize_text(data: bytes) -> bytes:
    """Decode UTF-8 and normalize all supported line endings to LF."""

    text = data.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def text_sha256(data: bytes) -> str:
    """Return the normalized UTF-8 SHA-256 digest for distributed text."""

    return hashlib.sha256(normalize_text(data)).hexdigest()


def strict_json_loads(text: str) -> object:
    """Load JSON while rejecting duplicate keys and non-finite constants."""

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def path_collision_key(path: str | PurePosixPath) -> str:
    """Return a Win32-compatible comparison key for a relative path."""

    parts = PurePosixPath(path).parts
    return "/".join(component.rstrip(" .").casefold() for component in parts)


def _windows_component_is_unsafe(component: str) -> bool:
    normalized = component.rstrip(" .")
    stem = normalized.split(".", 1)[0].rstrip(" ").casefold()
    return (
        normalized != component
        or any(ord(character) < 32 for character in component)
        or any(character in _WINDOWS_RESERVED_CHARACTERS for character in component)
        or stem in _WINDOWS_RESERVED_STEMS
    )


def validate_relative_path(value: object, *, kind: str) -> PurePosixPath:
    """Return a safe normalized POSIX relative path or raise ``ManifestError``."""

    if not isinstance(value, str) or not value:
        raise ManifestError(f"unsafe {kind} path")
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ManifestError(f"unsafe {kind} path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ManifestError(f"unsafe {kind} path")
    if any(_windows_component_is_unsafe(component) for component in components):
        raise ManifestError(f"unsafe {kind} path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.parts != tuple(components):
        raise ManifestError(f"unsafe {kind} path")
    return path


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _require_keys(
    mapping: dict[str, object], expected: set[str], *, label: str
) -> None:
    actual = set(mapping)
    if actual != expected:
        raise ManifestError(f"{label} has unexpected or missing fields")


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def _require_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestError(f"{label} must be an integer")
    return value


def _validate_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ManifestError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _source_path(source_root: Path, source: PurePosixPath) -> Path:
    root = source_root.resolve()
    path = (root / Path(*source.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ManifestError("source escapes source root") from error
    if not path.is_file():
        raise ManifestError("source is not a file")
    return path


def _parse_array_identities(value: object) -> tuple[JsonArrayIdentity, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError("array identities must be an array")
    identities: list[JsonArrayIdentity] = []
    seen_pointers: set[str] = set()
    for item in value:
        identity = _require_mapping(item, label="array identity")
        _require_keys(identity, {"pointer", "fields"}, label="array identity")
        pointer = _require_string(identity["pointer"], label="array identity pointer")
        fields = identity["fields"]
        if not isinstance(fields, list) or not fields:
            raise ManifestError("array identity fields must be a non-empty array")
        parsed_fields = tuple(
            _require_string(field, label="array identity field") for field in fields
        )
        if len(set(parsed_fields)) != len(parsed_fields) or pointer in seen_pointers:
            raise ManifestError("duplicate array identity")
        seen_pointers.add(pointer)
        identities.append(JsonArrayIdentity(pointer, parsed_fields))
    return tuple(identities)


def _parse_artifact(value: object, source_root: Path) -> Artifact:
    artifact = _require_mapping(value, label="artifact")
    allowed = {"source", "sha256", "install", "block_id", "array_identities"}
    required = {"source", "sha256", "install"}
    if not required.issubset(artifact) or not set(artifact).issubset(allowed):
        raise ManifestError("artifact has unexpected or missing fields")
    source = validate_relative_path(artifact["source"], kind="source")
    sha256 = _validate_sha256(artifact["sha256"], label="source sha256")
    install = _require_mapping(artifact["install"], label="install")
    _require_keys(install, {"mode", "transform", "targets"}, label="install")
    mode = _require_string(install["mode"], label="mode")
    if mode not in _MODES:
        raise ManifestError("unknown mode")
    if install["transform"] != "none":
        raise ManifestError("unknown transform")
    targets_value = install["targets"]
    if not isinstance(targets_value, list) or not targets_value:
        raise ManifestError("targets must be a non-empty array")
    targets = tuple(
        validate_relative_path(target, kind="target") for target in targets_value
    )
    if len({path_collision_key(target) for target in targets}) != len(targets):
        raise ManifestError("target collision")
    block_id_value = artifact.get("block_id")
    if block_id_value is not None and not isinstance(block_id_value, str):
        raise ManifestError("block id must be a string")
    if mode == "managed-block" and not block_id_value:
        raise ManifestError("managed-block requires a block id")
    if mode != "managed-block" and block_id_value is not None:
        raise ManifestError("block id is only valid for managed-block")
    identities = _parse_array_identities(artifact.get("array_identities"))
    if mode != "merge-json" and identities:
        raise ManifestError("array identities are only valid for merge-json")
    source_path = _source_path(source_root, source)
    if text_sha256(source_path.read_bytes()) != sha256:
        raise ManifestError("source hash does not match manifest")
    return Artifact(source, sha256, mode, targets, block_id_value, identities)


def load_manifest(source_root: Path) -> Manifest:
    """Parse a version 2 manifest and verify every source artifact hash."""

    root = Path(source_root)
    manifest_path = root / "distribution" / "manifest.json"
    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManifestError("invalid manifest JSON") from error
    try:
        payload = strict_json_loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise ManifestError("invalid manifest JSON") from error
    except ValueError as error:
        raise ManifestError(str(error)) from error
    manifest = _require_mapping(payload, label="manifest")
    _require_keys(
        manifest,
        {
            "manifest_version",
            "toolkit_version",
            "record_schema_version",
            "runtime",
            "text_hash",
            "artifacts",
        },
        label="manifest",
    )
    if _require_int(manifest["manifest_version"], label="manifest version") != 2:
        raise ManifestError("manifest version must be 2")
    if manifest["text_hash"] != _TEXT_HASH:
        raise ManifestError("unknown text hash strategy")
    toolkit_version = _require_string(
        manifest["toolkit_version"], label="toolkit version"
    )
    record_schema_version = _require_int(
        manifest["record_schema_version"], label="record schema version"
    )
    runtime = _require_mapping(manifest["runtime"], label="runtime")
    _require_keys(runtime, {"python_command", "minimum_version"}, label="runtime")
    python_command = _require_string(runtime["python_command"], label="python command")
    minimum_python = _require_string(
        runtime["minimum_version"], label="minimum version"
    )
    artifacts_value = manifest["artifacts"]
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise ManifestError("artifacts must be a non-empty array")
    artifacts = tuple(_parse_artifact(item, root) for item in artifacts_value)
    sources = [path_collision_key(artifact.source) for artifact in artifacts]
    if len(set(sources)) != len(sources):
        raise ManifestError("source collision")
    targets = [
        path_collision_key(target)
        for artifact in artifacts
        for target in artifact.targets
    ]
    if len(set(targets)) != len(targets):
        raise ManifestError("target collision")
    return Manifest(
        toolkit_version,
        record_schema_version,
        python_command,
        minimum_python,
        artifacts,
    )
