# Agent Handoff Toolkit

Portable session-continuation and completion-audit standards for Claude Code and Codex.

This repository provides a shared contract, deterministic validation and rendering, repo-local agent instructions, and thin host adapters. It is designed to replace handoff policies that drift between projects while preserving project-specific instructions.

## Status

Version 0.2.5 provides release-pinned managed installation and synchronization
from an inspectable source checkout. It preserves consumer-owned instructions,
JSON settings, and opaque deprecated historical records. It also defines a
prospective post-install acceptance checklist for both Claude Code and Codex.
Validation now measures the rendered continuation tail against the record's real
absolute path, so a record cannot pass `validate` and then exceed the output
budget in `render-tail` solely because its destination path is longer.

## Core rules

- A **continuation** exists only when authorized work remains and the next session has a concrete, executable first action.
- A **completion audit** is mandatory when the highest authorized epic, feature, or standalone outcome is complete. It is evidence, never a continuation point.
- Every active scope explicitly states whether code remains. A child-level “no” never implies that its parent is complete.
- Questions that can change the next session's first action are answered before a continuation is finalized.
- Recorded repository and tracker state is a timestamped snapshot. A resumed session reconciles live state before acting.
- Verification results are classified as `pass`, `fail`, or `not-run`; skipped checks are never reported as passing.
- A continuation response says `Continue from handoff`, identifies the exact next
  action, and lists only essential blockers, decisions, and validation gates.
  Its stored context is limited to 120 words and the complete generated tail to
  300 words. The response must not reproduce the handoff document. The absolute
  clickable link remains the final line.

See [the contract](docs/agent-handoff/contract.md) for the normative requirements.

## Command surface

```text
handoff-toolkit validate <record.md>
handoff-toolkit render <record.json> --output <record.md>
handoff-toolkit render-tail <continuation.md>
handoff-toolkit context-health --percent <0-100> --session-id <id>
handoff-toolkit hook --platform claude|codex --event post-tool-use
```

Automatic hooks fail open so they cannot break an agent session. Explicit commands fail closed and return a non-zero exit code for invalid input.

## Consumer installation

Check out the public v0.2.5 release, inspect the proposed changes, then apply
them from that checkout:

```powershell
git clone --branch v0.2.5 --depth 1 https://github.com/redducklabs/agent-handoff-toolkit.git agent-handoff-toolkit
Set-Location agent-handoff-toolkit
python distribution/runner.py install --target <consumer-repository> --release v0.2.5 --dry-run
python distribution/runner.py install --target <consumer-repository> --release v0.2.5 --apply
python distribution/runner.py sync --target <consumer-repository> --release v0.2.5 --check
python distribution/runner.py sync --target <consumer-repository> --release v0.2.5 --apply
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

Last checked: 2026-09-06.

- Minimum supported Python: 3.11
- CI Python versions: 3.11, 3.12, 3.13, 3.14
- Latest stable Python checked: 3.14.7
- GitHub Actions: `actions/checkout@v7`, `actions/setup-python@v7`
- Build frontend: `build==1.6.0`
- Build backend: `setuptools==84.0.0`
- Linter and formatter: `ruff==0.16.6`

Version sources are the official Python release index and the official GitHub Action repositories.

## License

MIT. See [LICENSE](LICENSE).
