import json
import http.client
from pathlib import Path
import secrets
import socket
import ssl
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_handoff_toolkit.jev import (  # noqa: E402
    validate_response,
    post_json,
    NoRedirect,
    MAX_RESPONSE_BYTES,
)
from agent_handoff_toolkit.review_config import ReviewUnavailable  # noqa: E402

# Independently handwritten contract from https://docs.typesafe.ai/api (2026-10-11).
WIRE = b'{"model":"jev-1.13.0","answers":{"goal-clarity":{"type":"choice","choice":"supported","probabilities":{"supported":0.9,"deficient":0.1,"insufficient_evidence":0.0},"confidence":0.85}},"usage":{"input_tokens":1,"output_tokens":1}}'
QUESTIONS = {"goal-clarity": {"type": "choice"}}


class ResponseTests(unittest.TestCase):
    def test_incoherent_confidence_cannot_pass(self):
        data = json.loads(WIRE)
        data["answers"]["goal-clarity"]["probabilities"] = {
            "supported": 0.34,
            "deficient": 0.33,
            "insufficient_evidence": 0.33,
        }
        data["answers"]["goal-clarity"]["confidence"] = 0.95
        with self.assertRaisesRegex(ReviewUnavailable, "^response-schema$"):
            validate_response(json.dumps(data).encode(), QUESTIONS)

    def test_wire_and_unknown_fields_reduced(self):
        data = json.loads(WIRE)
        data["extra"] = "discard"
        data["answers"]["goal-clarity"]["extra"] = "discard"
        result = validate_response(json.dumps(data).encode(), QUESTIONS)
        self.assertEqual(set(result), {"model", "answers"})
        self.assertEqual(
            set(result["answers"]["goal-clarity"]),
            {"type", "choice", "probabilities", "confidence"},
        )
        self.assertEqual(result["model"], "jev-1.13.0")

    def test_bad_json_and_size(self):
        for raw in (
            b"{",
            b"\xff",
            b'{"x":NaN}',
            b'{"x":1e999}',
            b'{"x":{"y":1,"y":2}}',
        ):
            with self.assertRaisesRegex(ReviewUnavailable, "^response-json$"):
                validate_response(raw, QUESTIONS)
        with self.assertRaisesRegex(ReviewUnavailable, "^response-size$"):
            validate_response(b" " * (MAX_RESPONSE_BYTES + 1), QUESTIONS)

    def test_huge_probability_integer_is_schema_fault(self):
        data = json.loads(WIRE)
        data["answers"]["goal-clarity"]["probabilities"]["supported"] = 10**400
        with self.assertRaisesRegex(ReviewUnavailable, "^response-schema$"):
            validate_response(json.dumps(data).encode(), QUESTIONS)

    def test_response_schema(self):
        variants = []
        for key, values in (
            ("type", ["noul", None]),
            ("choice", ["other", "deficient", None]),
            ("confidence", [True, -0.1, 1.1, None]),
            (
                "probabilities",
                [
                    {},
                    {"supported": 0.9, "deficient": 0.1},
                    {
                        "supported": 1,
                        "deficient": 0,
                        "insufficient_evidence": 0,
                        "extra": 0,
                    },
                    {"supported": True, "deficient": 0, "insufficient_evidence": 0},
                    {"supported": -0.1, "deficient": 1.1, "insufficient_evidence": 0},
                    {"supported": 0.8, "deficient": 0.1, "insufficient_evidence": 0},
                ],
            ),
        ):
            for value in values:
                data = json.loads(WIRE)
                data["answers"]["goal-clarity"][key] = value
                variants.append(data)
        for model in ("", "x y", "x" * 129, 1):
            data = json.loads(WIRE)
            data["model"] = model
            variants.append(data)
        for answers in ({}, {"extra": {}}, [], {"goal-clarity": None}):
            data = json.loads(WIRE)
            data["answers"] = answers
            variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ReviewUnavailable):
                validate_response(json.dumps(data).encode(), QUESTIONS)
        data = json.loads(WIRE)
        data["answers"]["goal-clarity"]["probabilities"] = {
            "supported": 0.5,
            "deficient": 0.5,
            "insufficient_evidence": 0,
        }
        data["answers"]["goal-clarity"]["confidence"] = 0.25
        self.assertTrue(validate_response(json.dumps(data).encode(), QUESTIONS))


class TransportTests(unittest.TestCase):
    def test_one_bounded_utf8_request(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = WIRE
        opener = MagicMock()
        opener.open.return_value = response
        credential = secrets.token_hex(16)
        payload = {
            "model": "jev-1.13.0",
            "state": {"text": "synthetic \u2713"},
            "questions": QUESTIONS,
        }
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(post_json(payload, credential, 4), WIRE)
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.get_header("Authorization") == "Bearer " + credential)
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 4)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_http_and_network_failures_are_fixed_codes(self):
        cases = [
            (urllib.error.HTTPError("synthetic", status, "remote text", {}, None), code)
            for status, code in (
                (301, "redirect"),
                (302, "redirect"),
                (307, "redirect"),
                (308, "redirect"),
                (401, "authentication"),
                (403, "authentication"),
                (429, "rate-limit"),
                (529, "server"),
                (503, "server"),
                (400, "http"),
            )
        ]
        cases += [
            (http.client.IncompleteRead(b"synthetic", 1), "connection"),
            (http.client.BadStatusLine("synthetic"), "connection"),
            (socket.timeout("private"), "timeout"),
            (urllib.error.URLError("private"), "connection"),
            (ssl.SSLError("private"), "connection"),
        ]
        for exception, code in cases:
            opener = MagicMock()
            opener.open.side_effect = exception
            with (
                patch("urllib.request.build_opener", return_value=opener),
                self.assertRaisesRegex(ReviewUnavailable, "^" + code + "$"),
            ):
                post_json({}, secrets.token_hex(16), 1)
            self.assertEqual(opener.open.call_count, 1)

    def test_redirect_handler_and_bad_bearer(self):
        self.assertIsNone(
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.invalid"
            )
        )
        for credential in ("x y", "\u2713", "x\r\ny", ""):
            with (
                patch("urllib.request.build_opener") as opener,
                self.assertRaisesRegex(ReviewUnavailable, "^credential-format$"),
            ):
                post_json({}, credential, 1)
            opener.assert_not_called()

    def test_bad_transport_bodies(self):
        for body, code in (
            (b"x" * (MAX_RESPONSE_BYTES + 1), "response-size"),
            (b"\xff", "response-json"),
        ):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = body
            opener = MagicMock()
            opener.open.return_value = response
            with (
                patch("urllib.request.build_opener", return_value=opener),
                self.assertRaisesRegex(ReviewUnavailable, "^" + code + "$"),
            ):
                post_json({}, secrets.token_hex(16), 1)
