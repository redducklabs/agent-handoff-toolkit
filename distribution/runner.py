"""Run the vendored agent handoff toolkit from a consumer repository."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys


_GATED_EVENTS = ("user-prompt-submit", "pre-tool-use", "stop")
# Every other event the managed adapters install. They decide nothing, so
# they are silent and exit 0 - but they have to be recognised here, or a
# malformed invocation of one falls through to a non-zero exit.
_QUIET_EVENTS = ("session-start", "session-end", "post-tool-use")


def _compact(value: str) -> str:
    """Fold an event spelling the way the CLI's own alias lookup does."""

    return value.strip().lower().replace("-", "").replace("_", "")


_CANONICAL_EVENTS = {_compact(event): event for event in _GATED_EVENTS + _QUIET_EVENTS}


def _gated_hook_event(arguments: list[str]) -> str | None:
    """Name the hook event this invocation is for, however malformed it is.

    Deliberately looser than the CLI's own parsing. A managed command that
    has been corrupted - an extra argument, a reordering - is still a hook
    invocation, and the host still reads its exit status as a decision. An
    exact-shape check returned `None` for those, argparse then exited 2, and
    a gated event read that as a block: the very wedge this boundary exists
    to prevent, reachable by a typo in a settings file.
    """

    if not arguments or arguments[0] != "hook":
        return None
    found = None
    for index, argument in enumerate(arguments):
        # Both spellings argparse accepts, and the same normalisation the CLI
        # applies, so this classifier cannot disagree with the parser about
        # which event an invocation is for. It takes the last occurrence for
        # the same reason: that is the one argparse would use.
        if argument.startswith("--event="):
            raw = argument[len("--event=") :]
        elif argument == "--event" and index + 1 < len(arguments):
            raw = arguments[index + 1]
        else:
            continue
        event = _CANONICAL_EVENTS.get(_compact(raw))
        if event is not None:
            found = event
    return found


def _bootstrap_hook_failure(
    arguments: list[str], stage: str, error: BaseException | None = None
) -> str | None:
    """Classify a bounded hook invocation the installed runtime could not serve.

    `stage` names which boundary is reporting: the import that never
    produced a runtime, or a dispatch that failed after one loaded. Both
    reach here, and calling a dispatch failure an import failure would send
    a consumer looking for a broken install that is not broken.
    """

    event = _gated_hook_event(arguments)
    if event is None:
        return None
    if event in _QUIET_EVENTS:
        return ""
    # The installed runtime could not be imported at all, so nothing here can
    # tell whether the session declared tracked work. That is install
    # corruption rather than a transient fault, and it keeps failing closed -
    # but it still names the stage, so a consumer can tell it apart from a
    # lifecycle decision.
    details = "stage:" + stage
    name = type(error).__name__ if error is not None else ""
    # A fixed identifier from the interpreter: bounded, and never host content.
    if name.isascii() and name.isidentifier() and len(name) <= 64:
        details += ",error:" + name.replace("_", "-")
    reason = f"AHK-HOOK-RUNTIME: Repair lifecycle runtime and retry. failed={details}"
    if event == "pre-tool-use":
        value = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    elif event == "user-prompt-submit":
        # Work still fails closed here, but the user's own message never does.
        # Blocking it erases what they typed and starts no turn, so nobody is
        # left who could act on this reason - and a corrupt runtime is exactly
        # the state in which someone has to be able to talk to the session to
        # repair it. This duplicates the package renderer deliberately: it runs
        # only when no package module can be imported at all.
        value = {
            "systemMessage": (
                "Agent handoff toolkit reported AHK-HOOK-RUNTIME; this prompt "
                "was delivered and the session continues."
            ),
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": reason,
            },
        }
    else:
        value = {"decision": "block", "reason": reason}
    return json.dumps(value, separators=(",", ":"))


_MAX_CAPTURE = 1 << 20


class _HookBuffer(io.StringIO):
    """Collect one hook response, accepting the stream setup the CLI applies.

    The CLI reconfigures the stream it is handed to UTF-8, which matters on a
    Windows console defaulting to cp1252. Handing it a plain `StringIO` made
    that call silently skip, and the real stream then raised
    `UnicodeEncodeError` on the first non-ASCII character in a record.

    Capture is bounded. A hook response is one small JSON object, so anything
    past the cap is a malfunction, and accumulating it would trade a reported
    fault for an exhausted process.
    """

    overflowed = False

    def reconfigure(self, **_: object) -> None:
        return None

    def write(self, text: str) -> int:
        # The remaining capacity, not the size so far: checking only the
        # latter stored a single oversized write in full and left the
        # overflow unflagged, which is the case the cap exists for.
        room = _MAX_CAPTURE - self.tell()
        if len(text) >= room:
            self.overflowed = True
            if room > 0:
                super().write(text[:room])
            return len(text)
        return super().write(text)


def _one_response(text: str) -> bool:
    """True when this is exactly one JSON object, which is what a hook owes."""

    try:
        return isinstance(json.loads(text), dict)
    except Exception:
        return False


def _publish(text: str) -> None:
    """Write one response to the real stream, as the CLI would have."""

    if not text:
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.write(text)
    sys.stdout.flush()


def main() -> int:
    """Load the package adjacent to this runner and dispatch its CLI."""
    source_root = Path(__file__).resolve().parent / "src"
    if not source_root.is_dir():
        source_root = Path(__file__).resolve().parents[1] / "src"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source_root))

    # Classified before anything can fail, because the answer decides what an
    # exit status and a stream are allowed to carry. A hook command must never
    # exit 2: every gated host reads that as a deliberate block, so a usage
    # error, an internal status, or an exit raised while importing would be
    # indistinguishable from a denial.
    hook = _bootstrap_hook_failure(sys.argv[1:], "dispatch-runtime")

    # Both streams are captured for a hook, and the import happens inside the
    # capture: corrupt code that prints while being imported would otherwise
    # put bytes on the real stream before this boundary could say anything,
    # and the notice would land beside them as a second response.
    buffer = _HookBuffer()
    noise = _HookBuffer()
    stage = None
    failure: BaseException | None = None
    status = 0
    with contextlib.ExitStack() as capture:
        capture.enter_context(contextlib.redirect_stdout(buffer))
        if hook is not None:
            capture.enter_context(contextlib.redirect_stderr(noise))
        try:
            from agent_handoff_toolkit.cli import main as cli_main
        except BaseException as error:  # SystemExit during import included
            stage, failure = "import-runtime", error
        else:
            # Anything an import printed on its way to succeeding is not part
            # of the response, and leaving it in front of the dispatch's JSON
            # would hand the host two things where it expects one.
            buffer.seek(0)
            buffer.truncate(0)
            try:
                status = cli_main()
            except BaseException as error:
                stage, failure = "dispatch-runtime", error

    if stage is not None:
        if hook is None:
            # Not a hook, so nothing here is a decision. An unusable install
            # says so plainly and exits non-zero, which is what an explicit
            # command should do; anything raised by a runtime that did load
            # belongs to the caller and propagates.
            _publish(buffer.getvalue())
            if stage == "import-runtime":
                sys.stderr.write("error: installed runtime unavailable\n")
                return 2
            raise failure
        # One rule for every abnormal exit of a gated hook, however it is
        # spelled: discard whatever was half-written and publish exactly one
        # structured response.
        _publish(_bootstrap_hook_failure(sys.argv[1:], stage, failure) or "")
        return 0

    if hook is None:
        _publish(buffer.getvalue())
        return status

    # One invocation owes the host exactly one response, so what is about to
    # be published has to be that and nothing else: not silence standing in
    # for a denial, not a half-written object, not output that outgrew the
    # capture. Anything else is replaced by the structured notice.
    published = buffer.getvalue()
    sound = not buffer.overflowed and (
        _one_response(published) if published else not status
    )
    if not sound:
        published = _bootstrap_hook_failure(sys.argv[1:], "dispatch-runtime") or ""
    _publish(published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
