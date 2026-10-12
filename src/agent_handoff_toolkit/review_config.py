"""Project-owned review settings and bounded, non-evaluating input readers."""

import copy
import json
import math
import os
from pathlib import Path
import re
from collections.abc import Mapping


class ReviewInputError(ValueError):
    """Invalid local input; message is a fixed code."""


class ReviewUnavailable(ValueError):
    """No substantive assessment; message is a fixed code."""


MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
VARIABLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DEFAULT_CONFIG = {
    "enabled": False,
    "credential": {
        "variable": "JEV_API_KEY",
        "source": "environment-or-file",
        "file": ".env",
    },
    "model": "jev-1.13.0",
    "timeout_seconds": 10,
    "minimum_confidence": 0.8,
}


def read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError:
        raise ReviewInputError("input-read") from None
    if len(raw) > limit:
        raise ReviewInputError("input-size")
    return raw


def strict_object(raw: bytes) -> dict[str, object]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(value):
        raise ValueError

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)

    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
        finite(data)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise ReviewInputError("input-json") from None


def resolve_project_root(record: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
        if not root.is_dir():
            raise ReviewInputError("project-root")
        return root
    start = record.resolve().parent
    ceilings = {
        Path(value).resolve()
        for value in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
        if value
    }
    for candidate in (start, *start.parents):
        if candidate != start and candidate in ceilings:
            break
        if (candidate / ".git").exists():
            return candidate
    raise ReviewInputError("project-root")


def load_config(root: Path) -> dict[str, object]:
    result = copy.deepcopy(DEFAULT_CONFIG)
    path = root / ".agent-handoff-toolkit" / "jev.json"
    if not path.exists():
        return result
    data = strict_object(read_bounded(path, 16 * 1024))
    if set(data) - set(result):
        raise ReviewInputError("config-schema")
    credential = data.get("credential", {})
    if not isinstance(credential, dict) or set(credential) - set(result["credential"]):
        raise ReviewInputError("config-schema")
    result.update({key: value for key, value in data.items() if key != "credential"})
    result["credential"].update(credential)
    cred = result["credential"]
    if (
        type(result["enabled"]) is not bool
        or not isinstance(result["model"], str)
        or not MODEL_PATTERN.fullmatch(result["model"])
        or not isinstance(cred["variable"], str)
        or not VARIABLE_PATTERN.fullmatch(cred["variable"])
        or cred["source"] not in ("environment", "file", "environment-or-file")
        or not isinstance(cred["file"], str)
        or not cred["file"].strip()
        or "\x00" in cred["file"]
    ):
        raise ReviewInputError("config-schema")
    for key, lower, upper in (("timeout_seconds", 0, 30), ("minimum_confidence", 0, 1)):
        value = result[key]
        if (
            type(value) not in (int, float)
            or not lower <= value <= upper
            or (key == "timeout_seconds" and value == 0)
        ):
            raise ReviewInputError("config-schema")
    return result


def resolve_credential(root: Path, config: dict, environ: Mapping[str, str]) -> str:
    settings = config["credential"]
    variable = settings["variable"]
    if settings["source"] != "file":
        value = environ.get(variable)
        if value:
            return value
        if settings["source"] == "environment":
            raise ReviewUnavailable("credential-missing")
    try:
        content = read_bounded(root / settings["file"], 64 * 1024).decode("utf-8-sig")
    except (ReviewInputError, UnicodeError, OSError, ValueError):
        raise ReviewUnavailable("credential-read") from None
    selected = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)\Z", line)
        if match and match[1] == variable:
            value = match[2].strip()
            if value.startswith(('"', "'")):
                end = value.find(value[0], 1)
                if end < 0 or (
                    value[end + 1 :].strip()
                    and not value[end + 1 :].strip().startswith("#")
                ):
                    raise ReviewUnavailable("credential-format")
                value = value[1:end]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                if '"' in value or "'" in value:
                    raise ReviewUnavailable("credential-format")
            selected.append(value)
        elif re.match(re.escape(variable) + r"(?:\s|$)", line):
            raise ReviewUnavailable("credential-format")
    if len(selected) > 1:
        raise ReviewUnavailable("credential-format")
    if not selected or not selected[0]:
        raise ReviewUnavailable("credential-missing")
    return selected[0]
