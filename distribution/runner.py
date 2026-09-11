"""Run the vendored agent handoff toolkit from a consumer repository."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def _bootstrap_hook_failure(arguments: list[str]) -> str | None:
    """Classify a bounded hook invocation when the installed runtime cannot import."""

    if len(arguments) != 5 or arguments[0] != "hook":
        return None
    if arguments[1] != "--platform" or arguments[3] != "--event":
        return None
    platform, event = arguments[2], arguments[4]
    if platform not in {"claude", "codex"}:
        return None
    if event in {"session-start", "post-tool-use"}:
        return ""
    if event not in {"user-prompt-submit", "pre-tool-use", "stop"}:
        return None
    reason = "AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry."
    if event == "pre-tool-use":
        value = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    else:
        value = {"decision": "block", "reason": reason}
    return json.dumps(value, separators=(",", ":"))


def main() -> int:
    """Load the package adjacent to this runner and dispatch its CLI."""
    source_root = Path(__file__).resolve().parent / "src"
    if not source_root.is_dir():
        source_root = Path(__file__).resolve().parents[1] / "src"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source_root))

    try:
        from agent_handoff_toolkit.cli import main as cli_main
    except Exception:
        fallback = _bootstrap_hook_failure(sys.argv[1:])
        if fallback is not None:
            sys.stdout.write(fallback)
            return 0
        sys.stderr.write("error: installed runtime unavailable\n")
        return 2

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
