"""Ordered optional review; reports never enter deterministic enforcement."""

from collections.abc import Mapping, Callable
import hashlib
import json
import os
import time
from pathlib import Path

from .lineage import record_digest
from .records import parse_markdown, validate_markdown
from .review_config import (
    ReviewInputError,
    ReviewUnavailable,
    load_config,
    read_bounded,
    resolve_credential,
    resolve_project_root,
)
from .review_rubrics import (
    RUBRIC_VERSION,
    prepare_evidence,
    project_state,
    build_questions,
    summarize_answers,
)
from .verification_case import summarize_case

MESSAGES = {
    "project-root": "Select an existing checkout with --project-root.",
    "input-read": "A required local input could not be read.",
    "input-size": "A local input exceeds its byte limit.",
    "input-json": "A local input is not strict UTF-8 JSON.",
    "config-schema": "Correct the project review configuration schema.",
    "candidate-invalid": "Supply a structurally valid schema-v2 candidate.",
    "evidence-size": "Evidence exceeds 64 KiB.",
    "evidence-schema": "Correct the evidence schema and references.",
    "credential-missing": "The selected review credential is missing.",
    "credential-read": "The selected credential source could not be read.",
    "credential-format": "The selected credential has an unsupported format.",
    "timeout": "The review socket operation timed out.",
    "connection": "The review connection failed.",
    "redirect": "The review endpoint attempted a redirect; it was rejected.",
    "authentication": "The review endpoint rejected authentication.",
    "rate-limit": "The review endpoint rate limited the request.",
    "server": "The review endpoint reported a server failure.",
    "http": "The review endpoint reported an HTTP failure.",
    "response-size": "The review response exceeds 256 KiB.",
    "response-json": "The review response is not strict UTF-8 JSON.",
    "response-schema": "The review response does not satisfy the required answer contract.",
    "internal": "A toolkit review defect prevented assessment; investigate independently.",
}


def review_record(
    record_path: Path,
    evidence_path: Path | None = None,
    project_root: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Callable[[dict, str, float], bytes] | None = None,
) -> dict:
    report = {"status": "unavailable", "reasons": [], "rubric_version": RUBRIC_VERSION}
    try:
        root = resolve_project_root(record_path, project_root)
        config = load_config(root)
        if not config["enabled"]:
            report["status"] = "disabled"
            return report
        try:
            text = read_bounded(record_path, 1024 * 1024).decode("utf-8")
            if validate_markdown(text, record_path=record_path):
                raise ReviewInputError("candidate-invalid")
            record = parse_markdown(text)
            if record["schema_version"] != 2:
                raise ReviewInputError("candidate-invalid")
        except (UnicodeError, ValueError) as error:
            if isinstance(error, ReviewInputError):
                raise
            raise ReviewInputError("candidate-invalid") from None
        report["candidate_digest"] = record_digest(text)
        raw = (
            read_bounded(evidence_path, 64 * 1024)
            if evidence_path is not None
            else None
        )
        if raw is not None:
            report["evidence_digest"] = hashlib.sha256(raw).hexdigest()
        bundle, findings = prepare_evidence(
            raw,
            record["record_type"],
            project_root=root,
            root_outcome=next(
                scope["scope_definition"]["outcome"]
                for scope in record["active_scopes"]
                if scope["highest_authorized"]
            ),
        )
        if findings:
            report.update(
                status="inconclusive",
                reasons=findings,
                implementation={"state": "cannot-determine"},
            )
            return report
        credential = resolve_credential(
            root, config, os.environ if environ is None else environ
        )
        from .jev import post_json, validate_response

        state = project_state(record, bundle)
        questions = build_questions(state)
        # Give each unique judgment only its selected facts in the API's state.
        # Isolation avoids sibling evidence leakage; no retry or judgment feedback.
        reduced = {"answers": {}}
        unique = {}
        signatures = {}
        sources = {}
        for ident, question in questions.items():
            signature = json.dumps(question, sort_keys=True, ensure_ascii=False)
            if signature not in signatures:
                signatures[signature] = ident
                unique[ident] = question
            sources[ident] = signatures[signature]
        started = time.monotonic()
        request_count = 0
        for ident, question in unique.items():
            instructions = question["instructions"]
            isolated = {
                ident: {
                    **question,
                    "instructions": {
                        key: value
                        for key, value in instructions.items()
                        if key != "supplied"
                    },
                }
            }
            raw_response = (post_json if transport is None else transport)(
                {
                    "model": config["model"],
                    "state": instructions["supplied"],
                    "questions": isolated,
                },
                credential,
                config["timeout_seconds"],
            )
            validated = validate_response(raw_response, isolated)
            if "model" in reduced and reduced["model"] != validated["model"]:
                raise ReviewUnavailable("response-schema")
            reduced["model"] = validated["model"]
            reduced["answers"].update(validated["answers"])
            request_count += 1
        reduced["answers"] = {
            ident: reduced["answers"][source] for ident, source in sources.items()
        }
        report.update(
            elapsed_ms=round((time.monotonic() - started) * 1000),
            request_count=request_count,
            judgment_sources=sources,
        )
        report.update(reduced)
        implementation = summarize_case(
            bundle, reduced["answers"], config["minimum_confidence"]
        )
        progress_questions = set()
        if record["record_type"] == "continuation":
            progress_questions = {
                f"criterion-{i}"
                for i, criterion in enumerate(bundle["criteria"], 1)
                if criterion.get("claim", "pending") == "pending"
            } | {
                f"component-{c['id']}"
                for feature in bundle.get("features", [])
                for c in feature["components"]
                if c["claim"] == "pending"
            }
            pending_criteria = {
                c["id"]
                for c in bundle["criteria"]
                if c.get("claim", "pending") == "pending"
            }
            progress_questions |= {
                f"feature-implementation-{feature['id']}"
                for feature in bundle.get("features", [])
                if pending_criteria.intersection(feature["criterion_ids"])
                or any(c["claim"] == "pending" for c in feature["components"])
            }
        status, findings = summarize_answers(
            {
                key: value
                for key, value in reduced["answers"].items()
                if key not in progress_questions
            },
            config["minimum_confidence"],
        )
        _, progress = summarize_answers(
            {
                key: value
                for key, value in reduced["answers"].items()
                if key in progress_questions
            },
            config["minimum_confidence"],
        )
        for finding in findings + progress:
            question = finding["question_id"]
            if question == "criteria-coverage" or question.startswith("coverage-unit-"):
                if question.startswith("coverage-unit-"):
                    finding["requirement_id"] = question.removeprefix("coverage-unit-")
                if finding["code"] == "deficient":
                    finding.update(
                        code="criteria-inadequate",
                        message="Correct the acceptance-condition coverage without changing the authorized requirement; this finding does not establish missing implementation.",
                    )
            elif question.startswith("feature-implementation-"):
                finding["feature_id"] = question.removeprefix("feature-implementation-")
            if question.startswith("criterion-"):
                finding["criterion_id"] = bundle["criteria"][
                    int(question.removeprefix("criterion-")) - 1
                ]["id"]
            elif question.startswith("component-"):
                ident = question.removeprefix("component-")
                for feature in bundle.get("features", []):
                    for component in feature["components"]:
                        if component["id"] == ident:
                            finding.update(
                                component_id=ident,
                                feature_id=feature["id"],
                                path=component["path"],
                                evidence_ids=component["evidence_ids"],
                            )
            elif question.startswith("feature-coverage-"):
                feature = bundle["features"][
                    int(question.removeprefix("feature-coverage-")) - 1
                ]
                finding.update(
                    feature_id=feature["id"],
                    evidence_ids=feature["discovery_evidence_ids"],
                )
        report.update(
            status=status,
            reasons=findings,
            progress=progress,
            implementation=implementation,
        )
    except (ReviewInputError, ReviewUnavailable) as error:
        code = str(error)
        if code not in MESSAGES:
            code = "internal"
        report.update(
            status="error"
            if isinstance(error, ReviewInputError) and code != "internal"
            else "unavailable",
            reasons=[{"code": code, "message": MESSAGES[code]}],
        )
        stage = getattr(error, "validation_stage", None)
        if code == "response-schema" and stage in (
            "envelope",
            "answer-shape",
            "probabilities",
            "confidence",
        ):
            report["reasons"][0]["validation_stage"] = stage
    except Exception:
        # Automatic lifecycle boundaries are untouched; this explicit optional command
        # also redacts internal defects rather than echoing arbitrary exception text.
        report.update(
            status="unavailable",
            reasons=[{"code": "internal", "message": MESSAGES["internal"]}],
        )
    return report


def report_exit_code(report: dict) -> int:
    return {
        "pass": 0,
        "disabled": 0,
        "fail": 1,
        "inconclusive": 1,
        "unavailable": 1,
        "error": 2,
    }[report["status"]]
