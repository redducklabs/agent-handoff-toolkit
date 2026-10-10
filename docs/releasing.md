# Releasing and re-pinning consumers

This is the maintainer procedure for cutting a toolkit release and moving
consumer repositories onto it. It is not distributed to consumers.

## Before opening the release PR

- **Run all five commands** under `## Development` in `README.md` and confirm
  they pass.
- **Run the POSIX-only tests as well.** Tests marked
  `skipIf(os.name == "nt")` never run on a Windows workstation, and CI runs
  them on Linux. On Windows, run the suite under WSL first:
  `wsl -e bash -lc 'cd /mnt/<drive>/<path>/agent-handoff-toolkit && python3 -m unittest discover -s tests'`.
  Under a `/mnt/` Windows checkout, two `test_distribution` tests fail only
  because the checkout has CRLF bytes:
  - `test_managed_artifact_bytes_are_stable_across_git_checkouts`
  - `test_sync_upgrades_a_prior_release_without_touching_legacy_records`

  CI is authoritative for those two.
- **Re-sync manifest hashes by hand.** No script regenerates
  `distribution/manifest.json`. Each managed artifact carries the `sha256` of
  its source with CRLF normalized to LF, and `tests/test_distribution.py`
  recomputes them. Any edit to a managed source leaves the manifest stale.
  Recompute the affected hashes after every source change, not only at
  release time.
- **Respect the `mechanics.md` word budget.** It is under 3200 words and a
  test enforces this. Tighten wording rather than raising the limit.
- **Write helper scripts to a file.** Writing scripts that contain backslashes
  through a Bash heredoc on Windows halves the backslashes. Write the helper
  script to a file and run it instead.

## Version bump

A release bump is part of the release commit. It touches:

- `pyproject.toml`
- `src/agent_handoff_toolkit/__init__.py`
- `distribution/manifest.json` (`toolkit_version`, plus re-synced hashes)
- `src/agent_handoff_toolkit/acceptance.py` (the release the smoke check
  installs)
- `tests/test_acceptance.py`
- `tests/test_distribution.py`
- `docs/consumer-integration.md`
- `README.md`

`test_manifest_hashes_every_managed_artifact` asserts that every `vX.Y.Z`
string in `README.md` and `docs/consumer-integration.md` equals the new
release, so a stale reference anywhere in either file fails the build.

`README.md` `## Status` gets a new written paragraph for the release. Older
paragraphs are history: only the previous release's opening verb changes to
the past tense.

## Merge, tag and publish

1. Merge only after `gh pr checks` reports `pass` for every job. The
   repository has no rulesets requiring checks, so `gh pr merge --auto` is
   not a gate.
2. Confirm the merge commit's tree equals the tested head, then run
   `git tag -a vX.Y.Z -m vX.Y.Z <merge sha>` and push the tag.
3. Run `gh release create vX.Y.Z --verify-tag` with notes. The notes call out
   any breaking change and any security consideration.

Consumers pin both the tag and its commit SHA, so the release must exist
before any consumer is re-pinned.

## Re-pinning a consumer

Each consumer's own installation document (for example
`docs/context-handoff-installation.md`) and its context-health test record
that consumer's pin sites. Never rely on an outside note for them.

1. Create a detached checkout of the release tag. Run everything from there.
2. Never touch the consumer's primary checkout: live sessions may be running
   in it. Create a new worktree off the consumer's `origin/main`, following
   that repository's worktree convention.
3. Run
   `python <release checkout>/distribution/runner.py sync --target <worktree> --release vX.Y.Z --apply`,
   then `--check` until it prints `CURRENT`. Never hand-edit managed files.
4. Update every pin the consumer carries. A context-health test typically
   pins:
   - `INSTALL_STATE_SHA256`: the sha256 of
     `.agent-handoff-toolkit/install-state.json` with CRLF normalized to LF;
   - the release string;
   - the bare version;
   - the toolkit commit SHA, which the test also requires in the installation
     document.

   The `INSTALL_STATE_SHA256` assignment is formatted differently in different
   consumers, so check each diff rather than trusting one substitution.
5. A release that changes the managed hook command strings also changes the
   consumer's exact-command assertions. On Windows, a hook-execution helper
   that runs `shlex.split(command, posix=False)` keeps the quotes it split on.
   Strip matching surrounding quotes per token, or the quoted `-c` program
   text becomes a string literal that silently does nothing.
6. Grep the consumer for the old release, the old commit SHA and any
   instruction the release made stale, and correct its own docs. Leave
   historical records under `handoffs/` alone.
7. Follow the consumer's own PR rules: commit format, ticket references,
   required pre-PR review, lint gates. Then open the PR and merge once its
   checks pass.
