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
from agent_handoff_toolkit import render_record, parse_markdown  # noqa: E402
from agent_handoff_toolkit.lineage import record_digest  # noqa: E402
from agent_handoff_toolkit.review_config import ReviewUnavailable  # noqa: E402
from test_records import make_v2_audit, make_v2_continuation  # noqa: E402
from test_review_rubrics import evidence_bundle, answer  # noqa: E402
from test_verification_case import verification_case  # noqa: E402


class ReviewTests(unittest.TestCase):
    def test_exact_accounting_does_not_fabricate_implementation_or_confidence(self):
        from test_verification_case import anchored_case

        bundle = anchored_case()
        outcome = parse_markdown(self.record.read_text(encoding="utf-8"))[
            "active_scopes"
        ][0]["scope_definition"]["outcome"]
        bundle["criteria"][0].update(condition=outcome)
        bundle["requirements"][0].update(quote=outcome, end=len(outcome))
        for choice, expected in (
            ("insufficient_evidence", "cannot-determine"),
            ("deficient", "work-remains"),
        ):
            self.evidence.write_text(json.dumps(bundle))

            def transport(payload, credential, timeout):
                answers = {key: answer() for key in payload["questions"]}
                answers["criterion-1"] = answer(choice)
                return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

            report = self.review(transport)
            self.assertEqual(report["implementation"]["state"], expected)
            self.assertNotIn("coverage-unit-req-0", report["answers"])
            self.assertEqual(
                report["implementation"]["coverage_provenance"][0]["method"],
                "exact-text",
            )

    def test_protocol_fault_reports_closed_validation_stage_without_payload(self):
        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criterion-1"]["probabilities"]["supported"] = 0.2
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["reasons"][0].get("validation_stage"), "probabilities")
        self.assertNotIn("answers", report)

    def test_one_uncertain_anchored_unit_prevents_supported_completion(self):
        from test_verification_case import anchored_case

        bundle = anchored_case()
        outcome = parse_markdown(self.record.read_text(encoding="utf-8"))[
            "active_scopes"
        ][0]["scope_definition"]["outcome"]
        unit = bundle["requirements"][0]
        unit.update(quote=outcome, end=len(outcome))
        self.evidence.write_text(json.dumps(bundle))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["coverage-unit-req-0"] = answer("supported", 0.6)
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["implementation"]["behavior_state"], "implemented")
        self.assertEqual(report["implementation"]["state"], "cannot-determine")
        self.assertEqual(report["reasons"][0]["requirement_id"], "req-0")

    def test_coverage_gap_does_not_claim_missing_implementation(self):
        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criteria-coverage"] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["implementation"].get("behavior_state"), "implemented")
        self.assertEqual(report["implementation"]["state"], "cannot-determine")
        self.assertEqual(report["implementation"]["coverage"], "inadequate")
        self.assertEqual(report["reasons"][0]["code"], "criteria-inadequate")

    def test_full_feature_gap_survives_supported_narrow_criteria(self):
        self.evidence.write_text(json.dumps(verification_case()))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            for key in answers:
                if key.startswith("feature-implementation-"):
                    answers[key] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["implementation"]["state"], "work-remains")

    def test_identical_component_and_criterion_share_one_judgment(self):
        bundle = verification_case()
        criterion = bundle["criteria"][0]
        component = bundle["features"][0]["components"][0]
        component["condition"] = criterion["condition"]
        component["evidence_ids"] = criterion["evidence_ids"][:]
        self.evidence.write_text(json.dumps(bundle))
        sent = []

        def transport(payload, credential, timeout):
            sent.extend(payload["questions"])
            answers = {key: answer() for key in payload["questions"]}
            answers["criterion-1"] = answer("deficient", 0.445)
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertNotIn("component-unit1", sent)
        self.assertEqual(
            report["answers"]["component-unit1"], report["answers"]["criterion-1"]
        )
        self.assertEqual(report["implementation"]["state"], "cannot-determine")
        self.assertEqual(report["status"], "inconclusive")

    def test_reused_pending_judgment_does_not_excuse_implemented_component(self):
        self.record.write_text(render_record(make_v2_continuation()), encoding="utf-8")
        bundle = verification_case()
        criterion = bundle["criteria"][0]
        criterion["claim"] = "pending"
        component = bundle["features"][0]["components"][0]
        component["condition"] = criterion["condition"]
        component["evidence_ids"] = criterion["evidence_ids"][:]
        self.evidence.write_text(json.dumps(bundle))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criterion-1"] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["judgment_sources"]["component-unit1"], "criterion-1")
        self.assertEqual(report["reasons"][0]["component_id"], "unit1")
        self.assertEqual(report["progress"][0]["criterion_id"], criterion["id"])

    def test_missing_discovery_is_inconclusive_before_network(self):
        bundle = verification_case()
        bundle["features"][0]["discovery_evidence_ids"] = []
        self.evidence.write_text(json.dumps(bundle))
        report = self.review(
            lambda *args: self.fail("missing discovery reached network")
        )
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["reasons"][0]["code"], "missing-discovery")

    def test_large_case_batches_without_losing_child_findings(self):
        # Distinct obligations must remain separate, even when paths are shared.
        bundle = verification_case()
        bundle["criteria"] = []
        bundle["features"] = []
        for index in range(16):
            bundle["criteria"].append(
                {
                    "id": f"c{index}",
                    "condition": f"Validate criterion {index}.",
                    "evidence_ids": ["e1"],
                    "claim": "implemented",
                }
            )
            bundle["features"].append(
                {
                    "id": f"f{index}",
                    "requirement": "Validate input.",
                    "criterion_ids": [f"c{index}"],
                    "discovery_evidence_ids": ["e1"],
                    "required_paths": ["shared.py"],
                    "components": [
                        {
                            "id": f"unit{index}-{child}",
                            "path": "shared.py",
                            "condition": f"Validate component {index}-{child}.",
                            "evidence_ids": ["e1"],
                            "claim": "implemented",
                        }
                        for child in range(2)
                    ],
                }
            )
        self.evidence.write_text(json.dumps(bundle))
        batches = []

        def transport(payload, credential, timeout):
            batches.append(list(payload["questions"]))
            answers = {key: answer() for key in payload["questions"]}
            if "component-unit15-1" in answers:
                answers["component-unit15-1"] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["request_count"], 3)
        self.assertTrue(all(len(batch) <= 21 for batch in batches))
        self.assertEqual(
            report["implementation"]["features"][-1]["state"], "work-remains"
        )
        calls = 0

        def failing_later_batch(payload, credential, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ReviewUnavailable("connection")
            return transport(payload, credential, timeout)

        partial = self.review(failing_later_batch)
        self.assertEqual(partial["status"], "unavailable")
        for withheld in ("answers", "implementation", "judgment_sources"):
            self.assertNotIn(withheld, partial)

    def test_distinct_component_proof_keeps_its_own_negative_judgment(self):
        bundle = verification_case()
        component = bundle["features"][0]["components"][0]
        component["condition"] = bundle["criteria"][0]["condition"]
        bundle["evidence"].append(
            {
                "id": "current-gap",
                "source": "different caller",
                "text": "The batch caller bypasses validation.",
            }
        )
        component["evidence_ids"] = ["current-gap"]
        self.evidence.write_text(json.dumps(bundle))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["component-unit1"] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(
            report["judgment_sources"]["component-unit1"], "component-unit1"
        )
        self.assertEqual(report["implementation"]["state"], "work-remains")

    def test_hierarchical_report_localizes_unfinished_component_without_source_text(
        self,
    ):
        self.evidence.write_text(json.dumps(verification_case()))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            if "component-unit1" in answers:
                answers["component-unit1"] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["implementation"]["state"], "work-remains")
        feature = report["implementation"]["features"][0]
        self.assertEqual(feature["state"], "work-remains")
        self.assertEqual(feature["components"][0]["path"], "handler.py")
        self.assertNotIn("Both entry points call", json.dumps(report))

    def test_honest_pending_continuation_does_not_raise_defect_alarm(self):
        self.record.write_text(render_record(make_v2_continuation()), encoding="utf-8")
        bundle = verification_case()
        bundle["criteria"][0]["claim"] = "pending"
        bundle["features"][0]["components"][0]["claim"] = "pending"
        self.evidence.write_text(json.dumps(bundle))

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            for key in ("criterion-1", "component-unit1", "feature-implementation-f1"):
                if key in answers:
                    answers[key] = answer("deficient")
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["implementation"]["state"], "work-remains")
        self.assertEqual(report["progress"][0]["code"], "deficient")

    def test_unknown_work_cannot_be_reported_as_implemented(self):
        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criterion-1"] = answer("deficient", 0.445)
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["implementation"]["state"], "cannot-determine")
        self.assertEqual(report_exit_code(report), 1)

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
            self.assertEqual(self.review(evidence=False)["status"], "inconclusive")
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
            self.assertEqual(result["status"], "inconclusive")
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

    def test_float_noise_does_not_lower_coherent_confidence_below_floor(self):
        self.config.write_text('{"enabled":true,"minimum_confidence":0.9}')

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criteria-coverage"].update(
                probabilities={
                    "supported": 0.9333333333333333,
                    "deficient": 0.0666666666666667,
                    "insufficient_evidence": 0.0,
                },
                confidence=0.9,
            )
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["answers"]["criteria-coverage"]["confidence"], 0.9)

    def test_rounded_probability_formula_hits_floor_without_binary_drift(self):
        self.config.write_text('{"enabled":true,"minimum_confidence":0.91}')

        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criteria-coverage"].update(
                probabilities={
                    "supported": 0.94,
                    "deficient": 0.06,
                    "insufficient_evidence": 0.0,
                },
                confidence=0.92,
            )
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["answers"]["criteria-coverage"]["confidence"], 0.91)

    def test_wire_rounding_cannot_raise_confidence_above_floor(self):
        def transport(payload, credential, timeout):
            answers = {key: answer() for key in payload["questions"]}
            answers["criteria-coverage"].update(
                probabilities={
                    "supported": 0.86,
                    "deficient": 0.14,
                    "insufficient_evidence": 0.0,
                },
                confidence=0.8,
            )
            return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

        report = self.review(transport)
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["reasons"][0]["code"], "low-confidence")
        self.assertEqual(report["reasons"][0]["question_id"], "criteria-coverage")
        self.assertAlmostEqual(
            report["answers"]["criteria-coverage"]["confidence"], 0.79
        )

    def test_substantive_and_confidence_failures(self):
        for key, choice, confidence, expected, status in (
            ("criterion-1", "deficient", 0.95, "deficient", "fail"),
            (
                "criterion-1",
                "insufficient_evidence",
                0.95,
                "insufficient-evidence",
                "inconclusive",
            ),
            ("criteria-coverage", "deficient", 0.95, "criteria-inadequate", "fail"),
            ("criteria-coverage", "supported", 0.1, "low-confidence", "inconclusive"),
        ):

            def transport(payload, credential, timeout):
                answers = {name: answer() for name in payload["questions"]}
                answers[key] = answer(choice, confidence)
                return json.dumps({"model": "jev-1.13.0", "answers": answers}).encode()

            report = self.review(transport)
            self.assertEqual(report["status"], status)
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
        v1 = render_record(make_v2_audit()).replace(
            '"schema_version": 2', '"schema_version": 1'
        )
        self.record.write_text(v1, encoding="utf-8")
        self.assertEqual(self.review()["status"], "error")
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
