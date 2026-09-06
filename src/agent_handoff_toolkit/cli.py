"""Command-line interface for explicit record operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .records import render_record, render_tail, validate_markdown


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff-toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a Markdown record")
    validate.add_argument("record", type=Path)

    render = subparsers.add_parser("render", help="render JSON data as Markdown")
    render.add_argument("record", type=Path)
    render.add_argument("--output", type=Path)

    tail = subparsers.add_parser("render-tail", help="render a final response tail")
    tail.add_argument("record", type=Path)
    return parser


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicit, fail-closed command."""

    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            issues = validate_markdown(_read_text(args.record))
            if issues:
                for issue in issues:
                    print(f"{issue.code}: {issue.message}", file=sys.stderr)
                return 1
            print(f"valid: {args.record}")
            return 0

        if args.command == "render":
            data = json.loads(_read_text(args.record))
            text = render_record(data)
            if args.output is None:
                sys.stdout.write(text)
            else:
                args.output.write_text(text, encoding="utf-8", newline="\n")
                print(f"rendered: {args.output}")
            return 0

        text = _read_text(args.record)
        sys.stdout.write(render_tail(args.record, text) + "\n")
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
