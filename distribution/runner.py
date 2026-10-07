"""Run the vendored agent handoff toolkit from a consumer repository."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
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


# The CLI reads at most this many bytes of hook input, so replaying exactly
# this many keeps every bound it applies unchanged.
_MAX_HOOK_INPUT = 128 * 1024 + 1
# Set on the hand-off so the runner it reaches dispatches instead of looking
# again: one hop, whatever the two runners' payload readings would say.
_HANDOFF_MARKER = "AHK_RUNNER_HANDOFF"


def _checkout(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding `.git`."""

    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _git_common_dir(checkout: Path) -> Path | None:
    """Name the repository a checkout belongs to, without spawning git.

    The same reading as the package's: a `.git` directory is the common
    directory; a `.git` file names a private directory whose `commondir` leads
    back to it. This runner cannot import the package before deciding which
    copy of it to load, so the reading is repeated here. Any error raises and
    the caller hands nothing off.
    """

    marker = checkout / ".git"
    if marker.is_dir():
        return marker.resolve()
    with marker.open("rb") as source:
        text = source.read(4097)
    if len(text) > 4096 or not text.startswith(b"gitdir:"):
        return None
    private = Path(text[len(b"gitdir:") :].decode("utf-8").strip())
    if not private.is_absolute():
        private = checkout / private
    private = private.resolve()
    pointer = private / "commondir"
    if not pointer.is_file():
        return private
    with pointer.open("rb") as source:
        text = source.read(4097)
    if len(text) > 4096:
        return None
    common = Path(text.decode("utf-8").strip())
    return (common if common.is_absolute() else private / common).resolve()


def _session_runner(data: bytes) -> Path | None:
    """Name the runner pinned by the checkout the payload's `cwd` is in.

    The managed command locates this runner by walking up from the hook
    process's directory, and the host does not promise that is the session's.
    A desktop worktree sits inside the main checkout, so the walk can land on
    the main checkout's runner while the session works in the worktree. The
    payload's `cwd` is the session's directory, and the checkout holding it
    names the runner: `<that checkout>/.agent-handoff-toolkit/runner.py`, and
    only when that checkout is a worktree of the same repository as this
    runner's - the same git common directory. A session can stand in any
    clone on the machine, and running that clone's runner would execute code
    this repository never pinned. `None` means there is nothing to hand to.
    """

    payload = json.loads(data.decode("utf-8"))
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return None
    start = Path(cwd)
    if not start.is_absolute() or not start.is_dir():
        return None
    checkout = _checkout(start.resolve())
    own = _checkout(Path(__file__).resolve().parent)
    if checkout is None or own is None or checkout == own:
        return None
    candidate = checkout / ".agent-handoff-toolkit" / "runner.py"
    if not candidate.is_file() or os.path.samefile(candidate, __file__):
        return None
    common = _git_common_dir(checkout)
    if common is None or common != _git_common_dir(own):
        return None
    return candidate


def _hand_off(arguments: list[str]) -> bool:
    """Run a hook through the session checkout's runner, if it has another.

    Returns True when that runner answered and its answer has been published.
    Anything else - no other runner, a guarded second hop, a runner that
    raised, exited non-zero, or answered with something other than one
    response - publishes nothing and returns False, so this runner serves the
    hook as before. Either way the stdin bytes read here are put back for
    whoever dispatches next.
    """

    if not arguments or arguments[0] != "hook":
        return False
    source = getattr(sys.stdin, "buffer", None)
    if source is None:
        return False
    try:
        data = source.read(_MAX_HOOK_INPUT)
    except (OSError, ValueError):
        # Unreadable input names no other checkout. Nothing was consumed, and
        # the CLI's own read reports the fault under its failure policy.
        return False

    def replay() -> None:
        sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")

    replay()
    if os.environ.get(_HANDOFF_MARKER):
        return False
    try:
        target = _session_runner(data)
    except Exception:
        # An unreadable payload names no other checkout. The CLI reads the
        # same bytes and reports what is wrong with them.
        return False
    if target is None:
        return False

    saved_argv, saved_path = sys.argv, list(sys.path)
    answer = _HookBuffer()
    answered = False
    try:
        os.environ[_HANDOFF_MARKER] = "1"
        sys.argv = [str(target), *arguments]
        with (
            contextlib.redirect_stdout(answer),
            contextlib.redirect_stderr(_HookBuffer()),
        ):
            try:
                runpy.run_path(str(target), run_name="__main__")
                answered = True
            except SystemExit as exit_:
                answered = exit_.code in (0, None)
    except BaseException:
        answered = False
    finally:
        os.environ.pop(_HANDOFF_MARKER, None)
        sys.argv = saved_argv
        sys.path[:] = saved_path
        # The other runner imported its own copy of the package. Dropping it
        # keeps a fall-through from running that copy as this runner's.
        for name in [
            name
            for name in sys.modules
            if name == "agent_handoff_toolkit"
            or name.startswith("agent_handoff_toolkit.")
        ]:
            del sys.modules[name]
        replay()

    text = answer.getvalue()
    if not answered or answer.overflowed or (text and not _one_response(text)):
        return False
    _publish(text)
    return True


def main() -> int:
    """Load the package adjacent to this runner and dispatch its CLI."""
    # Before anything is imported: the runner handed to loads its own package.
    if _hand_off(sys.argv[1:]):
        return 0
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
