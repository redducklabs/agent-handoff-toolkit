"""Run the vendored agent handoff toolkit from a consumer repository."""

from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    """Load the package adjacent to this runner and dispatch its CLI."""
    source_root = Path(__file__).resolve().parent / "src"
    sys.path.insert(0, str(source_root))

    from agent_handoff_toolkit.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
