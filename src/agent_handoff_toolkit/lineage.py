"""Provider-neutral canonical identity and authorization-lineage primitives."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import re
import unicodedata


class LineageError(ValueError):
    """Raised when a lineage value is not structurally valid."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str) -> None:
    raise LineageError(message)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON canonically, preserving UTF-8 characters."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise LineageError("value is not supported canonical JSON") from error
    return encoded.encode("utf-8")


def normalize_text(value: str, *, limit: int, label: str) -> str:
    """Validate bounded, already-normalized human text."""

    if not isinstance(value, str):
        _fail(f"{label} must be text")
    if not value or value != value.strip():
        _fail(f"{label} must be non-empty and trimmed")
    if "\r" in value:
        _fail(f"{label} must use LF rather than CR")
    if any(
        character != "\n"
        and unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
        for character in value
    ):
        _fail(f"{label} contains an unsafe control or line-separator character")
    if len(value.encode("utf-8")) > limit:
        _fail(f"{label} exceeds its UTF-8 byte limit")
    return value


def validate_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail(f"{label} must match the identifier format")
    return value


def validate_hex_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def validate_scope_definition(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"title", "outcome"}:
        _fail("scope_definition must contain exactly title and outcome")
    return {
        "title": normalize_text(
            value["title"], limit=160, label="scope_definition.title"
        ),
        "outcome": normalize_text(
            value["outcome"], limit=1000, label="scope_definition.outcome"
        ),
    }


def scope_definition_digest(scope: Mapping[str, object]) -> str:
    required = {
        "scope_id",
        "scope_kind",
        "parent_scope_id",
        "scope_definition",
    }
    if not isinstance(scope, Mapping) or not required.issubset(scope):
        _fail("scope must contain exactly the canonical definition fields")
    scope_id = validate_identifier(scope["scope_id"], label="scope_id")
    scope_kind = validate_identifier(scope["scope_kind"], label="scope_kind")
    parent = scope["parent_scope_id"]
    if parent is not None:
        parent = validate_identifier(parent, label="parent_scope_id")
    payload = {
        "scope_id": scope_id,
        "scope_kind": scope_kind,
        "parent_scope_id": parent,
        "scope_definition": validate_scope_definition(scope["scope_definition"]),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def record_digest(text: str) -> str:
    if not isinstance(text, str):
        _fail("record must be text")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_hmac(secret: bytes, payload: Mapping[str, object]) -> str:
    if not isinstance(secret, bytes) or not secret:
        _fail("secret must be non-empty bytes")
    if not isinstance(payload, Mapping):
        _fail("payload must be a mapping")
    return hmac.new(secret, canonical_json_bytes(payload), hashlib.sha256).hexdigest()
