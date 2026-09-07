# Managed Install and Sync Design

## Purpose

Version 0.2.0 adds a release-pinned, repository-local installation and update
mechanism for the Agent Handoff Toolkit. It replaces manual copying with a
deterministic workflow that preserves consumer-specific instructions and hooks,
detects local edits to toolkit-owned content, and makes every proposed change
reviewable before it is written.

The initial rollout will exercise both ends of the compatibility range: a
mature-policy consumer with project-owned overlays and a minimal-policy consumer
with little or no existing agent configuration.

Historical handoff records are evidence and are never rewritten by installation
or synchronization.

## Scope and non-goals

Version 0.2.0 provides:

- `install --dry-run` and `install --apply`;
- `sync --check` and `sync --apply`;
- source-manifest validation and source-content hash verification;
- an installed-state file that pins the toolkit release and records ownership;
- managed copies, marked instruction blocks, and merge-safe JSON fragments;
- local-edit, collision, and path-escape detection;
- unified patch previews;
- per-file atomic replacement with best-effort rollback if a later write fails;
  and
- documentation and tests for source and installed layouts.

It does not:

- download releases or call GitHub;
- install Python or change application dependencies;
- rewrite legacy handoffs;
- infer whether statements inside a handoff are true;
- automatically delete superseded project-specific scripts or documentation;
- merge arbitrary prose outside a marked managed block; or
- resolve a JSON ownership collision without an explicit consumer migration.

The command runs from a checked-out tagged toolkit release. This keeps network
and authentication behavior outside the installer and makes the bytes being
installed inspectable. Install and sync run only in an isolated, clean Git
worktree with no concurrent filesystem writers.

## Command contract

The release checkout exposes these commands through
`python distribution/runner.py`:

```text
python distribution/runner.py install --target <repo> --release v0.2.0 --dry-run
python distribution/runner.py install --target <repo> --release v0.2.0 --apply
python distribution/runner.py sync --target <repo> --release v0.2.0 --check
python distribution/runner.py sync --target <repo> --release v0.2.0 --apply
```

`--source-root` may override the inferred toolkit checkout root for tests and
packaging probes. The source root must contain `distribution/manifest.json`.
The requested release must exactly equal `v` followed by the manifest's toolkit
version.

Exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | The dry run is installable, apply succeeded, or sync check is current. |
| `1` | `sync --check` found safe changes that can be applied. |
| `2` | Invalid input, unsafe path, source corruption, ownership conflict, or write failure. |

Dry-run and check modes never write. Every mode prints a concise per-target
status. Changed text targets include a unified diff with repository-relative
paths. Secret values are never introduced by toolkit artifacts, and commands do
not print unrelated consumer-file contents.

`install` refuses when a valid installed-state file already exists. `sync`
refuses when it does not exist. Re-running an apply after a successful operation
is therefore explicit rather than ambiguous.

## Distribution manifest

`distribution/manifest.json` advances to manifest version 2 and adds:

- `toolkit_version: "0.2.0"`;
- `text_hash: "utf8-lf-sha256-v1"`;
- a `managed-block` installation mode for `AGENTS.md` and `CLAUDE.md`;
- an ownership identifier for marked blocks;
- JSON array identity declarations where a same-purpose entry must be treated as
  a collision rather than appended; and
- the additional source modules needed by the installed runtime.

All sources and targets must be relative, normalized paths. Absolute paths,
empty components, `.` or `..` components, duplicate targets, Windows reserved
device names and characters, control characters, trailing dots or spaces, and
Windows-normalized collisions are invalid. Resolved sources must remain beneath
the source root. Resolved targets and every existing target parent must remain
beneath the consumer root, including through symlinks or junctions. Every
existing intermediate target component must be a directory.

Every distributed text artifact is UTF-8. Hashes normalize CRLF and CR to LF
before hashing. This prevents `core.autocrlf` from making an otherwise unchanged
managed artifact appear edited while retaining exact content sensitivity.

## Ownership model

The tracked file `.agent-handoff-toolkit/install-state.json` is the consumer's
ownership record. It contains:

- state schema version;
- installed toolkit release and toolkit version;
- handoff record schema version;
- each managed target's source, mode, and installed normalized hash; and
- the exact owned JSON fragment for each JSON merge target.

The state contains no machine-specific absolute paths or installation timestamp,
so identical installations produce identical state.

Synchronization refuses when a previously managed target is absent from the new
manifest. Removing ownership requires an explicit consumer migration because
version 0.2.0 has no managed-target deletion mechanism.

### Copy targets

A copy target may be created when absent. An existing unowned file is accepted
only when its normalized content is byte-equivalent to the desired artifact;
otherwise installation reports a conflict. During synchronization, the current
target must match either the recorded installed hash or the new desired hash. A
missing, modified, or replaced target is a conflict; synchronization never
silently restores or overwrites it.

### Managed instruction blocks

One canonical consumer-policy source is installed into both `AGENTS.md` and
`CLAUDE.md` between these markers:

```text
<!-- agent-handoff-toolkit:start -->
<!-- agent-handoff-toolkit:end -->
```

An absent instruction file is created. An existing file receives the block at
the end without reordering its content. The existing file's newline convention
is preserved. Partial, duplicated, nested, or locally modified toolkit markers
are conflicts. Synchronization replaces only a block whose normalized hash
matches the installed state. Content outside the block is never owned or
rewritten.

The block states that `docs/agent-handoff/contract.md` is the shared normative
minimum for new continuation and completion-audit records, that project overlays
may add stricter requirements, and that conflicts in record semantics or final
response shape resolve to the shared contract. It directs both hosts to the
same installed skill and commands.

### JSON fragments

JSON merge targets are parsed as strict UTF-8 JSON objects. Object keys merge
recursively. Arrays preserve existing order and append absent managed entries.
Exact existing entries are adopted without duplication.

The manifest may identify an array entry by selected fields, such as the
`matcher` in a Claude `PostToolUse` hook. If an unowned entry has the same
identity but different content, installation reports a collision. It does not
append a second same-purpose hook or replace the existing one. Consumer-specific
migration must remove or alter the conflicting entry explicitly before apply.

During synchronization, the exact old fragment stored in installed state is the
ownership baseline. Owned entries may advance to the new fragment; unrelated
keys and array entries remain untouched. A missing or locally changed owned
entry is a conflict unless it already equals the new desired entry.

## Planning and application

The implementation separates four responsibilities:

- `manifest.py` parses and validates the source manifest and verifies source
  hashes.
- `operations.py` plans copy, managed-block, and JSON-fragment transformations.
- `state.py` parses, validates, and renders installed ownership state.
- `installer.py` builds a complete immutable plan, renders status and diffs,
  rechecks preconditions, applies atomic replacements, and rolls back already
  changed files if a later write fails.

No target is written until every source, target, state transition, and
transformation has planned successfully. Immediately before each write, the
installer confirms that the target still matches the bytes used to create the
plan. Writes use a sibling temporary file followed by an atomic replacement,
preserving an existing POSIX mode and using the process umask for new files. The
state file is written last.

Atomicity is per file, not across the entire multi-file operation. On failure,
best-effort rollback processes changed files in reverse order and restores a
file only while its current bytes still equal the bytes written by this run; a
different current value is preserved and reported. The isolated-worktree,
no-concurrent-writer operating constraint is required for the multi-file result.

Self-installation, where the source root and target repository resolve to the
same directory, is rejected.

## Installed runtime

The consumer continues to run hooks through:

```text
python .agent-handoff-toolkit/runner.py ...
```

The installed hook runtime remains Python 3.11 or newer and standard-library
only. Installer modules are lazy-loaded by the CLI so a consumer's vendored
runtime does not need source-manifest installation capabilities to validate
records or execute hooks.

Toolkit installation does not change a consumer's package manifest, virtual
environment, build configuration, or application Python version.

## Pilot migrations

### Minimal-policy consumer

The minimal-policy pilot starts in an isolated clean worktree. It verifies
creation of the managed instruction files and toolkit artifacts, installed
runtime smoke tests, repository-native quality gates, and a current
`sync --check`. Historical handoffs remain legacy snapshots.

### Mature-policy consumer

The mature-policy pilot also starts in an isolated clean worktree. Before apply,
maintainers resolve any ownership or hook-identity conflicts explicitly. The
installer must preserve all project-owned instructions, settings, and overlays.
Repository-native contract tests verify the shared contract authority,
installed artifacts, hook uniqueness, runtime execution, and legacy-record
exemption without publishing consumer-specific implementation details.

## Testing strategy

All production behavior is developed test-first. Tests use temporary real
directories and subprocesses rather than mocks where practical. Each test names
the production regression it catches.

Toolkit tests cover:

- manifest schema, source hashes, duplicate targets, case collisions, and path
  traversal;
- source and target symlink escapes;
- first installation into empty and preconfigured repositories;
- no-op adoption of equivalent files and hooks;
- marked-block newline preservation and malformed markers;
- JSON preservation, deduplication, same-identity collisions, and owned updates;
- local modification, deletion, malformed JSON, and stale-plan conflicts;
- normalized hashes across LF and CRLF;
- deterministic state output;
- dry-run/check exit codes and zero-write guarantees;
- apply, rollback, and state-written-last behavior;
- installed runtime commands after source checkout removal; and
- full v0.1 record, rendering, hook, and context-health regression coverage.

Both pilots add repository-native contract tests rather than relying only on
toolkit tests. Each integration runs that consumer's documented checks.

## Release and rollout gates

Version 0.2.0 is tagged only after local tests, lint, formatting,
byte-compilation, package build, installed-layout probes, independent review,
and the complete Python 3.11 through 3.14 GitHub Actions matrix pass on the
merged toolkit `main` commit.

The pilots pin `v0.2.0` and remain separate so consumer-specific migration and
review evidence do not enter the generic toolkit. Broader rollout begins only
after both pilots pass their repository checks and exercise one continuation
plus one highest-scope completion audit under both host contracts.
