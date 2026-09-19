# Agent Handoff Toolkit

Portable session-continuation and completion-audit standards for Claude Code and Codex.

This repository provides a shared contract, deterministic validation and rendering, repo-local agent instructions, and thin host adapters. It is designed to replace handoff policies that drift between projects while preserving project-specific instructions.

## Status

Version 1.0.1 fixes two faults that could stop the toolkit running on a host
without reporting anything. `_LOCK_CONTENTION_ERRNOS` named `errno.EDEADLOCK`
directly, which is a Linux and Windows alias macOS does not define, so
importing `lifecycle_storage` raised `AttributeError` and every module that
reaches it failed at import: on macOS the installed runner could not start and
the test suite stopped at collection. Both spellings are now resolved by name.
A test that deletes the alias before importing the module reproduces the
failure on the Linux CI runners, so the regression is covered where it can be
run.

Hook commands run `python`. Where that name is missing, as on stock macOS,
Debian and Ubuntu, or is the Windows Store alias stub, every hook failed
non-blockingly and the lifecycle went dead with nothing visible to say so.
`install` and `sync` now resolve the manifest's `python_command` on `PATH`, run
the resolved program with `--version`, and raise a `launcher-unavailable`
conflict when it is absent, cannot start, exits non-zero, reports no version, or
is below the manifest minimum. The check lives in the CLI, not `build_plan`, so
a plan still does not depend on the machine computing it. The hook command
strings are unchanged, the record schema and lifecycle state format are
unchanged, and the installed files stay byte-identical across operating systems.

Version 1.0.0 removes what the 2026-09-17 review found nobody was using. The
record schema has one version: schema v1 was kept parseable so an existing
chain could be adopted into v2, and across the three consumers no chain ever
was. The comment metadata form, the four sections the renderer derived
byte-for-byte from metadata, the v1 response tail, `adopt-v1` and the
`v1-adoption` evidence kind are gone. A record written before this schema is
unaffected: it was already historical by policy, never opened, validated or
migrated.

The `context-health` command is gone with them. Nothing installed or called it,
and no host exposes a context percentage to a hook, so its 50/60/70 per cent
reminders could never fire.

The distributed mechanics reference is down from 4,950 words to about 2,750.
The reasoning behind each rule, and the failures each was written against, moved
to `docs/design/mechanics-rationale.md` in this repository and is no longer
installed into a consumer: a session diagnosing a block needs the rule, not its
history. The transition mechanism moved with it — it stays in the code, and 0 of
the 39 records across the three consumers carry one.

Host verification, observed rather than assumed: on Claude Code an acceptance
run reports `discovered=pass` and `advisory_seen=pass`, which confirms the model
receives `hookSpecificOutput.additionalContext` at `PreToolUse` and so receives
`AHK-DECLARE`. Codex remains **unverified**: the account had no usage credits
when this was cut, so the host could not start a session and nothing could be
observed.

Version 0.6.0 acts on the 2026-09-17 goal-alignment and token review. The
toolkit had become a net token cost: a 737-word block was loaded into every
session of every consumer, and a tracked session had to author a full
continuation at every turn end, because `Stop` runs when the agent stops
talking and not when the session ends. The managed block is now 239 words, the
SessionStart reminder is one sentence, the first-write advisory is under 400
bytes and is delivered where the model can see it, and a tracked turn that is
merely unfinished may end on one canonical progress line instead of a record.
A session that ends on that line is reported at `SessionEnd`.

Three ways a session silently lost its goal are closed. A pasted handoff now
resumes on its first line with the record's digest as the evidence, and a
failed resume is reported instead of leaving the session quietly untracked. A
user can declare the goal themselves: a prompt whose first line is
`Track: <goal>` registers it as the tracked root, bound to the turn that asked
for it. Records chain by digest rather than by absolute path, so a chain
survives a worktree or a WSL path form.

The responses now read as English addressed to a person: a continuation names
the goal and the progress, an audit says what was completed and tallies its
checks, and neither is a bare link or a line of jargon. The validator enforces
the record budgets the contract already stated, `render --successor-of` copies
the lineage an author never chooses, and a newly tracked session is handed its
bound `inspect` command instead of buying it with a denied tool call.

Ordinary tool calls no longer open lifecycle state, `NotebookEdit` is matched,
and state written by one installed release now loads in another. That last one
mattered at the time: every worktree of a repository shares one lifecycle
registry, and 0.5.0 rejects any field it does not know, so a worktree still
pinned to it could not read state a 0.6.0 runner had written. From 0.6.0 onward
the hazard cannot recur, and re-pinning any repository still on 0.5.0 means
re-syncing every live worktree of it.

Version 0.5.0 stops the toolkit presenting its own malfunctions as policy
decisions. Managed hook commands now locate the runner by walking up from the
working directory, so a session working in a subdirectory is no longer denied
every tool it would need to recover; the Windows lock waits for a contended
region instead of giving up; and a runtime fault blocks only a session that
declared tracked work, naming its failing stage and exception either way. The
read-only `lifecycle doctor` command is the supported recovery entry point when
the hook flow itself is broken.

Version 0.4.0 narrows enforcement to declared tracked work. A session runs
ungated until it registers a root; the first repository write after that says
so once; and a session that ends with unfinished work is reported rather than
prevented. The guarantee is now about declared work: the repository does not
claim more than its mechanisms support.

Version 0.3.3 stops the hook rejecting the work's own content. A tool call's
input is the host's payload: a tracked session could not write a file over about
4 KB, could not write a tab at all — so no Go source and no Makefile — and could
not accept a pasted log over 16 KB, because `UserPromptSubmit` rejected the
user's own message. Hook input is now bounded once, as a whole; what the
lifecycle parses is still validated where it is read, and no host content
reaches hook feedback, which is why inspecting the rest proved nothing.

Version 0.3.2 lets a session that has not yet registered a root bootstrap one,
see why an attempt was rejected, and reach a legal terminal outcome. The bound
`register-root` command accepts the scope title and outcome as plain text and
returns itself fully encoded, because before a root exists there is no shell
with which to encode one. A rejected attempt names the check that failed after
`failed=`, rather than returning one string for every cause. `Stop` treats the
host's report of the turn's final text as turn content, so a turn that ended on
a tool call no longer fails the hook, and a blocked `Stop` always advances the
correction count so the circuit can arm without a registered chain.

Version 0.3.1 repaired the opt-in host smoke check. It lends the host the
operator's existing credential for the run, allows a real multi-turn session to
finish, and emits hook commands the host's POSIX shell can actually execute.
Each of those previously reported as undiscovered hooks rather than as the
missing prerequisite, the timeout, or the shell-quoting failure it actually was.

Version 0.3.0 added synchronous lifecycle enforcement for tracked sessions and
directs the user to resume in a new session from the handoff. Schema v2 also
stores every structured fact exactly once, in a visible metadata block, removing
the narrative sections that schema v1 derived from that same metadata. It
preserves release-pinned installation, consumer-owned instructions, and opaque
deprecated historical records for both Claude Code and Codex. No-action
continuation fields are rejected even when prefixed as ordered or unordered list
items.

## Core rules

- A **continuation** exists only when authorized work remains and the next session has a concrete, executable first action.
- A **completion audit** is mandatory when the highest authorized epic, feature, or standalone outcome is complete. It is evidence, never a continuation point.
- Every active scope explicitly states whether code remains. A child-level “no” never implies that its parent is complete.
- Questions that can change the next session's first action are answered before a continuation is finalized.
- Recorded repository and tracker state is a timestamped snapshot. A resumed session reconciles live state before acting.
- Verification results are classified as `pass`, `fail`, or `not-run`; skipped checks are never reported as passing. Record one entry per gate the next session would rerun, not one per invocation.
- A record stores each fact once, in a visible metadata block at the top of the document. No narrative section restates verification, the exact next action, the scope list, or the next-session prompt.
- A continuation response says `Stopping here. Work remains on "<root title>"`
  and `What you need to do: start a new session and paste the block below`. Its
  copy/paste block for the new session begins with `Continue
  from handoff`, identifies the exact next action, and lists only essential blockers,
  decisions, and validation gates. Its stored context is limited to 120 words
  and the complete generated tail to 300 words. The response must not reproduce
  the handoff document. The absolute clickable link remains the final line.
- A tracked session may also end a turn on the canonical progress line instead
  of authoring a record. `lifecycle inspect` publishes that line as
  `progress_response`, and a session that ends on one is reported at
  `SessionEnd`.

See [the contract](docs/agent-handoff/contract.md) for what an author must write, and [the mechanics reference](docs/agent-handoff/mechanics.md) for the exact formats and enforcement the toolkit applies.

## Command surface

```text
handoff-toolkit validate <record.md>
handoff-toolkit render <record.json> --output <record.md>
handoff-toolkit render <record.json> --successor-of <predecessor.md> --output <record.md>
handoff-toolkit render-tail <continuation.md>
handoff-toolkit hook --platform claude|codex --event post-tool-use
handoff-toolkit hook --platform claude|codex --event session-end
handoff-toolkit acceptance --platform claude|codex --scratch <empty-scratch-directory>
```

Advisory automatic hooks fail open so they cannot break an agent session.
Tracked lifecycle hooks (`UserPromptSubmit`, `PreToolUse`, and `Stop`) fail closed.
Explicit commands fail closed and return a non-zero exit code for invalid input.

`acceptance` is an opt-in local host smoke check. It creates and removes the
named empty scratch repository, keeps host output only in memory, and prints
only reduced platform properties and lifecycle issue codes. It redirects the
host's configuration home into that scratch directory, so it lends the host the
operator's existing credential for the run and removes that copy as soon as the
host exits; without it the host cannot start a session and the run reports
undiscovered hooks instead of the missing prerequisite. It is not part of
ordinary public CI. A host without observed trusted hook discovery is reported
as `unverified`, not `pass`. When invoking the command from an installed
consumer runtime, supply `--release-source <pinned-release-checkout>` so the
disposable consumer can be installed from a verified local release source.

## Consumer installation

Check out the public v1.0.1 release, inspect the proposed changes, then apply
them from that checkout:

```powershell
git clone --branch v1.0.1 --depth 1 https://github.com/redducklabs/agent-handoff-toolkit.git agent-handoff-toolkit
Set-Location agent-handoff-toolkit
python distribution/runner.py install --target <consumer-repository> --release v1.0.1 --dry-run
python distribution/runner.py install --target <consumer-repository> --release v1.0.1 --apply
python distribution/runner.py sync --target <consumer-repository> --release v1.0.1 --check
python distribution/runner.py sync --target <consumer-repository> --release v1.0.1 --apply
```

Run install and sync only in an isolated, clean Git worktree with no concurrent
writers. Recheck the worktree after any failed apply: replacement is atomic per
file, while multi-file rollback is best effort and preserves bytes that no
longer match the output written by that run.

`install --dry-run` and a current `sync --check` exit `0`; `sync --check`
exits `1` when safe updates are available; and `2` reports invalid input,
source corruption, ownership conflicts, an unusable `python` launcher, or write
failures. See
[consumer integration](docs/consumer-integration.md) for conflict ownership,
rollback limitations, and pilot migration notes.

After installing, complete the post-install acceptance checklist in the
consumer's `.agent-handoff-toolkit/consumer-integration.md` before enabling the
workflow there.

The hooks and lifecycle commands run `python`, which must resolve to Python 3.11 or newer on every machine that uses the consumer repository. `install` and `sync` check this with the `PATH` of the shell that runs them and report `launcher-unavailable` instead of installing hooks that cannot start. Hosts launched another way, such as from a desktop shortcut, can see a different `PATH`. macOS, Debian and Ubuntu install only `python3` by default; make `python` resolve to it, for example by adding the `libexec/bin` directory of the installed Homebrew Python, such as `/opt/homebrew/opt/python@3.13/libexec/bin`, to `PATH`. On Windows, `python` must be a real interpreter rather than the Microsoft Store alias.

## Development

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests distribution
python -m ruff check src tests distribution
python -m ruff format --check src tests distribution
python -m build
```

The package has no runtime dependencies outside the Python standard library.

## Software versions

Last checked: 2026-09-10.

- Minimum supported Python: [3.11](https://www.python.org/downloads/)
- CI Python versions: [3.11, 3.12, 3.13, 3.14](https://www.python.org/downloads/)
- Latest stable Python checked: [3.14.7](https://www.python.org/downloads/)
- GitHub Actions: [`actions/checkout@v7`](https://github.com/actions/checkout), [`actions/setup-python@v7`](https://github.com/actions/setup-python)
- Build frontend: [`build==1.6.1`](https://pypi.org/project/build/1.6.1/)
- Build backend: [`setuptools==84.0.0`](https://pypi.org/project/setuptools/84.0.0/)
- Linter and formatter: [`ruff==0.16.7`](https://pypi.org/project/ruff/0.16.7/)

## License

MIT. See [LICENSE](LICENSE).
