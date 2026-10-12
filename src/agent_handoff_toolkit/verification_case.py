"""Explicit feature/component ownership and local evidence bindings; no uploads."""

import hashlib
from pathlib import Path, PurePosixPath
import re

from .review_config import ReviewInputError

ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def invalid():
    raise ReviewInputError("evidence-schema")


def text(value, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        invalid()
    try:
        value.encode("utf-8")
    except UnicodeError:
        invalid()


def path_name(value):
    text(value, 240)
    if (
        "\\" in value
        or ":" in value
        or "\x00" in value
        or PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        invalid()


def references(value, known, *, nonempty=False):
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(item, str) or not ID.fullmatch(item) for item in value)
        or len(set(value)) != len(value)
        or set(value) - known
        or (nonempty and not value)
    ):
        invalid()


def validate_case(
    bundle: dict, project_root: Path | None, *, root_outcome=None
) -> list[dict]:
    if type(bundle.get("case_version")) is not int or bundle["case_version"] not in (
        2,
        3,
    ):
        invalid()
    evidence_ids = {item["id"] for item in bundle["evidence"]}
    criterion_ids = {item["id"] for item in bundle["criteria"]}
    features = bundle["features"]
    files = bundle["files"]
    if not isinstance(features, list) or not 1 <= len(features) <= 16:
        invalid()
    if not isinstance(files, list) or len(files) > 32:
        invalid()
    owned = set()
    feature_ids = set()
    component_ids = set()
    findings = []
    for feature in features:
        if not isinstance(feature, dict) or set(feature) != {
            "id",
            "requirement",
            "criterion_ids",
            "components",
            "discovery_evidence_ids",
            "required_paths",
        }:
            invalid()
        ident = feature["id"]
        if (
            not isinstance(ident, str)
            or not ID.fullmatch(ident)
            or ident in feature_ids
        ):
            invalid()
        feature_ids.add(ident)
        text(feature["requirement"])
        references(feature["criterion_ids"], criterion_ids, nonempty=True)
        if owned.intersection(feature["criterion_ids"]):
            invalid()
        owned.update(feature["criterion_ids"])
        references(feature["discovery_evidence_ids"], evidence_ids)
        if not feature["discovery_evidence_ids"]:
            findings.append(
                {
                    "code": "missing-discovery",
                    "feature_id": feature["id"],
                    "message": "Supply observations supporting the required-location inventory.",
                }
            )
        required_paths = feature["required_paths"]
        if not isinstance(required_paths, list) or len(required_paths) > 32:
            invalid()
        for required in required_paths:
            path_name(required)
        if len(set(required_paths)) != len(required_paths):
            invalid()
        components = feature["components"]
        if not isinstance(components, list) or len(components) > 32:
            invalid()
        for component in components:
            if not isinstance(component, dict) or set(component) != {
                "id",
                "path",
                "condition",
                "evidence_ids",
                "claim",
            }:
                invalid()
            ident = component["id"]
            if (
                not isinstance(ident, str)
                or not ID.fullmatch(ident)
                or ident in component_ids
            ):
                invalid()
            component_ids.add(ident)
            if len(component_ids) > 32:
                invalid()
            path_name(component["path"])
            text(component["condition"])
            references(component["evidence_ids"], evidence_ids)
            if component["claim"] not in ("implemented", "pending"):
                invalid()
        accounted = {c["path"] for c in components}
        for missing in sorted(set(required_paths) - accounted):
            findings.append(
                {
                    "code": "unaccounted-location",
                    "feature_id": feature["id"],
                    "path": missing,
                    "message": "Add this discovered required location to the verification case.",
                }
            )
    if owned != criterion_ids:
        invalid()
    if bundle["case_version"] == 3:
        validate_requirements(bundle, root_outcome, criterion_ids)
    seen_paths = set()
    component_paths = {c["path"] for f in features for c in f["components"]}
    for binding in files:
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
            invalid()
        path_name(binding["path"])
        if binding["path"] in seen_paths or binding["path"] not in component_paths:
            invalid()
        seen_paths.add(binding["path"])
        if not isinstance(binding["sha256"], str) or not DIGEST.fullmatch(
            binding["sha256"]
        ):
            invalid()
        if project_root is None:
            raise ReviewInputError("project-root")
        root = project_root.resolve()
        target = (root / binding["path"]).resolve()
        if not target.is_relative_to(root):
            invalid()
        try:
            # Bounded read is local only: neither contents nor hashes enter the model state.
            with target.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            current = (
                hashlib.sha256(raw).hexdigest() if len(raw) <= 1024 * 1024 else None
            )
        except OSError:
            current = None
        if current != binding["sha256"]:
            findings.append(
                {
                    "code": "stale-evidence",
                    "message": "Refresh evidence for the current file contents.",
                    "path": binding["path"],
                }
            )
    return findings


def validate_requirements(bundle, root_outcome, criterion_ids):
    text(root_outcome, 8000)
    sources = {"root": root_outcome} | {
        f"feature:{f['id']}": f["requirement"] for f in bundle["features"]
    }
    units = bundle["requirements"]
    if not isinstance(units, list) or not 1 <= len(units) <= 32:
        invalid()
    ids = set()
    intervals = {source: [] for source in sources}
    for unit in units:
        if not isinstance(unit, dict) or set(unit) - {"feature_ids"} != {
            "id",
            "source",
            "start",
            "end",
            "quote",
            "criterion_ids",
        }:
            invalid()
        ident = unit["id"]
        source = unit["source"]
        start = unit["start"]
        end = unit["end"]
        if (
            not isinstance(ident, str)
            or not ID.fullmatch(ident)
            or ident in ids
            or not isinstance(source, str)
            or source not in sources
        ):
            invalid()
        ids.add(ident)
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(sources[source])
        ):
            invalid()
        text(unit["quote"], 8000)
        if unit["quote"] != sources[source][start:end]:
            invalid()
        references(unit["criterion_ids"], criterion_ids)
        references(unit.get("feature_ids", []), {f["id"] for f in bundle["features"]})
        intervals[source].append((start, end))
    for source, spans in intervals.items():
        position = 0
        for start, end in sorted(spans):
            if start != position:
                invalid()
            position = end
        if position != len(sources[source]):
            invalid()


def implementation_state(answer: dict, floor: float) -> str:
    if answer["confidence"] < floor or answer["choice"] == "insufficient_evidence":
        return "cannot-determine"
    return "implemented" if answer["choice"] == "supported" else "work-remains"


def exact_accounted_units(bundle):
    """Text correspondence only; callers first validate every source partition."""
    units = bundle.get("requirements", [])
    criteria = {c["id"]: c["condition"] for c in bundle["criteria"]}
    return {
        unit["id"]
        for unit in units
        if unit["start"] == 0
        and sum(other["source"] == unit["source"] for other in units) == 1
        and any(criteria[ref] == unit["quote"] for ref in unit["criterion_ids"])
    }


def combine(states: list[str]) -> str:
    if "work-remains" in states:
        return "work-remains"
    if not states or "cannot-determine" in states:
        return "cannot-determine"
    return "implemented"


def summarize_case(bundle: dict, answers: dict, floor: float) -> dict:
    criteria = [
        {
            "id": criterion["id"],
            "state": implementation_state(answers[f"criterion-{i}"], floor),
            "evidence_ids": criterion["evidence_ids"],
        }
        for i, criterion in enumerate(bundle["criteria"], 1)
    ]
    criteria_by_id = {item["id"]: item["state"] for item in criteria}
    features = []
    for index, feature in enumerate(bundle.get("features", []), 1):
        components = [
            {
                "id": c["id"],
                "path": c["path"],
                "evidence_ids": c["evidence_ids"],
                "state": implementation_state(answers[f"component-{c['id']}"], floor),
            }
            for c in feature["components"]
        ]
        location_coverage = (
            "implemented" if feature["discovery_evidence_ids"] else "cannot-determine"
        )
        features.append(
            {
                "id": feature["id"],
                "components": components,
                "state": combine(
                    [criteria_by_id[ident] for ident in feature["criterion_ids"]]
                    + [c["state"] for c in components]
                    + [location_coverage]
                    + [
                        implementation_state(
                            answers[f"feature-implementation-{feature['id']}"], floor
                        )
                    ]
                ),
                "location_coverage": location_coverage,
            }
        )
    coverage_states = [
        implementation_state(answer, floor)
        for ident, answer in answers.items()
        if ident == "criteria-coverage" or ident.startswith("coverage-unit-")
    ]
    accounted = exact_accounted_units(bundle)
    coverage_states += ["implemented"] * len(accounted)
    coverage = {
        "implemented": "covered",
        "work-remains": "inadequate",
        "cannot-determine": "cannot-determine",
    }[combine(coverage_states)]
    if coverage == "covered" and accounted:
        coverage = "accounted"
    behavior_state = combine(
        [c["state"] for c in criteria] + [f["state"] for f in features]
    )
    return {
        "state": "cannot-determine"
        if behavior_state != "work-remains" and coverage not in ("covered", "accounted")
        else behavior_state,
        "behavior_state": behavior_state,
        "criteria": criteria,
        "features": features,
        "coverage": coverage,
        "coverage_provenance": [
            {
                "requirement_id": unit["id"],
                "method": "exact-text" if unit["id"] in accounted else "model",
            }
            for unit in bundle.get("requirements", [])
        ],
    }
