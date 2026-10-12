"""Opt-in labeled evaluation; persist only reduced reports and compact metrics."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile

from .records import render_record
from .review import review_record
from .review_config import ReviewInputError, read_bounded, strict_object
from .verification_case import ID

STATES = {"implemented", "work-remains", "cannot-determine"}


def measure(rows: list[dict]) -> dict:
    counts = Counter()
    matrix = Counter()
    elapsed = []
    for row in rows:
        expected = row["expected"]
        if expected not in STATES:
            raise ReviewInputError("evaluation-schema")
        report = row["report"]
        status = report["status"]
        observed = report.get("implementation", {}).get("state", "not-assessed")
        if status in ("disabled", "unavailable", "error"):
            observed = "not-assessed"
            counts["not_assessed"] += 1
            counts[status] += 1
        elif observed == "cannot-determine":
            counts["inconclusive"] += 1
        elif observed in STATES:
            counts["substantive"] += 1
        else:
            raise ReviewInputError("evaluation-schema")
        matrix[(expected, observed)] += 1
        if expected == "work-remains":
            counts["expected_defects"] += 1
            counts[
                "detected_defects"
                if observed == "work-remains"
                else "missed_defects"
                if observed == "implemented"
                else "unresolved_defects"
            ] += 1
        elif expected == "implemented":
            counts["expected_clean"] += 1
            if observed == "implemented":
                counts["supported_clean"] += 1
            if observed == "work-remains":
                counts["false_defects"] += 1
        else:
            counts["expected_insufficient"] += 1
            if observed == "implemented":
                counts["unsupported_completion"] += 1
            elif observed == "work-remains":
                counts["unsupported_defect"] += 1
            elif observed == "cannot-determine":
                counts["correct_insufficient"] += 1
        if type(report.get("elapsed_ms")) is int:
            elapsed.append(report["elapsed_ms"])
    names = (
        "missed_defects",
        "detected_defects",
        "unresolved_defects",
        "false_defects",
        "inconclusive",
        "not_assessed",
        "unavailable",
        "disabled",
        "error",
        "expected_clean",
        "supported_clean",
        "expected_defects",
        "expected_insufficient",
        "unsupported_completion",
        "unsupported_defect",
        "correct_insufficient",
    )
    return {
        "sample_size": len(rows),
        **{name: counts[name] for name in names},
        "substantive_coverage": counts["substantive"] / len(rows) if rows else 0,
        "mean_elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else None,
        "cost": None,
        "cost_note": "Billing cost is not supplied by the validated response contract.",
        "confusion": [
            {"expected": e, "observed": o, "count": n}
            for (e, o), n in sorted(matrix.items())
        ],
    }


def qualification(metrics: dict) -> bool:
    return (
        metrics["expected_defects"] >= 16
        and metrics["detected_defects"] == metrics["expected_defects"]
        and metrics["expected_clean"] >= 16
        and metrics["supported_clean"] >= metrics["expected_clean"] - 1
        and metrics["false_defects"] <= 1
        and metrics["expected_insufficient"] >= 8
        and metrics["unsupported_completion"] == 0
        and metrics["unsupported_defect"] == 0
        and metrics["correct_insufficient"] == metrics["expected_insufficient"]
        and metrics["not_assessed"] == 0
    )


def load_catalog(raw: bytes) -> dict:
    catalog = strict_object(raw)
    if (
        set(catalog) != {"cases"}
        or not isinstance(catalog["cases"], list)
        or not 1 <= len(catalog["cases"]) <= 200
    ):
        raise ReviewInputError("evaluation-schema")
    ids = set()
    partitions = {}
    for case in catalog["cases"]:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "group",
            "partition",
            "expected",
            "record",
            "evidence",
        }:
            raise ReviewInputError("evaluation-schema")
        for key in ("id", "group"):
            if not isinstance(case[key], str) or not ID.fullmatch(case[key]):
                raise ReviewInputError("evaluation-schema")
        if (
            case["id"] in ids
            or not isinstance(case["partition"], str)
            or not isinstance(case["expected"], str)
            or case["partition"] not in ("development", "held-out")
            or case["expected"] not in STATES
        ):
            raise ReviewInputError("evaluation-schema")
        ids.add(case["id"])
        previous = partitions.setdefault(case["group"], case["partition"])
        if previous != case["partition"]:
            raise ReviewInputError("evaluation-schema")
        if not isinstance(case["record"], dict) or not isinstance(
            case["evidence"], dict
        ):
            raise ReviewInputError("evaluation-schema")
    return catalog


def evaluate_catalog(
    path: Path, project_root: Path, partition: str, *, live: bool, reviewer=None
) -> dict:
    if not live:
        raise ReviewInputError("evaluation-opt-in")
    raw = read_bounded(path, 2 * 1024 * 1024)
    catalog = load_catalog(raw)
    rows = []
    selected = [case for case in catalog["cases"] if case["partition"] == partition]
    if not selected:
        raise ReviewInputError("evaluation-schema")
    assess = review_record if reviewer is None else reviewer
    with tempfile.TemporaryDirectory(prefix="ahk-evaluation-") as directory:
        root = Path(directory)
        record_path = root / "candidate.md"
        evidence_path = root / "evidence.json"
        for case in selected:
            # Labels never enter the model input. No raw response is retained.
            try:
                record_path.write_text(render_record(case["record"]), encoding="utf-8")
            except (ValueError, TypeError, UnicodeError):
                raise ReviewInputError("evaluation-schema") from None
            evidence_path.write_text(
                json.dumps(case["evidence"], ensure_ascii=False), encoding="utf-8"
            )
            report = assess(record_path, evidence_path, project_root)
            rows.append(
                {"id": case["id"], "expected": case["expected"], "report": report}
            )
            if report["status"] in ("unavailable", "error", "disabled"):
                # Do not turn an outage or disabled machine into a retry loop.
                break
    metrics = measure(rows)
    return {
        "catalog_digest": hashlib.sha256(raw).hexdigest(),
        "partition": partition,
        "selected_count": len(selected),
        "assessed_count": len(rows),
        "metrics": metrics,
        "qualified": len(rows) == len(selected) and qualification(metrics),
        "results": rows,
        "limitations": "Synthetic labeled sample; qualification does not establish general semantic accuracy.",
    }
