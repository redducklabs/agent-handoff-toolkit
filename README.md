# Agent Handoff Toolkit

Portable session-continuation and completion-audit standards for Claude Code and Codex.

This repository provides a shared contract, deterministic validation and rendering, repo-local agent instructions, and thin host adapters. It is designed to replace handoff policies that drift between projects while preserving project-specific instructions.

## Status

The initial toolkit is under active development. Consumer repositories are not modified automatically by this release.

## Core rules

- A **continuation** exists only when authorized work remains and the next session has a concrete, executable first action.
- A **completion audit** is mandatory when the highest authorized epic, feature, or standalone outcome is complete. It is evidence, never a continuation point.
- Every active scope explicitly states whether code remains. A child-level “no” never implies that its parent is complete.
- Questions that can change the next session's first action are answered before a continuation is finalized.
- Recorded repository and tracker state is a timestamped snapshot. A resumed session reconciles live state before acting.
- Verification results are classified as `pass`, `fail`, or `not-run`; skipped checks are never reported as passing.
- A continuation response ends with the stored next-session prompt in a copy/paste block and an absolute clickable link as its final line.

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

## Consumer strategy

V1 ships source artifacts and documents the integration contract. The next rollout increment will add manifest-based `install` and `sync` commands with dry-run-first behavior, tagged-version pinning, managed-file hashes, and merge-safe updates for existing `AGENTS.md`, `CLAUDE.md`, and Claude settings. See [consumer integration](docs/consumer-integration.md).

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
