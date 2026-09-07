"""Pure transformations used by managed installation planning."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import re

from .manifest import text_sha256


class OperationConflict(ValueError):
    """Raised when current consumer content cannot be changed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def merge_copy(
    current: bytes | None,
    desired: bytes,
    installed_hash: str | None = None,
) -> bytes:
    """Return safe copy content or raise when ownership cannot be established."""

    desired_hash = text_sha256(desired)
    if current is None:
        if installed_hash is not None:
            raise OperationConflict(
                "managed-copy-missing", "installed copy target is missing"
            )
        return desired
    try:
        current_hash = text_sha256(current)
    except UnicodeDecodeError as error:
        code = (
            "managed-copy-modified"
            if installed_hash is not None
            else "unowned-copy-conflict"
        )
        raise OperationConflict(code, "copy target is not valid UTF-8 text") from error
    if current_hash == desired_hash:
        return current
    if installed_hash is None:
        raise OperationConflict(
            "unowned-copy-conflict", "unowned copy target has different content"
        )
    if current_hash != installed_hash:
        raise OperationConflict(
            "managed-copy-modified", "installed copy target was locally modified"
        )
    return desired


def _first_newline(data: bytes) -> bytes:
    match = re.search(rb"\r\n|\r|\n", data)
    return match.group(0) if match is not None else b"\n"


def _marked_block(desired_body: bytes, block_id: str, newline: bytes) -> bytes:
    if not block_id or any(character in block_id for character in "\r\n"):
        raise ValueError("block id must be a non-empty single-line string")
    body = desired_body.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    normalized = body.encode("utf-8").replace(b"\n", newline)
    if normalized and not normalized.endswith(newline):
        normalized += newline
    start = f"<!-- {block_id}:start -->".encode()
    end = f"<!-- {block_id}:end -->".encode()
    return start + newline + normalized + end + newline


def _managed_block_span(current: bytes, block_id: str) -> tuple[int, int] | None:
    start_marker = f"<!-- {block_id}:start -->".encode()
    end_marker = f"<!-- {block_id}:end -->".encode()
    start_count = current.count(start_marker)
    end_count = current.count(end_marker)
    if start_count == end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise OperationConflict(
            "managed-block-malformed", "managed block markers are malformed"
        )
    start = current.find(start_marker)
    end_marker_start = current.find(end_marker)
    after_start = start + len(start_marker)
    after_end = end_marker_start + len(end_marker)
    start_is_line = start == 0 or current[start - 1 : start] in {b"\r", b"\n"}
    end_is_line = end_marker_start == 0 or current[
        end_marker_start - 1 : end_marker_start
    ] in {b"\r", b"\n"}
    start_has_newline = current[after_start:].startswith((b"\r\n", b"\r", b"\n"))
    end_has_boundary = after_end == len(current) or current[after_end:].startswith(
        (b"\r\n", b"\r", b"\n")
    )
    if (
        start >= end_marker_start
        or not start_is_line
        or not end_is_line
        or not start_has_newline
        or not end_has_boundary
    ):
        raise OperationConflict(
            "managed-block-malformed", "managed block markers are malformed"
        )
    if current[after_end:].startswith(b"\r\n"):
        after_end += 2
    elif current[after_end:].startswith((b"\r", b"\n")):
        after_end += 1
    return start, after_end


def merge_managed_block(
    current: bytes | None,
    desired_body: bytes,
    block_id: str,
    installed_hash: str | None = None,
) -> bytes:
    """Append, adopt, or advance one exactly owned marked instruction block."""

    content = current if current is not None else b""
    newline = _first_newline(content)
    desired_block = _marked_block(desired_body, block_id, newline)
    span = _managed_block_span(content, block_id)
    if span is None:
        if installed_hash is not None:
            raise OperationConflict(
                "managed-block-missing", "installed managed block is missing"
            )
        separator = newline if content and not content.endswith((b"\r", b"\n")) else b""
        return content + separator + desired_block

    start, end = span
    existing_block = content[start:end]
    try:
        existing_hash = text_sha256(existing_block)
        desired_hash = text_sha256(desired_block)
    except UnicodeDecodeError as error:
        code = (
            "managed-block-modified"
            if installed_hash is not None
            else "unowned-block-conflict"
        )
        raise OperationConflict(
            code, "managed block is not valid UTF-8 text"
        ) from error
    if existing_hash == desired_hash:
        return current if current is not None else content
    if installed_hash is None:
        raise OperationConflict(
            "unowned-block-conflict",
            "unowned managed block has different content",
        )
    if existing_hash != installed_hash:
        raise OperationConflict(
            "managed-block-modified", "installed managed block was locally modified"
        )
    return content[:start] + desired_block + content[end:]


_MISSING = object()
_DELETE = object()


def _pointer_child(pointer: str, key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _identity(value: object, fields: tuple[str, ...]) -> object:
    if not isinstance(value, dict) or any(field not in value for field in fields):
        return _MISSING
    return tuple(value[field] for field in fields)


def _identity_matches(
    values: list[object], identity: object, fields: tuple[str, ...]
) -> list[int]:
    return [
        index
        for index, value in enumerate(values)
        if _identity(value, fields) == identity
    ]


def _require_managed_identity(value: object, fields: tuple[str, ...]) -> object:
    identity = _identity(value, fields)
    if identity is _MISSING:
        raise OperationConflict(
            "invalid-json-identity", "managed JSON entry is missing an identity field"
        )
    return identity


def _merge_install_array(
    current: list[object],
    desired: list[object],
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> list[object]:
    result = copy.deepcopy(current)
    fields = identities.get(pointer)
    for desired_item in desired:
        if fields is None:
            if desired_item not in result:
                result.append(copy.deepcopy(desired_item))
            continue
        desired_identity = _require_managed_identity(desired_item, fields)
        matches = _identity_matches(result, desired_identity, fields)
        exact_matches = [index for index in matches if result[index] == desired_item]
        if exact_matches and len(matches) == len(exact_matches) == 1:
            continue
        if matches:
            raise OperationConflict(
                "json-identity-collision",
                "existing JSON entry has the same identity but different content",
            )
        result.append(copy.deepcopy(desired_item))
    return result


def _merge_install_value(
    current: object,
    desired: object,
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> object:
    if isinstance(current, dict) and isinstance(desired, dict):
        return _merge_install_object(current, desired, pointer, identities)
    if isinstance(current, list) and isinstance(desired, list):
        return _merge_install_array(current, desired, pointer, identities)
    if current == desired:
        return copy.deepcopy(current)
    raise OperationConflict(
        "unowned-json-conflict",
        "existing JSON value conflicts with the managed fragment",
    )


def _merge_install_object(
    current: dict[str, object],
    desired: dict[str, object],
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    result = copy.deepcopy(current)
    for key, desired_value in desired.items():
        child = _pointer_child(pointer, key)
        if key not in result:
            result[key] = copy.deepcopy(desired_value)
        else:
            result[key] = _merge_install_value(
                result[key], desired_value, child, identities
            )
    return result


def _unique_managed_identities(
    values: list[object], fields: tuple[str, ...]
) -> list[tuple[object, object]]:
    found: list[tuple[object, object]] = []
    for value in values:
        identity = _require_managed_identity(value, fields)
        if any(existing == identity for existing, _ in found):
            raise OperationConflict(
                "invalid-json-identity", "managed JSON fragment repeats an identity"
            )
        found.append((identity, value))
    return found


def _merge_owned_identity_array(
    current: list[object],
    desired: list[object],
    owned: list[object],
    fields: tuple[str, ...],
) -> list[object]:
    result = copy.deepcopy(current)
    old_entries = _unique_managed_identities(owned, fields)
    desired_entries = _unique_managed_identities(desired, fields)

    for old_identity, old_item in old_entries:
        new_item = next(
            (
                item
                for desired_identity, item in desired_entries
                if desired_identity == old_identity
            ),
            _MISSING,
        )
        matches = _identity_matches(result, old_identity, fields)
        if len(matches) > 1:
            raise OperationConflict(
                "managed-json-modified",
                "owned JSON entry was locally modified or duplicated",
            )
        if new_item is _MISSING:
            if not matches:
                raise OperationConflict(
                    "managed-json-missing", "owned JSON entry is missing"
                )
            index = matches[0]
            if result[index] != old_item:
                raise OperationConflict(
                    "managed-json-modified",
                    "owned JSON entry was locally modified",
                )
            result.pop(index)
            continue
        if not matches:
            raise OperationConflict(
                "managed-json-missing", "owned JSON entry is missing"
            )
        index = matches[0]
        if result[index] == new_item:
            continue
        if result[index] != old_item:
            raise OperationConflict(
                "managed-json-modified", "owned JSON entry was locally modified"
            )
        result[index] = copy.deepcopy(new_item)

    for desired_identity, desired_item in desired_entries:
        if any(old_identity == desired_identity for old_identity, _ in old_entries):
            continue
        matches = _identity_matches(result, desired_identity, fields)
        if len(matches) == 1 and result[matches[0]] == desired_item:
            continue
        if matches:
            raise OperationConflict(
                "json-identity-collision",
                "existing JSON entry has the same identity but different content",
            )
        result.append(copy.deepcopy(desired_item))
    return result


def _find_equal(values: list[object], wanted: object) -> int | None:
    return next((index for index, value in enumerate(values) if value == wanted), None)


def _merge_owned_positional_array(
    current: list[object], desired: list[object], owned: list[object]
) -> list[object]:
    result = copy.deepcopy(current)
    common_length = min(len(owned), len(desired))
    for index in range(common_length):
        old_item = owned[index]
        desired_item = desired[index]
        if old_item == desired_item:
            if _find_equal(result, old_item) is None:
                raise OperationConflict(
                    "managed-json-missing", "owned JSON entry is missing"
                )
            continue
        old_index = _find_equal(result, old_item)
        desired_index = _find_equal(result, desired_item)
        if old_index is not None:
            result[old_index] = copy.deepcopy(desired_item)
        elif desired_index is None:
            raise OperationConflict(
                "managed-json-missing",
                "owned JSON entry is missing or locally modified",
            )
    for old_item in owned[common_length:]:
        old_index = _find_equal(result, old_item)
        if old_index is None:
            raise OperationConflict(
                "managed-json-missing",
                "owned JSON entry is missing or locally modified",
            )
        result.pop(old_index)
    for desired_item in desired[common_length:]:
        if _find_equal(result, desired_item) is None:
            result.append(copy.deepcopy(desired_item))
    return result


def _merge_owned_array(
    current: list[object],
    desired: list[object],
    owned: list[object],
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> list[object]:
    fields = identities.get(pointer)
    if fields is not None:
        return _merge_owned_identity_array(current, desired, owned, fields)
    return _merge_owned_positional_array(current, desired, owned)


def _remove_owned_value(
    current: object,
    owned: object,
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> object:
    if isinstance(current, dict) and isinstance(owned, dict):
        result = copy.deepcopy(current)
        for key, owned_value in owned.items():
            if key not in result:
                raise OperationConflict(
                    "managed-json-missing", "owned JSON value is missing"
                )
            child = _pointer_child(pointer, key)
            replacement = _remove_owned_value(
                result[key], owned_value, child, identities
            )
            if replacement is _DELETE:
                del result[key]
            else:
                result[key] = replacement
        return result if result else _DELETE
    if isinstance(current, list) and isinstance(owned, list):
        result = _merge_owned_array(current, [], owned, pointer, identities)
        return result if result else _DELETE
    if current != owned:
        raise OperationConflict(
            "managed-json-modified", "owned JSON value was locally modified"
        )
    return _DELETE


def _merge_owned_value(
    current: object,
    desired: object,
    owned: object,
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> object:
    if (
        isinstance(current, dict)
        and isinstance(desired, dict)
        and isinstance(owned, dict)
    ):
        return _merge_owned_object(current, desired, owned, pointer, identities)
    if (
        isinstance(current, list)
        and isinstance(desired, list)
        and isinstance(owned, list)
    ):
        return _merge_owned_array(current, desired, owned, pointer, identities)
    if current == desired:
        return copy.deepcopy(current)
    if current == owned:
        return copy.deepcopy(desired)
    raise OperationConflict(
        "managed-json-modified", "owned JSON value was locally modified"
    )


def _merge_owned_object(
    current: dict[str, object],
    desired: dict[str, object],
    owned: dict[str, object],
    pointer: str,
    identities: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    result = copy.deepcopy(current)
    for key, owned_value in owned.items():
        child = _pointer_child(pointer, key)
        if key in desired:
            if key not in result:
                raise OperationConflict(
                    "managed-json-missing", "owned JSON value is missing"
                )
            result[key] = _merge_owned_value(
                result[key], desired[key], owned_value, child, identities
            )
        elif key not in result:
            raise OperationConflict(
                "managed-json-missing", "owned JSON value is missing"
            )
        else:
            replacement = _remove_owned_value(
                result[key], owned_value, child, identities
            )
            if replacement is _DELETE:
                del result[key]
            else:
                result[key] = replacement
    for key, desired_value in desired.items():
        if key in owned:
            continue
        child = _pointer_child(pointer, key)
        if key not in result:
            result[key] = copy.deepcopy(desired_value)
        else:
            result[key] = _merge_install_value(
                result[key], desired_value, child, identities
            )
    return result


def merge_json_fragment(
    current: object,
    desired: object,
    identities: Mapping[str, tuple[str, ...]],
    owned: object | None = None,
) -> object:
    """Merge one managed JSON object without overwriting unowned content."""

    if not isinstance(current, dict) or not isinstance(desired, dict):
        raise OperationConflict(
            "invalid-json", "current and desired JSON roots must be JSON objects"
        )
    if owned is None:
        return _merge_install_object(current, desired, "", identities)
    if not isinstance(owned, dict):
        raise OperationConflict(
            "invalid-owned-json", "owned JSON fragment must be a JSON object"
        )
    return _merge_owned_object(current, desired, owned, "", identities)
