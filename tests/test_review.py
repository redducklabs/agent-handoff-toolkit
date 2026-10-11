import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_handoff_toolkit.review import review_record, report_exit_code  # noqa: E402
from agent_handoff_toolkit import render_record  # noqa: E402
from agent_handoff_toolkit.lineage import record_digest  # noqa: E402
from agent_handoff_toolkit.review_config import ReviewUnavailable  # noqa: E402
from test_records import make_v2_audit, make_v2_continuation, load_fixture  # noqa: E402
from test_review_rubrics import evidence_bundle, answer  # noqa: E402


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / ".agent-handoff-toolkit" / "jev.json"
        self.config.parent.mkdir()
        self.config.write_text('{"enabled":true}')
        self.record = self.root / "candidate.md"
        self.record.write_text(render_record(make_v2_audit()), encoding="utf-8")
        self.evidence = self.root / "evidence.json"
        self.evidence.write_text(json.dumps(evidence_bundle()))
        self.environ = {"JEV_API_KEY": secrets.token_hex(16)}

    def review(self, transport=None, evidence=True, environ=None):
        return review_record(
            self.record,
            self.evidence if evidence else None,
            self.root,
            environ=self.environ if environ is None else environ,
            transport=transport or self.transport,
        )

    def transport(self, payload, credential, timeout):
        self.assertTrue(credential == self.environ["JEV_API_KEY"])
        self.assertEqual(set(payload), {"state", "model", "questions"})
        self.assertEqual(timeout, 10)
        state = json.dumps(payload["state"])
        for key in ("evidence_hmac", "authorization_id", "predecessor", "record_id"):
            self.assertNotIn(key, state)
        return json.dumps(
            {
                "model": "jev-1.13.0",
                "answers": {key: answer() for key in payload["questions"]},
            }
        ).encode()

    def test_disabled_checks_no_candidate_evidence_or_credential(self):
        self.config.unlink()
        self.record.unlink()
        self.evidence.unlink()
        with patch(
            "agent_handoff_toolkit.review.resolve_credential",
            side_effect=AssertionError("credential read"),
        ):
            report = self.review(lambda *args: self.fail("HTTP"))
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(report_exit_code(report), 0)

    def test_local_findings_precede_credentials_and_transport(self):
        with patch(
            "agent_handoff_toolkit.review.resolve_credential",
            side_effect=AssertionError("credential read"),
        ):
            self.assertEqual(self.review(evidence=False)["status"], "fail")
            bundle = evidence_bundle()
            bundle["criteria"].append(
                {
                    "id": "missing-proof",
                    "condition": "More work complete.",
                    "evidence_ids": [],
                }
            )
            self.evidence.write_text(json.dumps(bundle))
            result = self.review()
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["reasons"][0]["criterion_id"], "missing-proof")

    def test_audit_pass_and_input_digests(self):
        report = self.review()
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["candidate_digest"],
            record_digest(self.record.read_text(encoding="utf-8")),
        )
        self.assertEqual(
            report["evidence_digest"],
            hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
        )
        self.assertEqual(report["model"], "jev-1.13.0")
        old = report
        self.evidence.write_bytes(self.evidence.read_bytes() + b" ")
        self.record.write_text(
            render_record(make_v2_audit(record_id="record-new")), encoding="utf-8"
        )
        new = self.review()
        self.assertNotEqual(old["candidate_digest"], new["candidate_digest"])
        self.assertNotEqual(old["evidence_digest"], new["evidence_digest"])

    def test_substantive_and_confidence_failures(self):
        for key, choice, confidence, expected in (
            ("root-completion", "deficient", 0.95, "deficient"),
            ("criterion-1", "insufficient_evidence", 0.95, "insufficient-evidence"),
            ("goal-clarity", "deficient", 0.95, "deficient"),
            ("scope-alignment", "supported", 0.1, "low-confidence"),
        ):

            def transport(payload, credential, timeout):
                answers = {name: answer() for name in payload["questions"]}
                answers[key] = answer(choice, confidence)
                return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

            report = self.review(transport)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["reasons"][0]["code"], expected)
            if key == "criterion-1":
                self.assertEqual(report["reasons"][0]["criterion_id"], "acceptance-1")

    def test_continuation_does_not_require_future_completion(self):
        self.record.write_text(render_record(make_v2_continuation()), encoding="utf-8")
        bundle = evidence_bundle()
        bundle["criteria"][0]["evidence_ids"] = []
        self.evidence.write_text(json.dumps(bundle))
        self.assertEqual(self.review()["status"], "pass")

    def test_input_errors_precede_enabled_review(self):
        self.config.write_text('{"enabled":false,"bad":1}')
        self.assertEqual(self.review()["status"], "error")
        self.config.write_text('{"enabled":true}')
        for raw in (b"{", b"\xff", b"{}"):
            self.evidence.write_bytes(raw)
            self.assertEqual(self.review()["status"], "error")
        self.evidence.write_text(json.dumps(evidence_bundle()))
        self.record.write_text("invalid")
        self.assertEqual(self.review()["status"], "error")
        v1 = load_fixture("completion-audit.json")
        v1["schema_version"] = 1
        # A missing/invalid v2 candidate must fail before network access.
        self.record.unlink()
        self.assertEqual(self.review()["status"], "error")

    def test_faults_and_unknown_exception_are_redacted(self):
        self.assertEqual(self.review(environ={})["status"], "unavailable")
        for fault, code in (
            (ReviewUnavailable("connection"), "connection"),
            (RuntimeError("private data"), "internal"),
        ):

            def transport(*args):
                raise fault

            report = self.review(transport)
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["reasons"][0]["code"], code)
            self.assertNotIn("private data", json.dumps(report))
        self.assertEqual(self.review(lambda *args: b"{")["status"], "unavailable")
        for status, code in (
            ("pass", 0),
            ("disabled", 0),
            ("fail", 1),
            ("unavailable", 1),
            ("error", 2),
        ):
            self.assertEqual(report_exit_code({"status": status}), code)
