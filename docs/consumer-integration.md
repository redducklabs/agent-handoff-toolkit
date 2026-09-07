# Consumer integration

## Release checkout installation

Run installation from an inspectable checkout of the public release rather than
from a network downloader:

```powershell
git clone --branch v0.2.0 --depth 1 https://github.com/redducklabs/agent-handoff-toolkit.git agent-handoff-toolkit
Set-Location agent-handoff-toolkit
python distribution/runner.py install --target <consumer-repository> --release v0.2.0 --dry-run
python distribution/runner.py install --target <consumer-repository> --release v0.2.0 --apply
python distribution/runner.py sync --target <consumer-repository> --release v0.2.0 --check
python distribution/runner.py sync --target <consumer-repository> --release v0.2.0 --apply
```

Run install and sync only in an isolated, clean Git worktree. Confirm the
worktree is clean before planning, and prevent editors, hooks, or other processes
from writing target files until the command finishes.

Exit code `0` means a dry run is installable, an apply completed, or the sync
check is current. Exit code `1` means `sync --check` found safe pending
updates. Exit code `2` means invalid input, an unsafe path, source corruption,
an ownership conflict, or a write failure.

## Runtime prerequisite

Before enabling either host's hooks, run `python --version` in the consumer repository. The `python` command must resolve to Python 3.11 or newer. This requirement is machine-readable in `distribution/manifest.json` and matches the launcher in every distributed hook fragment.

## Managed ownership and conflicts

Consumers pin a tagged toolkit release. The installer:

- provides `install --dry-run`, `install --apply`, `sync --check`, and `sync --apply`;
- copies the same canonical skill into `.agents/skills/agent-handoff/` and `.claude/skills/agent-handoff/` rather than use symlinks;
- merges managed instruction blocks into existing `AGENTS.md` and `CLAUDE.md` without replacing project-specific content;
- merges Claude settings and installs Codex hooks without overwriting unrelated hooks;
- records the toolkit release, schema version, managed paths, and content hashes
  in `.agent-handoff-toolkit/install-state.json`;
- detects local edits, shows a patch, and refuses destructive replacement;
- keeps generated policy text derived from one canonical source.

The installer owns only copied toolkit files, the marked toolkit blocks in
`AGENTS.md` and `CLAUDE.md`, and its precise JSON fragment entries. Existing
project instructions and unrelated JSON settings remain consumer-owned. It
refuses locally modified, missing, or identity-colliding owned content instead
of overwriting it. Historical handoff records are evidence and are never
rewritten by installation or sync.

Each target is stale-checked immediately before its write and published with a
per-file atomic replacement. Multi-file atomicity is not provided. If a later
write fails, rollback is best effort and restores an earlier target only if its
current bytes still equal this run's output. Different current bytes are
preserved and reported. Inspect the worktree before retrying any failed apply.

## Pilot migrations

Before bulk adoption, select one repository with mature handoffs and one with little or no handoff policy. In each pilot:

1. Inventory local instructions, hooks, skills, handoffs, audits, and tracker conventions.
2. Run the v0.2.0 installer in dry-run mode from its release checkout.
3. Review the proposed instruction and hook merges.
4. Apply on a branch and validate existing records without rewriting history.
5. Exercise one continuation and one highest-scope completion audit in both Claude Code and Codex.
6. Record compatibility exceptions as project-local overlays, not forks of the core contract.

Broader rollout begins only after both pilots pass their repository checks and
agent workflows. The minimal-policy consumer verifies clean creation and runtime
operation. The mature-policy consumer resolves identity conflicts explicitly
and verifies that all project-owned instructions, settings, and overlays remain
unchanged. Consumer-specific implementation details stay in the consumer
repository.
