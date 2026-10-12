from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class EvaluationTests(unittest.TestCase):
    def test_definite_negative_on_missing_proof_does_not_qualify(self):
        from agent_handoff_toolkit.review_evaluation import qualification

        rows = (
            [
                {
                    "expected": "work-remains",
                    "report": {
                        "status": "fail",
                        "implementation": {"state": "work-remains"},
                    },
                }
            ]
            * 16
            + [
                {
                    "expected": "implemented",
                    "report": {
                        "status": "pass",
                        "implementation": {"state": "implemented"},
                    },
                }
            ]
            * 16
            + [
                {
                    "expected": "cannot-determine",
                    "report": {
                        "status": "fail",
                        "implementation": {"state": "work-remains"},
                    },
                }
            ]
            * 8
        )
        self.assertFalse(qualification(self.metrics(rows)))

    def test_cli_redacts_invalid_record_and_runtime_exception(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest.mock import patch
        from agent_handoff_toolkit.cli import main

        for exception in (
            ValueError("private synthetic input"),
            RuntimeError("private synthetic input"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                output = io.StringIO()
                error = io.StringIO()
                with (
                    patch(
                        "agent_handoff_toolkit.review_evaluation.evaluate_catalog",
                        side_effect=exception,
                    ),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(error),
                ):
                    try:
                        result = main(
                            [
                                "review-evaluate",
                                "catalog.json",
                                "--project-root",
                                directory,
                                "--partition",
                                "development",
                                "--live",
                            ]
                        )
                    except Exception:
                        self.fail(
                            "Evaluation exception escaped the fixed-report boundary."
                        )
                self.assertEqual(result, 2 if isinstance(exception, ValueError) else 1)
                self.assertNotIn(
                    "private synthetic input", output.getvalue() + error.getvalue()
                )
                self.assertEqual(
                    json.loads(output.getvalue())["status"],
                    "error" if isinstance(exception, ValueError) else "unavailable",
                )

    def test_group_cannot_leak_between_development_and_held_out(self):
        import copy
        import json
        from agent_handoff_toolkit.review_evaluation import load_catalog
        from agent_handoff_toolkit.review_config import ReviewInputError

        path = Path(__file__).parent / "fixtures/jev-evaluation.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        variant = copy.deepcopy(data["cases"][0])
        variant.update(id="leaked-variant", partition="held-out")
        data["cases"].append(variant)
        with self.assertRaises(ReviewInputError):
            load_catalog(json.dumps(data).encode())

    def test_uncertainty_on_all_clean_cases_cannot_qualify_as_useful(self):
        from agent_handoff_toolkit.review_evaluation import qualification

        rows = (
            [
                {
                    "expected": "work-remains",
                    "report": {
                        "status": "fail",
                        "implementation": {"state": "work-remains"},
                    },
                }
            ]
            * 16
            + [
                {
                    "expected": "implemented",
                    "report": {
                        "status": "inconclusive",
                        "implementation": {"state": "cannot-determine"},
                    },
                }
            ]
            * 16
            + [
                {
                    "expected": "cannot-determine",
                    "report": {
                        "status": "inconclusive",
                        "implementation": {"state": "cannot-determine"},
                    },
                }
            ]
            * 8
        )
        self.assertFalse(qualification(self.metrics(rows)))

    def metrics(self, rows):
        from agent_handoff_toolkit import review_evaluation

        return review_evaluation.measure(rows)

    def test_misses_false_alarms_and_abstention_are_counted_separately(self):
        self.assertTrue(
            (
                Path(__file__).resolve().parents[1]
                / "src/agent_handoff_toolkit/review_evaluation.py"
            ).exists(),
            "No independent evaluation reducer exists yet.",
        )
        rows = [
            {
                "expected": "work-remains",
                "report": {
                    "status": "pass",
                    "implementation": {"state": "implemented"},
                },
            },
            {
                "expected": "implemented",
                "report": {
                    "status": "fail",
                    "implementation": {"state": "work-remains"},
                },
            },
            {
                "expected": "cannot-determine",
                "report": {
                    "status": "inconclusive",
                    "implementation": {"state": "cannot-determine"},
                },
            },
            {"expected": "implemented", "report": {"status": "unavailable"}},
        ]
        result = self.metrics(rows)
        self.assertEqual(result["missed_defects"], 1)
        self.assertEqual(result["false_defects"], 1)
        self.assertEqual(result["inconclusive"], 1)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["substantive_coverage"], 0.5)

    def test_unknown_answer_does_not_count_as_catching_a_defect(self):
        self.assertTrue(
            (
                Path(__file__).resolve().parents[1]
                / "src/agent_handoff_toolkit/review_evaluation.py"
            ).exists()
        )
        result = self.metrics(
            [
                {
                    "expected": "work-remains",
                    "report": {
                        "status": "inconclusive",
                        "implementation": {"state": "cannot-determine"},
                    },
                }
            ]
        )
        self.assertEqual(result["detected_defects"], 0)
        self.assertEqual(result["unresolved_defects"], 1)

    def test_disabled_service_is_not_a_successful_evaluation(self):
        self.assertTrue(
            (
                Path(__file__).resolve().parents[1]
                / "src/agent_handoff_toolkit/review_evaluation.py"
            ).exists()
        )
        result = self.metrics(
            [{"expected": "implemented", "report": {"status": "disabled"}}]
        )
        self.assertEqual(result["not_assessed"], 1)
        self.assertEqual(result["substantive_coverage"], 0)
