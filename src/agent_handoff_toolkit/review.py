"""Ordered optional review; reports never enter deterministic enforcement."""

from collections.abc import Mapping, Callable
import hashlib
import os
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
        bundle, findings = prepare_evidence(raw, record["record_type"])
        if findings:
            report.update(status="fail", reasons=findings)
            return report
        credential = resolve_credential(
            root, config, os.environ if environ is None else environ
        )
        from .jev import post_json, validate_response

        state = project_state(record, bundle)
        questions = build_questions(state)
        raw_response = (post_json if transport is None else transport)(
            {"model": config["model"], "state": state, "questions": questions},
            credential,
            config["timeout_seconds"],
        )
        reduced = validate_response(raw_response, questions)
        report.update(reduced)
        status, findings = summarize_answers(
            reduced["answers"], config["minimum_confidence"]
        )
        for finding in findings:
            question = finding["question_id"]
            if question.startswith("criterion-"):
                finding["criterion_id"] = bundle["criteria"][
                    int(question.removeprefix("criterion-")) - 1
                ]["id"]
        report.update(status=status, reasons=findings)
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
    except Exception:
        # Automatic lifecycle boundaries are untouched; this explicit optional command
        # also redacts internal defects rather than echoing arbitrary exception text.
        report.update(
            status="unavailable",
            reasons=[{"code": "internal", "message": MESSAGES["internal"]}],
        )
    return report


def report_exit_code(report: dict) -> int:
    return {"pass": 0, "disabled": 0, "fail": 1, "unavailable": 1, "error": 2}[
        report["status"]
    ]
