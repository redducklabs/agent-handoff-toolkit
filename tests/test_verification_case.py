import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_handoff_toolkit.review_rubrics import prepare_evidence
from agent_handoff_toolkit.review_config import ReviewInputError


def verification_case():
    return {
        "case_version": 2,
        "criteria": [
            {
                "id": "c1",
                "condition": "Both entry points use validation.",
                "evidence_ids": ["e1"],
                "claim": "implemented",
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "source": "synthetic source inspection",
                "text": "Both entry points call validate_input before saving.",
            }
        ],
        "features": [
            {
                "id": "f1",
                "requirement": "Validate inputs on both entry points.",
                "criterion_ids": ["c1"],
                "discovery_evidence_ids": ["e1"],
                "required_paths": ["handler.py"],
                "components": [
                    {
                        "id": "unit1",
                        "path": "handler.py",
                        "condition": "The handler validates inputs.",
                        "evidence_ids": ["e1"],
                        "claim": "implemented",
                    }
                ],
            }
        ],
        "files": [],
    }


class VerificationCaseTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX symlink setup")
    def test_file_binding_cannot_follow_symlink_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "consumer"
            root.mkdir()
            outside = base / "outside.py"
            outside.write_bytes(b"outside\n")
            (root / "handler.py").symlink_to(outside)
            bundle = verification_case()
            bundle["files"] = [
                {
                    "path": "handler.py",
                    "sha256": hashlib.sha256(b"outside\n").hexdigest(),
                }
            ]
            with self.assertRaises(ReviewInputError):
                self.prepare(bundle, root)

    def prepare(self, bundle, root=None, kind="completion-audit"):
        self.assertIn(
            "project_root",
            inspect.signature(prepare_evidence).parameters,
            "Evidence preparation cannot yet verify local file bindings.",
        )
        return prepare_evidence(json.dumps(bundle).encode(), kind, project_root=root)

    def test_explicit_feature_components_are_accepted(self):
        bundle, findings = self.prepare(verification_case())
        self.assertEqual(findings, [])
        self.assertEqual(bundle["features"][0]["components"][0]["path"], "handler.py")

    def test_unowned_acceptance_condition_is_rejected(self):
        bundle = verification_case()
        bundle["features"][0]["criterion_ids"] = []
        with self.assertRaises(ReviewInputError):
            self.prepare(bundle)

    def test_explicit_required_file_cannot_disappear_from_components(self):
        bundle = verification_case()
        bundle["features"][0]["required_paths"] = ["handler.py", "caller.py"]
        try:
            _, findings = self.prepare(bundle)
        except ReviewInputError:
            self.fail("Required location inventories are not supported yet.")
        self.assertEqual(findings[0]["code"], "unaccounted-location")
        self.assertEqual(findings[0]["path"], "caller.py")

    def test_unknown_discovery_and_component_evidence_are_rejected(self):
        for field in ("discovery_evidence_ids", "component"):
            bundle = verification_case()
            if field == "component":
                bundle["features"][0]["components"][0]["evidence_ids"] = ["absent"]
            else:
                bundle["features"][0][field] = ["absent"]
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                self.prepare(bundle)

    def test_shared_unchanged_component_path_can_serve_two_features(self):
        bundle = verification_case()
        other = copy.deepcopy(bundle["features"][0])
        other.update(id="f2", criterion_ids=["c2"])
        other["components"][0]["id"] = "unit2"
        bundle["features"].append(other)
        criterion = copy.deepcopy(bundle["criteria"][0])
        criterion["id"] = "c2"
        bundle["criteria"].append(criterion)
        self.assertEqual(self.prepare(bundle)[1], [])

    def test_stale_file_binding_is_insufficient_not_implemented(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "handler.py").write_bytes(b"changed\n")
            bundle = verification_case()
            bundle["files"] = [
                {"path": "handler.py", "sha256": hashlib.sha256(b"old\n").hexdigest()}
            ]
            _, findings = self.prepare(bundle, root)
            self.assertEqual(findings[0]["code"], "stale-evidence")

    def test_matching_file_binding_has_no_implicit_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "handler.py").write_bytes(b"local source\n")
            bundle = verification_case()
            bundle["files"] = [
                {
                    "path": "handler.py",
                    "sha256": hashlib.sha256(b"local source\n").hexdigest(),
                }
            ]
            prepared, findings = self.prepare(bundle, root)
            self.assertEqual(findings, [])
            self.assertNotIn("local source", json.dumps(prepared))

    def test_unsafe_component_paths_and_duplicate_ownership_are_rejected(self):
        for path in ("../outside.py", "/absolute.py", "C:/outside.py", "a\\b.py"):
            bundle = verification_case()
            bundle["features"][0]["components"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ReviewInputError):
                self.prepare(bundle)
        bundle = verification_case()
        feature = copy.deepcopy(bundle["features"][0])
        feature["id"] = "f2"
        feature["components"][0]["id"] = "unit2"
        bundle["features"].append(feature)
        with self.assertRaises(ReviewInputError):
            self.prepare(bundle)

    def test_pending_work_without_proof_is_allowed_in_continuation(self):
        bundle = verification_case()
        bundle["criteria"][0].update(claim="pending", evidence_ids=[])
        bundle["features"][0]["components"][0].update(claim="pending", evidence_ids=[])
        self.assertEqual(self.prepare(bundle, kind="continuation")[1], [])
