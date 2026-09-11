"""Command-line interface for explicit record operations."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence

from .acceptance import AcceptancePrerequisiteError, format_result, run_acceptance
from .hooks import MAX_HOOK_INPUT_BYTES, observe_context, run_hook
from .records import render_record, render_tail, validate_markdown


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff-toolkit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "lifecycle", help="inspect and control authorization lifecycle"
    )

    validate = subparsers.add_parser("validate", help="validate a Markdown record")
    validate.add_argument("record", type=Path)

    render = subparsers.add_parser("render", help="render JSON data as Markdown")
    render.add_argument("record", type=Path)
    render.add_argument("--output", type=Path)

    tail = subparsers.add_parser("render-tail", help="render a final response tail")
    tail.add_argument("record", type=Path)

    hook = subparsers.add_parser("hook", help="run a host lifecycle or advisory hook")
    hook.add_argument("--platform", choices=("claude", "codex"), required=True)
    hook.add_argument("--event", required=True)

    acceptance = subparsers.add_parser(
        "acceptance", help="run an opt-in, content-redacting host smoke check"
    )
    acceptance.add_argument("--platform", choices=("claude", "codex"), required=True)
    acceptance.add_argument("--scratch", type=Path, required=True)
    acceptance.add_argument("--release-source", type=Path)

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


class _LifecycleParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        # argparse diagnostics include rejected argument values; never expose them.
        raise ValueError("invalid lifecycle arguments; use lifecycle --help")


def _lifecycle_parser():
    parser = _LifecycleParser(prog="handoff-toolkit lifecycle")
    commands = parser.add_subparsers(dest="operation", required=True)
    inspection = commands.add_parser(
        "inspect", help="print bounded lifecycle IDs and status"
    )
    inspection.add_argument("--session-key", required=True)
    inspection.add_argument("--challenge", required=True)
    inspection.add_argument("--expected-session-revision", type=int, required=True)
    register = commands.add_parser(
        "register-root", help="consume the live turn challenge and register a root"
    )
    register.add_argument("--scope-id", required=True)
    register.add_argument("--scope-kind", required=True)
    register.add_argument("--scope-definition-b64", required=True)
    resume = commands.add_parser(
        "resume", help="resume one explicit absolute v2 continuation"
    )
    resume.add_argument("--record", required=True)
    join = commands.add_parser("join", help="join one active authorization chain")
    join.add_argument("--authorization-id", required=True)
    adopt = commands.add_parser(
        "adopt-v1", help="adopt the selected v1 record after trusted adjacent approval"
    )
    adopt.add_argument("--record", required=True)
    for command in (register, resume, join, adopt):
        command.add_argument("--session-key", required=True)
        command.add_argument("--challenge", required=True)
        command.add_argument("--expected-session-revision", type=int, required=True)
    join.add_argument("--expected-chain-revision", type=int, required=True)
    proposal = commands.add_parser(
        "propose-transition", help="register an immutable proposal without approving it"
    )
    proposal.add_argument("--old-scopes-b64", required=True)
    proposal.add_argument("--new-scopes-b64", required=True)
    proposal.add_argument("--assistant-turn-reference", required=True)
    proposal.add_argument(
        "--kind", choices=("transition", "v1-adoption"), default="transition"
    )
    proposal.add_argument("--record")
    proposal.add_argument("--record-id")
    proposal.add_argument("--record-sha256")
    decision = commands.add_parser(
        "request-decision",
        help="render a decision using only the model-authored question on stdin",
    )
    decision.add_argument("--category", required=True)
    decision.add_argument("--blocked-action-field", required=True)
    decision.add_argument("--blocked-action-value", required=True)
    decision.add_argument("--reason", required=True)
    for command in (proposal, decision):
        command.add_argument("--session-key", required=True)
        command.add_argument("--challenge", required=True)
        command.add_argument("--expected-chain-revision", type=int, required=True)
        command.add_argument("--expected-session-revision", type=int, required=True)
    return parser


def _lifecycle_main(argv):
    # Imports remain local so legacy informational hooks retain their behavior.
    from .lifecycle import RecordReference
    from .lifecycle_operations import (
        LifecycleOperationError,
        LifecycleService,
        canonical_record_path,
    )
    from .lifecycle_storage import (
        LifecycleStorageError,
        LocalLifecycleStorage,
        StaleLifecycleState,
    )
    from .lineage import canonical_json_bytes, record_digest
    from .records import parse_markdown

    try:
        if len(argv) > 40 or any(len(value.encode("utf-8")) > 16384 for value in argv):
            raise ValueError("lifecycle arguments exceed bounds")
        flags = [value.split("=", 1)[0] for value in argv if value.startswith("--")]
        if len(flags) != len(set(flags)):
            raise ValueError("duplicate lifecycle flag")
        args = vars(_lifecycle_parser().parse_args(argv))
        operation = args.pop("operation")
        session_key = args.pop("session_key")
        capability = args["challenge"]
        state_root = os.environ.get("AHK_STATE_ROOT")
        owned_root = _source_root()
        storage = LocalLifecycleStorage(
            owned_root.parent
            if owned_root.name == ".agent-handoff-toolkit"
            else _repository_root(Path.cwd()),
            state_root=Path(state_root) if state_root else None,
        )
        service = LifecycleService.for_control(
            storage, session_key, capability, args["expected_session_revision"]
        )
        if operation not in {"register-root", "resume", "join", "adopt-v1"}:
            args.pop("challenge")
        if operation == "inspect":
            service.consume_control()
            result = service.inspect()
        elif operation == "request-decision":
            reconfigure = getattr(sys.stdin, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="strict")
            response = service.request_decision(question=sys.stdin.read(401), **args)
            sys.stdout.write(response + "\n")
            return 0
        else:
            if operation in {"resume", "adopt-v1"}:
                path = canonical_record_path(args.pop("record"))
                if operation == "adopt-v1":
                    pending = (
                        service._load_snapshot().session.pending_transition_reference
                    )
                    if (
                        pending is None
                        or pending.kind != "v1-adoption"
                        or pending.status != "approved"
                        or pending.selected_record.path != path
                    ):
                        raise ValueError("selected adoption record is not approved")
                with Path(path).open("r", encoding="utf-8") as source:
                    text = source.read(131073)
                args.update(record_path=path, record_text=text)
                if operation == "resume":
                    args.update(
                        record_metadata=parse_markdown(text),
                        record_digest=record_digest(text),
                    )
            if operation == "propose-transition":
                for name in ("old_scopes", "new_scopes"):
                    value = args.pop(name + "_b64")
                    raw = base64.b64decode(
                        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
                    )
                    scopes = json.loads(raw.decode("utf-8"))
                    if (
                        canonical_json_bytes(scopes) != raw
                        or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value
                    ):
                        raise ValueError("proposal scope JSON must be canonical")
                    args[name] = scopes
                record = args.pop("record")
                record_id = args.pop("record_id")
                digest = args.pop("record_sha256")
                if args["kind"] == "v1-adoption":
                    args["selected_record"] = RecordReference(
                        record_id, canonical_record_path(record), digest
                    )
                elif any(item is not None for item in (record, record_id, digest)):
                    raise ValueError("transition cannot select a v1 record")
            updated = getattr(service, operation.replace("-", "_"))(**args)
            result = service.inspect()
            if operation == "register-root":
                result["authorization_evidence"] = {
                    "kind": "initial-user-turn",
                    "user_turn_ref": updated.chain.authorization_user_turn_reference,
                    "proposal_turn_ref": None,
                    "evidence_hmac": updated.chain.authorization_evidence_hmac,
                }
            if (
                updated.chain is not None
                and updated.chain.publication_evidence is not None
            ):
                proof = updated.chain.publication_evidence
                result["publication_evidence"] = {
                    "authorization_evidence": {
                        "kind": "approved-transition",
                        "user_turn_ref": proof.approval_turn_reference,
                        "proposal_turn_ref": proof.assistant_turn_reference,
                        "evidence_hmac": proof.evidence_hmac,
                    },
                    "transition": {
                        "from_authorization_id": proof.from_authorization_id,
                        "to_authorization_id": proof.to_authorization_id,
                        "old_root_scope_id": proof.old_scopes[0]["scope_id"],
                        "new_root_scope_id": proof.new_scopes[0]["scope_id"],
                        "proposal_turn_ref": proof.assistant_turn_reference,
                        "approval_turn_ref": proof.approval_turn_reference,
                        "evidence_hmac": proof.evidence_hmac,
                    },
                }
        result["session_key"] = session_key
        result["challenge"] = service._control[1]
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except LifecycleOperationError as error:
        print(
            json.dumps(
                {
                    "issue_codes": [error.code],
                    "corrective_action": error.corrective_action,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except StaleLifecycleState:
        print('{"issue_codes":["AHK-STATE-STALE"]}', file=sys.stderr)
        return 1
    except (LifecycleStorageError, OSError):
        print('{"issue_codes":["AHK-RUNTIME"]}', file=sys.stderr)
        return 2
    except (ValueError, TypeError, KeyError, UnicodeError):
        print('{"issue_codes":["AHK-INPUT"]}', file=sys.stderr)
        return 1
    except Exception:
        print('{"issue_codes":["AHK-RUNTIME"]}', file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run explicit commands and preserve each host hook's failure policy."""

    _configure_output_encoding()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] == "lifecycle":
        return _lifecycle_main(arguments[1:])
    args = _parser().parse_args(argv)

    if args.command == "hook":
        try:
            output = run_hook(
                args.platform,
                args.event,
                sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1),
                _repository_root(Path.cwd()),
            )
            sys.stdout.write(output.stdout)
            sys.stderr.write(output.stderr)
            sys.stdout.flush()
            sys.stderr.flush()
            return output.exit_code
        except Exception:
            event = args.event.lower().replace("-", "").replace("_", "")
            if event in {"stop", "pretooluse", "userpromptsubmit"}:
                sys.stderr.write(
                    "AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry."
                )
                return 2
            return 0

    if args.command == "acceptance":
        try:
            result = run_acceptance(
                args.platform,
                scratch=args.scratch,
                source_root=args.release_source,
            )
        except AcceptancePrerequisiteError:
            sys.stderr.write("error: acceptance release source unavailable\n")
            return 2
        sys.stdout.write(format_result(result) + "\n")
        return result.exit_code

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
            issues = validate_markdown(_read_text(args.record), record_path=args.record)
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
