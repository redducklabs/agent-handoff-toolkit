"""Command-line interface for explicit record operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Sequence

from .hooks import observe_context, run_hook
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

    hook = subparsers.add_parser("hook", help="run a fail-open host hook")
    hook.add_argument("--platform", choices=("claude", "codex"), required=True)
    hook.add_argument("--event", required=True)

    context = subparsers.add_parser(
        "context-health", help="record an explicit context percentage"
    )
    context.add_argument("--percent", type=float, required=True)
    context.add_argument("--session-id", required=True)
    context.add_argument(
        "--state-dir",
        type=Path,
        default=Path(tempfile.gettempdir())
        / "agent-handoff-toolkit"
        / "context-health",
    )

    for command in ("install", "sync"):
        managed = subparsers.add_parser(
            command, help=f"{command} managed toolkit artifacts"
        )
        managed.add_argument("--target", type=Path, required=True)
        managed.add_argument("--source-root", type=Path)
        managed.add_argument("--release", required=True)
        mode = managed.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--dry-run" if command == "install" else "--check",
            action="store_true",
        )
        mode.add_argument("--apply", action="store_true")
    return parser


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _repository_root(start: Path) -> Path:
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return resolved


def _source_root() -> Path:
    """Infer the checkout or installed toolkit root from this module's path."""

    return Path(__file__).resolve().parents[2]


def _configure_output_encoding() -> None:
    """Use deterministic UTF-8 for command output when the host supports it."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def main(argv: Sequence[str] | None = None) -> int:
    """Run explicit fail-closed commands or the automatic fail-open hook."""

    _configure_output_encoding()
    args = _parser().parse_args(argv)

    if args.command == "hook":
        try:
            output = run_hook(
                args.platform,
                args.event,
                sys.stdin.read(),
                _repository_root(Path.cwd()),
            )
            if output:
                sys.stdout.write(output + "\n")
                sys.stdout.flush()
        except Exception:
            return 0
        return 0

    if args.command in {"install", "sync"}:
        try:
            # The installed hook runtime deliberately omits installer-only modules.
            try:
                from .installer import apply_plan, build_plan, render_plan
            except ModuleNotFoundError as error:
                if error.name != f"{__package__}.installer":
                    raise
                print(
                    "error: install and sync must be run from an "
                    "agent-handoff-toolkit release checkout using "
                    "distribution/runner.py",
                    file=sys.stderr,
                )
                return 2

            plan = build_plan(
                args.source_root if args.source_root is not None else _source_root(),
                args.target,
                args.release,
                args.command,
            )
            sys.stdout.write(render_plan(plan))
            if plan.conflicts:
                return 2
            if getattr(args, "dry_run", False) or getattr(args, "check", False):
                return 1 if args.command == "sync" and plan.changes else 0
            apply_plan(plan)
            return 0
        except Exception as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    try:
        if args.command == "context-health":
            output = observe_context(
                args.session_id,
                args.percent,
                args.state_dir,
            )
            if output:
                print(output)
            return 0

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
