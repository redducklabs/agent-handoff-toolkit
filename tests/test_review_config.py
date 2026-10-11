import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_handoff_toolkit.review_config import (  # noqa: E402
    load_config,
    resolve_project_root,
    resolve_credential,
    strict_object,
    read_bounded,
    ReviewInputError,
    ReviewUnavailable,
)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_root_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                load_config(root),
                {
                    "enabled": False,
                    "credential": {
                        "variable": "JEV_API_KEY",
                        "source": "environment-or-file",
                        "file": ".env",
                    },
                    "model": "jev-1.13.0",
                    "timeout_seconds": 10,
                    "minimum_confidence": 0.8,
                },
            )
            child = root / "nested"
            child.mkdir()
            with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(root.parent)}):
                with self.assertRaises(ReviewInputError):
                    resolve_project_root(child / "record.md", None)
                (root / ".git").write_text("gitdir: synthetic")
                self.assertEqual(
                    resolve_project_root(child / "absent.md", None), root.resolve()
                )
                self.assertEqual(
                    resolve_project_root(child / "absent.md", child), child.resolve()
                )
            with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(root)}):
                with self.assertRaises(ReviewInputError):
                    resolve_project_root(child / "record.md", None)
            with self.assertRaises(ReviewInputError):
                resolve_project_root(child / "r", root / "missing")

    def test_strict_json_and_bounds(self):
        for raw in (
            b"{",
            b"[]",
            b'{"a":{"x":1,"x":2}}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":1e999}',
            b"\xff",
        ):
            with self.subTest(raw=raw), self.assertRaises(ReviewInputError):
                strict_object(raw)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input"
            p.write_bytes(b"abc")
            self.assertEqual(read_bounded(p, 3), b"abc")
            with self.assertRaises(ReviewInputError):
                read_bounded(p, 2)
            with self.assertRaises(ReviewInputError):
                read_bounded(Path(d) / "missing", 2)

    def test_config_validation(self):
        invalid = [
            {"unknown": 1},
            {"enabled": 1},
            {"credential": {"unknown": 1}},
            {"credential": {"variable": "bad-name"}},
            {"credential": {"source": "shell"}},
            {"credential": {"file": ""}},
            {"model": ""},
            {"model": "x y"},
            {"model": "x" * 129},
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": 31},
            {"minimum_confidence": -0.1},
            {"minimum_confidence": 1.1},
            {"minimum_confidence": False},
        ]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = root / ".agent-handoff-toolkit" / "jev.json"
            config.parent.mkdir()
            for data in invalid:
                config.write_text(json.dumps(data))
                with (
                    self.subTest(data=data),
                    self.assertRaisesRegex(ReviewInputError, "^config-schema$"),
                ):
                    load_config(root)
            for data in (
                {"timeout_seconds": 30},
                {"minimum_confidence": 0},
                {"minimum_confidence": 1},
            ):
                config.write_text(json.dumps(data))
                self.assertEqual(load_config(root) | data, load_config(root))
            for raw in (b"\xff", b" " * 16385, b'{"enabled":true,"enabled":false}'):
                config.write_bytes(raw)
                with self.assertRaises(ReviewInputError):
                    load_config(root)

    def test_credentials_selected_only_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = load_config(root)
            value = secrets.token_hex(16)
            with patch("subprocess.run", side_effect=AssertionError("executed")):
                for content, expected in [
                    (
                        "bad unrelated line\r\nexport JEV_API_KEY="
                        + value
                        + " # comment\r\n",
                        value,
                    ),
                    ('JEV_API_KEY="$(fake)#=literal" # ok', "$(fake)#=literal"),
                    ("JEV_API_KEY='literal#=text'", "literal#=text"),
                    ("JEV_API_KEY=x#y", "x#y"),
                ]:
                    (root / ".env").write_text(content)
                    before = dict(os.environ)
                    self.assertTrue(resolve_credential(root, config, {}) == expected)
                    self.assertEqual(dict(os.environ), before)
                self.assertTrue(
                    resolve_credential(root, config, {"JEV_API_KEY": value}) == value
                )
                for content in (
                    "JEV_API_KEY=",
                    'JEV_API_KEY="bad',
                    "JEV_API_KEY=x\nJEV_API_KEY=y",
                    'JEV_API_KEY="x" garbage',
                ):
                    (root / ".env").write_text(content)
                    with self.assertRaises(ReviewUnavailable):
                        resolve_credential(root, config, {})
                (root / ".env").write_text("JEV_API_KEY=" + value)
                self.assertTrue(
                    resolve_credential(root, config, {"JEV_API_KEY": ""}) == value
                )
                config["credential"]["source"] = "environment"
                with self.assertRaisesRegex(ReviewUnavailable, "^credential-missing$"):
                    resolve_credential(root, config, {})
                config["credential"] = {
                    "source": "file",
                    "variable": "CUSTOM",
                    "file": str(root / "key"),
                }
                (root / "key").write_text("CUSTOM=" + value)
                self.assertTrue(
                    resolve_credential(root, config, {"CUSTOM": "ignored"}) == value
                )
                (root / "key").write_bytes(b"x" * 65537)
                with self.assertRaises(ReviewUnavailable):
                    resolve_credential(root, config, {})
                (root / "key").unlink()
                with self.assertRaises(ReviewUnavailable):
                    resolve_credential(root, config, {})
