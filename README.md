# Agent Handoff Toolkit

Portable session-continuation and completion-audit standards for Claude Code and Codex.

This repository provides a shared contract, deterministic validation and rendering, repo-local agent instructions, and thin host adapters. It is designed to replace handoff policies that drift between projects while preserving project-specific instructions.

## Status

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
- A schema-v2 record stores each fact once, in a visible metadata block at the top of the document. No narrative section restates verification, the exact next action, the scope list, or the next-session prompt. Schema-v1 records keep their metadata comment and their derived sections.
- A continuation response says `This session is stopped because authorized work
  remains` and `What you need to do: Start a new session from the continuation
  handoff below`. Its copy/paste block for the new session begins with `Continue
  from handoff`, identifies the exact next action, and lists only essential blockers,
  decisions, and validation gates. Its stored context is limited to 120 words
  and the complete generated tail to 300 words. The response must not reproduce
  the handoff document. The absolute clickable link remains the final line.

See [the contract](docs/agent-handoff/contract.md) for what an author must write, and [the mechanics reference](docs/agent-handoff/mechanics.md) for the exact formats and enforcement the toolkit applies.

## Command surface

```text
handoff-toolkit validate <record.md>
handoff-toolkit render <record.json> --output <record.md>
handoff-toolkit render-tail <continuation.md>
handoff-toolkit context-health --percent <0-100> --session-id <id>
handoff-toolkit hook --platform claude|codex --event post-tool-use
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

Check out the public v0.3.2 release, inspect the proposed changes, then apply
them from that checkout:

```powershell
git clone --branch v0.3.2 --depth 1 https://github.com/redducklabs/agent-handoff-toolkit.git agent-handoff-toolkit
Set-Location agent-handoff-toolkit
python distribution/runner.py install --target <consumer-repository> --release v0.3.2 --dry-run
python distribution/runner.py install --target <consumer-repository> --release v0.3.2 --apply
python distribution/runner.py sync --target <consumer-repository> --release v0.3.2 --check
python distribution/runner.py sync --target <consumer-repository> --release v0.3.2 --apply
```

Run install and sync only in an isolated, clean Git worktree with no concurrent
writers. Recheck the worktree after any failed apply: replacement is atomic per
file, while multi-file rollback is best effort and preserves bytes that no
longer match the output written by that run.

`install --dry-run` and a current `sync --check` exit `0`; `sync --check`
exits `1` when safe updates are available; and `2` reports invalid input,
source corruption, ownership conflicts, or write failures. See
[consumer integration](docs/consumer-integration.md) for conflict ownership,
rollback limitations, and pilot migration notes.

Before enabling the distributed hooks, run `python --version` in the consumer repository and verify that `python` resolves to Python 3.11 or newer. The manifest declares this exact launcher and minimum version; the hook fragments use the same command.

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
