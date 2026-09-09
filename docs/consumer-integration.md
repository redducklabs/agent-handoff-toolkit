# Consumer integration

## Release checkout installation

Run installation from an inspectable checkout of the public release rather than
from a network downloader:

```powershell
git clone --branch v0.2.7 --depth 1 https://github.com/redducklabs/agent-handoff-toolkit.git agent-handoff-toolkit
Set-Location agent-handoff-toolkit
python distribution/runner.py install --target <consumer-repository> --release v0.2.7 --dry-run
python distribution/runner.py install --target <consumer-repository> --release v0.2.7 --apply
python distribution/runner.py sync --target <consumer-repository> --release v0.2.7 --check
python distribution/runner.py sync --target <consumer-repository> --release v0.2.7 --apply
```

Run install and sync only in an isolated, clean Git worktree. Confirm the
worktree is clean before planning, and prevent editors, hooks, or other processes
from writing target files until the command finishes.

Exit code `0` means a dry run is installable, an apply completed, or the sync
check is current. Exit code `1` means `sync --check` found safe pending
updates. Exit code `2` means invalid input, an unsafe path, source corruption,
an ownership conflict, or a write failure.

## Runtime prerequisite

Before enabling either host's hooks, run `python --version` in the consumer repository. The `python` command must resolve to Python 3.11 or newer. This requirement is machine-readable in the pinned release checkout's `distribution/manifest.json` and matches the launcher in every distributed hook fragment.

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

The installer owns every copied file listed in the pinned release checkout's
`distribution/manifest.json`, the marked toolkit blocks in `AGENTS.md` and
`CLAUDE.md`, and the precise merged JSON entries in `.claude/settings.json` and
`.codex/hooks.json`. This includes the vendored runtime and license, contract,
consumer acceptance guide, both host skill copies, both record templates, and
both hook integrations. The installed
`.agent-handoff-toolkit/install-state.json` is the source of truth for the
pinned release, schema version, managed targets, modes, and installed hashes.
Existing project instructions and unrelated JSON settings remain
consumer-owned. The installer refuses locally modified, missing, or
identity-colliding owned content instead of overwriting it.

Every handoff record that existed before the current pinned release was adopted
in the consumer is a deprecated historical artifact by policy. Installation and
synchronization preserve those records as opaque files: do not open, read,
review, validate, migrate, summarize, reconcile, or rewrite them. No unresolved
question or content in those files gates adoption. The current policy applies
only to new or materially replaced records.

Each target is stale-checked immediately before its write and published with a
per-file atomic replacement. Multi-file atomicity is not provided. If a later
write fails, rollback is best effort and restores an earlier target only if its
current bytes still equal this run's output. Different current bytes are
preserved and reported. Inspect the worktree before retrying any failed apply.

## Post-install consumer acceptance

Run these checks on the installation branch before enabling routine use:

1. Run `sync --check` from the pinned release checkout. It must report `CURRENT`
   without changing the consumer worktree.
2. Compare `.agent-handoff-toolkit/install-state.json` with the pinned release
   checkout's `distribution/manifest.json`. Confirm every managed copy, managed
   block, and merge-owned JSON entry is represented. Hash-check the complete
   managed blocks, including their markers, and require the `AGENTS.md` and
   `CLAUDE.md` blocks to be byte-identical after LF normalization. Inspect the
   merged Claude and Codex hook JSON while leaving unrelated settings unchanged.
   For the acceptance test, pin the LF-normalized SHA-256 of the reviewed
   `install-state.json` as a test constant; never derive expected values from the
   state under test. The state is the installer's source of truth, not the
   acceptance test's oracle.
3. From the consumer repository root, execute every installed Claude and Codex
   hook command directly with representative matching and non-matching input.
   Derive each command from the installed JSON rather than duplicating its
   argument list in the test. Assert that SessionStart emits the exact policy
   sentence `Do not search or inspect deprecated legacy handoffs.` Do not
   infer runtime viability from valid JSON alone.
4. Create fresh disposable continuation and completion-audit records. Render and
   validate both record types. Assert the continuation's copy/paste tail and the
   completion audit's `Audit record (not a handoff)` link, including correct URL
   encoding, and assert that an audit emits no restart prompt. Include the highest
   authorized scope completion audit. Verification results and exact next actions
   must be derived from record metadata, not maintained as handwritten prose.
5. Reconcile project overlays at the policy level. Overlays may be stricter but
   cannot loosen or contradict the record-type decision, canonical derived
   sections, or final-tail requirements. This review never includes deprecated
   historical records.
6. Add consumer regression tests for the merged Claude and Codex hook JSON,
   install-state ownership and `CURRENT` status, schema-derived section parity,
   and continuation/completion record-decision semantics. Recursively assert
   every merge-owned JSON fragment, not only copied-file hashes. If this focused
   test runs in a container, mount every asserted managed artifact read-only.
   Missing, empty, wrong-type, or unmounted artifacts must fail, never skip.
7. Report automatic Codex hook discovery as unverified unless it has been
   observed end-to-end in the consumer's actual Codex host. Direct command
   execution verifies the installed hook itself, not host discovery.
8. Confirm a rendered continuation says the current session is stopped, directs
   the user to start a new session, and begins its fenced block for that new
   session with `Continue from handoff`. It must name the absolute record path and exact next
   action, include only essential blockers, decisions, and validation gates, and
   not reproduce record metadata or narrative sections.
9. Search active consumer-owned documentation and tests, excluding deprecated
   handoffs, for stale toolkit release tags, versions, commit pins, or
   assertions. Update every active reference to the exact installed release.

For a meta-only toolkit or handoff change, run only these focused local
acceptance checks; never launch the consumer's full local application, backend,
frontend, end-to-end, build, or dependency-audit suites. Let required
pull-request CI run normally. Run broader local suites only when product code
also changes.

Structural checks prove schema, ownership, merge, and runtime properties. They
do not prove that prose is semantically complete or truthful; that remains a
review responsibility for each new or materially replaced record.

## Pilot migrations

Before bulk adoption, select one repository with mature handoffs and one with little or no handoff policy. In each pilot:

1. Inventory local instructions, hook and skill configuration, and tracker
   conventions. Count or locate historical handoff storage only if needed to
   protect it; do not open the records.
2. Run the v0.2.7 installer in dry-run mode from its release checkout.
3. Review the proposed instruction and hook merges.
4. Apply on a branch. Treat every record that predates this adoption as
   deprecated by policy without marking, reading, or rewriting individual files.
5. Exercise one continuation and one highest-scope completion audit in both Claude Code and Codex.
6. Record compatibility exceptions as project-local overlays, not forks of the core contract.

Broader rollout begins only after both pilots pass their repository checks and
agent workflows. The minimal-policy consumer verifies clean creation and runtime
operation. The mature-policy consumer resolves identity conflicts explicitly
and verifies that all project-owned instructions, settings, and overlays remain
unchanged. Consumer-specific implementation details stay in the consumer
repository.
