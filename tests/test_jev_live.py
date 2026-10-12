"""Explicit opt-in only; synthetic state and process credential, no dotenv reads."""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@unittest.skipUnless(
    os.environ.get("AHK_JEV_LIVE_SMOKE") == "1",
    "live Jev smoke requires explicit opt-in",
)
class LiveSmokeTests(unittest.TestCase):
    def test_synthetic_choice_contract(self):
        from agent_handoff_toolkit.jev import post_json, validate_response
        from agent_handoff_toolkit.review_config import ReviewUnavailable

        credential = os.environ.get("JEV_API_KEY")
        if not credential:
            self.skipTest("live smoke requires configured process credentials")
        questions = {
            "synthetic": {
                "type": "choice",
                "instructions": "Does the supplied number equal two?",
                "criteria": {
                    "supported": "Number equals two.",
                    "deficient": "Number differs from two.",
                    "insufficient_evidence": "Number is absent.",
                },
            }
        }
        try:
            result = validate_response(
                post_json(
                    {
                        "model": "jev-1.13.0",
                        "state": {"number": 2},
                        "questions": questions,
                    },
                    credential,
                    10,
                ),
                questions,
            )
        except ReviewUnavailable as error:
            self.fail("synthetic live smoke unavailable: " + str(error))
        self.assertEqual(set(result["answers"]), {"synthetic"})
