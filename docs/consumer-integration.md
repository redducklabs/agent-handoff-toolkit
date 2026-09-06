# Consumer integration

## V1 boundary

V1 provides the canonical contract, templates, skill, host hook fragments, and validation/rendering CLI. It does not rewrite consumer repositories. Copying files by hand is supported for evaluation but is not the long-term update path.

## Target rollout model

Consumers should pin a tagged toolkit release. A future installer will:

- offer `install --dry-run`, `install --apply`, `sync --check`, and `sync --apply`;
- copy the same canonical skill into `.agents/skills/agent-handoff/` and `.claude/skills/agent-handoff/` rather than use symlinks;
- merge managed instruction blocks into existing `AGENTS.md` and `CLAUDE.md` without replacing project-specific content;
- merge Claude settings and install Codex hooks without overwriting unrelated hooks;
- record the toolkit release, schema version, managed paths, and content hashes in a repository-local manifest;
- detect local edits, show a patch, and refuse destructive replacement;
- keep generated policy text derived from one canonical source.

## Evaluation rollout

Before bulk adoption, select one repository with mature handoffs and one with little or no handoff policy. In each pilot:

1. Inventory local instructions, hooks, skills, handoffs, audits, and tracker conventions.
2. Run the installer in dry-run mode once it exists.
3. Review the proposed instruction and hook merges.
4. Apply on a branch and validate existing records without rewriting history.
5. Exercise one continuation and one highest-scope completion audit in both Claude Code and Codex.
6. Record compatibility exceptions as project-local overlays, not forks of the core contract.

Bulk rollout begins only after both pilots pass their repository checks and agent workflows.
