import hashlib
import hmac
import math
import unittest

from agent_handoff_toolkit.lineage import (
    LineageError,
    canonical_json_bytes,
    evidence_hmac,
    normalize_text,
    record_digest,
    scope_definition_digest,
    validate_hex_digest,
    validate_identifier,
    validate_scope_definition,
)


class LineageTests(unittest.TestCase):
    def test_scope_digest_is_key_order_independent(self):
        first = {
            "scope_id": "issue-1",
            "scope_kind": "issue",
            "parent_scope_id": None,
            "scope_definition": {"title": "Title", "outcome": "Outcome"},
        }
        second = {
            "scope_definition": {"outcome": "Outcome", "title": "Title"},
            "parent_scope_id": None,
            "scope_kind": "issue",
            "scope_id": "issue-1",
        }
        self.assertEqual(
            scope_definition_digest(first), scope_definition_digest(second)
        )
        self.assertEqual(
            scope_definition_digest({**first, "status": "in-progress"}),
            scope_definition_digest(first),
        )

    def test_scope_digest_changes_for_semantic_whitespace(self):
        base = {
            "scope_id": "issue-1",
            "scope_kind": "issue",
            "parent_scope_id": None,
            "scope_definition": {"title": "Title", "outcome": "Outcome"},
        }
        changed = {
            **base,
            "scope_definition": {"title": "Title", "outcome": "Out  come"},
        }
        self.assertNotEqual(
            scope_definition_digest(base), scope_definition_digest(changed)
        )

    def test_utf8_is_preserved(self):
        self.assertEqual(
            canonical_json_bytes({"text": "café 🐍"}), '{"text":"café 🐍"}'.encode()
        )

    def test_canonical_json_rejects_nonfinite_and_unsupported_values(self):
        for value in ({"n": math.nan}, {"n": math.inf}, {"n": object()}):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    canonical_json_bytes(value)

    def test_identifiers_boundaries_and_invalid_values(self):
        valid = "a" + "x" * 127
        self.assertEqual(validate_identifier(valid, label="id"), valid)
        for value in (valid + "x", "_bad", "a b", "a/b", "a\n"):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    validate_identifier(value, label="id")

    def test_digests_require_lowercase_sha256_hex(self):
        valid = "a" * 64
        self.assertEqual(validate_hex_digest(valid, label="digest"), valid)
        for value in ("A" * 64, "a" * 63, "g" * 64):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    validate_hex_digest(value, label="digest")

    def test_text_validation(self):
        self.assertEqual(normalize_text("  hi  ".strip(), limit=2, label="text"), "hi")
        for value in ("", " ", " hi", "hi ", "a\r\nb", "a\x00b", "éé"):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    normalize_text(value, limit=2, label="text")

    def test_scope_definition_requires_exact_normalized_fields(self):
        self.assertEqual(
            validate_scope_definition({"title": "T", "outcome": "O"}),
            {"title": "T", "outcome": "O"},
        )
        for value in ({"title": "T"}, {"title": "T", "outcome": "O", "x": "y"}):
            with self.subTest(value=value):
                with self.assertRaises(LineageError):
                    validate_scope_definition(value)

    def test_record_digest_normalizes_line_endings(self):
        expected = hashlib.sha256(b"a\nb\nc").hexdigest()
        self.assertEqual(record_digest("a\nb\nc"), expected)
        self.assertEqual(record_digest("a\r\nb\r\nc"), expected)
        self.assertEqual(record_digest("a\rb\rc"), expected)

    def test_hmac_is_deterministic_and_sensitive(self):
        payload = {"b": 2, "a": "x"}
        expected = hmac.new(
            b"secret", canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
        self.assertEqual(evidence_hmac(b"secret", payload), expected)
        self.assertNotEqual(evidence_hmac(b"other", payload), expected)
        self.assertNotEqual(evidence_hmac(b"secret", {"a": "y"}), expected)
        with self.assertRaises(LineageError):
            evidence_hmac(b"", payload)


if __name__ == "__main__":
    unittest.main()
