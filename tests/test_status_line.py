"""The Claude Code status-line relay: records the reported window, then steps aside.

Payloads here are synthetic and hold structure and numbers only.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.context_window import (  # noqa: E402
    record_window,
    relay_status_line,
    reported_window,
)

INNER = (
    f'"{Path(sys.executable).as_posix()}" -c "import sys; '
    "data = sys.stdin.read(); sys.stdout.write('inner:' + str(len(data)))\""
)


class StatusLineRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root / "local"),
                "XDG_STATE_HOME": str(self.root / "local"),
                "CLAUDE_CONFIG_DIR": str(self.root / "config"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AHK_STATUSLINE_RELAY", None)
        (self.root / "config").mkdir()
        self.transcript = str(self.root / "transcript.jsonl")

    def payload(self, window=1_000_000, **changes):
        value = {
            "session_id": "session-1",
            "transcript_path": self.transcript,
            "cwd": str(self.root),
            "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
            "context_window": {
                "context_window_size": window,
                "used_percentage": 17,
            },
        }
        value.update(changes)
        return json.dumps(value).encode()

    def user_status_line(self, command):
        (self.root / "config" / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": command}}),
            encoding="utf-8",
        )

    def cache_files(self):
        return [path for path in (self.root / "local").rglob("*") if path.is_file()]

    def test_it_records_the_reported_window_for_the_transcript(self):
        self.assertIsNone(reported_window(self.transcript))
        self.assertEqual(relay_status_line(self.payload()), b"")
        self.assertEqual(reported_window(self.transcript), 1_000_000)
        self.assertIsNone(reported_window(str(self.root / "other.jsonl")))
        relay_status_line(self.payload(window=200_000))
        self.assertEqual(reported_window(self.transcript), 200_000)

    def test_the_cache_holds_numbers_only(self):
        relay_status_line(self.payload())
        (cached,) = self.cache_files()
        self.assertEqual(
            json.loads(cached.read_text(encoding="utf-8")), {"window": 1_000_000}
        )
        self.assertNotIn("transcript", cached.name)
        self.assertNotIn("session", cached.name)

    def test_it_runs_the_users_own_status_line_with_the_same_input(self):
        self.user_status_line(INNER)
        raw = self.payload()
        self.assertEqual(relay_status_line(raw), f"inner:{len(raw)}".encode())

    def test_a_malformed_payload_records_nothing_and_still_runs_the_users(self):
        self.user_status_line(INNER)
        for raw in (b"not json", b"[1]", self.payload(window=None)):
            with self.subTest(raw=raw[:20]):
                self.assertTrue(relay_status_line(raw).startswith(b"inner:"))
        self.assertIsNone(reported_window(self.transcript))
        self.assertEqual(self.cache_files(), [])

    def test_invalid_windows_are_not_recorded(self):
        for window in (0, -1, True, "1000000", 1.5, 10**10):
            with self.subTest(window=window):
                relay_status_line(self.payload(window=window))
                self.assertIsNone(reported_window(self.transcript))

    def test_a_relative_or_missing_transcript_records_nothing(self):
        for path in ("transcript.jsonl", "", None):
            with self.subTest(path=path):
                relay_status_line(self.payload(transcript_path=path))
                self.assertEqual(self.cache_files(), [])

    def test_the_relay_never_runs_itself(self):
        self.user_status_line(INNER)
        with patch.dict(os.environ, {"AHK_STATUSLINE_RELAY": "1"}):
            self.assertEqual(relay_status_line(self.payload()), b"")
        self.assertIsNone(reported_window(self.transcript))

    def test_a_failing_user_status_line_yields_its_output_and_no_error(self):
        self.user_status_line("exit 3")
        self.assertEqual(relay_status_line(self.payload()), b"")
        self.assertEqual(reported_window(self.transcript), 1_000_000)

    def test_an_unwritable_cache_still_runs_the_users(self):
        self.user_status_line(INNER)
        (self.root / "local").write_text("not a directory", encoding="utf-8")
        self.assertTrue(relay_status_line(self.payload()).startswith(b"inner:"))
        self.assertIsNone(reported_window(self.transcript))

    def test_record_window_none_forgets_it(self):
        record_window(self.transcript, 400_000)
        self.assertEqual(reported_window(self.transcript), 400_000)
        record_window(self.transcript, None)
        self.assertIsNone(reported_window(self.transcript))

    def test_the_runner_serves_it_end_to_end(self):
        self.user_status_line(INNER)
        raw = self.payload()
        result = subprocess.run(
            [sys.executable, str(ROOT / "distribution" / "runner.py"), "statusline"],
            input=raw,
            capture_output=True,
            check=False,
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"inner:{len(raw)}".encode())
        self.assertEqual(reported_window(self.transcript), 1_000_000)

    def test_the_runner_returns_the_status_lines_bytes_exactly(self):
        """Rows keep their own line endings: no carriage return is added."""

        for ending, escaped in ((b"\r\n", "\\r\\n"), (b"\n", "\\n")):
            with self.subTest(ending=ending):
                self.user_status_line(
                    f'"{Path(sys.executable).as_posix()}" -c "import sys; '
                    f"sys.stdout.buffer.write(b'one{escaped}two{escaped}')\""
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "distribution" / "runner.py"),
                        "statusline",
                    ],
                    input=self.payload(),
                    capture_output=True,
                    check=False,
                    cwd=self.root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"one" + ending + b"two" + ending)

    def test_the_claude_fragment_installs_the_relay(self):
        fragment = json.loads(
            (ROOT / "adapters" / "claude" / "settings.fragment.json").read_text(
                encoding="utf-8"
            )
        )
        status_line = fragment["statusLine"]
        self.assertEqual(status_line["type"], "command")
        hook = fragment["hooks"]["Stop"][0]["hooks"][0]["command"]
        locator = hook.split(" hook --platform")[0]
        self.assertEqual(status_line["command"], locator + " statusline")
        # The relay draws whatever it stands in front of, so it adds no
        # padding of its own.
        self.assertEqual(status_line["padding"], 0)


if __name__ == "__main__":
    unittest.main()
