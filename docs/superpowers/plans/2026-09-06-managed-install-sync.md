# Managed Install and Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, release-pinned install and sync commands that safely manage toolkit artifacts in consumer repositories.

**Architecture:** Parse and validate the v2 source manifest into immutable models, plan copy/managed-block/JSON changes without writing, record exact ownership in a deterministic consumer state file, then apply each file with an immediate stale-plan check and best-effort multi-file rollback. Installer modules are lazy-loaded so the smaller vendored hook runtime remains self-contained.

**Tech Stack:** Python 3.11+ standard library, `unittest`, `argparse`, JSON, `pathlib`, `hashlib`, `difflib`, and atomic filesystem replacement.

**Spec:** `docs/superpowers/specs/2026-09-06-managed-install-sync-design.md`

## Global Constraints

- The installer performs no network or GitHub calls.
- Runtime dependencies remain Python standard-library only and Python 3.11 or newer.
- Commands are exactly `install --target <repo> --release <tag> (--dry-run|--apply)` and `sync --target <repo> --release <tag> (--check|--apply)`, with optional `--source-root`.
- Exit code `0` means installable/applied/current, `1` means `sync --check` found safe changes, and `2` means invalid input, conflict, unsafe path, source corruption, or write failure.
- Dry-run and check modes perform zero writes.
- Install and sync run in an isolated, clean Git worktree with no concurrent
  filesystem writers; atomicity is per file and transaction rollback is best
  effort.
- Historical handoff records are never rewritten.
- Toolkit-managed text hashes normalize CRLF and CR to LF before SHA-256.
- Absolute, traversal, duplicate, Windows case-colliding, symlink-escaping, and self-install paths are rejected.
- Existing consumer content outside toolkit-owned copies, marked blocks, and exact JSON fragments is preserved.
- The installed state is deterministic and contains no timestamp or machine-specific absolute path.
- All production behavior follows a witnessed RED/GREEN test cycle.
- `AGENTS.md` and `CLAUDE.md` remain semantically identical for toolkit-repository instructions.
- No secrets, customer data, private-repository content, or AI attribution may enter the public repository.

---

### Task 1: Manifest and installed-state contracts

**Files:**
- Create: `src/agent_handoff_toolkit/manifest.py`
- Create: `src/agent_handoff_toolkit/state.py`
- Create: `tests/test_installer.py`
- Modify: `src/agent_handoff_toolkit/__init__.py`

**Interfaces:**
- Produces: `normalize_text(data: bytes) -> bytes`
- Produces: `text_sha256(data: bytes) -> str`
- Produces: `load_manifest(source_root: Path) -> Manifest`
- Produces: `load_state(target_root: Path) -> InstalledState | None`
- Produces: `render_state(state: InstalledState) -> bytes`
- Produces immutable `Manifest`, `Artifact`, `JsonArrayIdentity`, `InstalledState`, and `TargetState` dataclasses.
- Consumes: no new project interfaces.

- [ ] **Step 1: Write failing normalization and manifest tests**

Add table-driven tests whose literal expectations prove that LF and CRLF hash
identically, malformed UTF-8 fails, manifest version 2 is required, the release
is `v` plus `toolkit_version`, source hashes are verified, and unsafe source or
target paths are rejected. Include a Windows case-fold collision such as
`Docs/File.md` and `docs/file.md`.

```python
class ManifestTests(unittest.TestCase):
    def test_text_hash_normalizes_all_line_endings(self) -> None:
        expected = hashlib.sha256(b"one\ntwo\n").hexdigest()
        self.assertEqual(text_sha256(b"one\r\ntwo\r"), expected)

    def test_duplicate_targets_are_rejected_case_insensitively(self) -> None:
        root = self.make_source(
            artifacts=[
                self.copy_artifact("a.md", "Docs/File.md"),
                self.copy_artifact("b.md", "docs/file.md"),
            ]
        )
        with self.assertRaisesRegex(ManifestError, "target collision"):
            load_manifest(root)
```

- [ ] **Step 2: Run the manifest tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.ManifestTests -v
```

Expected: import or symbol failures because `manifest.py` does not exist.

- [ ] **Step 3: Implement immutable manifest parsing and source validation**

Implement focused dataclasses and strict parsing. Reject unknown modes and
unknown transform/hash strategies rather than guessing.

```python
class ManifestError(ValueError):
    pass

@dataclass(frozen=True)
class JsonArrayIdentity:
    pointer: str
    fields: tuple[str, ...]

@dataclass(frozen=True)
class Artifact:
    source: PurePosixPath
    sha256: str
    mode: str
    targets: tuple[PurePosixPath, ...]
    block_id: str | None = None
    array_identities: tuple[JsonArrayIdentity, ...] = ()

@dataclass(frozen=True)
class Manifest:
    toolkit_version: str
    record_schema_version: int
    python_command: str
    minimum_python: str
    artifacts: tuple[Artifact, ...]

def normalize_text(data: bytes) -> bytes:
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

def text_sha256(data: bytes) -> str:
    return hashlib.sha256(normalize_text(data)).hexdigest()
```

Resolve every source and existing target parent and confirm it remains below its
declared root with `Path.relative_to`. Treat symlinks and junctions as resolved
paths, not lexical paths.

- [ ] **Step 4: Run manifest tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.ManifestTests -v
```

Expected: all `ManifestTests` pass.

- [ ] **Step 5: Write failing deterministic-state tests**

Test missing state, malformed state, unknown state version, duplicate target
entries, JSON fragment round-tripping, sorted deterministic output, and the
absence of timestamps and absolute paths.

```python
def test_render_state_is_deterministic(self) -> None:
    state = InstalledState(
        state_version=1,
        release="v0.2.0",
        toolkit_version="0.2.0",
        record_schema_version=1,
        targets=(
            TargetState("z.md", "z.md", "copy", "f" * 64),
            TargetState("a.md", "a.md", "copy", "e" * 64),
        ),
    )
    rendered = render_state(state)
    self.assertEqual(rendered, render_state(state))
    self.assertLess(rendered.index(b'"a.md"'), rendered.index(b'"z.md"'))
    self.assertNotIn(b"installed_at", rendered)
```

- [ ] **Step 6: Run the state tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.StateTests -v
```

Expected: import or symbol failures because `state.py` does not exist.

- [ ] **Step 7: Implement state parsing and rendering**

Use `.agent-handoff-toolkit/install-state.json` and exact dataclasses:

```python
STATE_RELATIVE_PATH = PurePosixPath(".agent-handoff-toolkit/install-state.json")

@dataclass(frozen=True)
class TargetState:
    target: str
    source: str
    mode: str
    installed_sha256: str
    owned_fragment: object | None = None

@dataclass(frozen=True)
class InstalledState:
    state_version: int
    release: str
    toolkit_version: str
    record_schema_version: int
    targets: tuple[TargetState, ...]
```

Render with `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False)` plus
one LF. Validate hashes as 64 lowercase hexadecimal characters and validate all
stored paths with the same relative-path rules as the source manifest.

- [ ] **Step 8: Run Task 1 tests and the existing suite**

Run:

```powershell
python -m unittest tests.test_installer.ManifestTests tests.test_installer.StateTests -v
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit Task 1**

```powershell
git add src/agent_handoff_toolkit/manifest.py src/agent_handoff_toolkit/state.py src/agent_handoff_toolkit/__init__.py tests/test_installer.py
git commit -m "feat: define managed installation contracts"
```

---

### Task 2: Pure transformation and planning engine

**Files:**
- Create: `src/agent_handoff_toolkit/operations.py`
- Create: `src/agent_handoff_toolkit/installer.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: Task 1 manifest and state dataclasses and normalized hashing.
- Produces: `build_plan(source_root: Path, target_root: Path, release: str, operation: Literal["install", "sync"]) -> InstallPlan`
- Produces immutable `FileChange`, `Conflict`, and `InstallPlan` dataclasses.
- Produces: `render_plan(plan: InstallPlan) -> str`
- Task 3 consumes `InstallPlan` and `FileChange` preconditions for application.

- [ ] **Step 1: Write failing copy-operation tests**

Cover absent targets, identical unowned adoption, conflicting unowned content,
safe sync updates, locally modified files, missing installed targets, CRLF/LF
equivalence, and source/target self-installation.

```python
def test_sync_refuses_a_locally_modified_copy(self) -> None:
    installed = self.install_fixture()
    target = installed / "docs" / "agent-handoff" / "contract.md"
    target.write_text("local edit\n", encoding="utf-8")
    plan = build_plan(self.source_v2, installed, "v0.2.1", "sync")
    self.assertEqual([item.code for item in plan.conflicts], ["managed-copy-modified"])
    self.assertEqual(plan.changes, ())
```

- [ ] **Step 2: Run copy-operation tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.CopyPlanningTests -v
```

Expected: import or symbol failures because the planning interfaces do not exist.

- [ ] **Step 3: Implement change models and copy planning**

Use bytes captured at planning time as stale-plan preconditions:

```python
@dataclass(frozen=True)
class FileChange:
    target: Path
    relative_target: str
    before: bytes | None
    after: bytes
    mode: str

@dataclass(frozen=True)
class Conflict:
    target: str
    code: str
    message: str

@dataclass(frozen=True)
class InstallPlan:
    operation: str
    release: str
    target_root: Path
    changes: tuple[FileChange, ...]
    conflicts: tuple[Conflict, ...]
    state: InstalledState | None
```

`build_plan` must finish all validation before returning changes. If any
conflict exists, return no applicable changes so callers cannot partially apply
an invalid plan.

- [ ] **Step 4: Run copy-operation tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.CopyPlanningTests -v
```

Expected: all `CopyPlanningTests` pass.

- [ ] **Step 5: Write failing managed-block tests**

Cover absent files, append to LF and CRLF files, unchanged outside content,
equivalent block adoption, safe block update, modified block content, duplicate,
partial, reversed, and nested markers.

```python
def test_managed_block_preserves_crlf_outside_the_block(self) -> None:
    before = b"# Project\r\n\r\nKeep me.\r\n"
    result = merge_managed_block(before, b"Managed\n", "agent-handoff-toolkit")
    self.assertTrue(result.startswith(before))
    self.assertNotIn(b"Keep me.\n", result.replace(b"\r\n", b""))
    self.assertIn(b"<!-- agent-handoff-toolkit:start -->\r\n", result)
```

- [ ] **Step 6: Run managed-block tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.ManagedBlockTests -v
```

Expected: missing managed-block behavior.

- [ ] **Step 7: Implement managed-block transformation**

Expose a pure operation:

```python
def merge_managed_block(
    current: bytes | None,
    desired_body: bytes,
    block_id: str,
    installed_hash: str | None = None,
) -> bytes:
    ...
```

Hash only the normalized complete marked block for ownership. Preserve all
outside bytes and use the first existing newline convention, defaulting to LF.

- [ ] **Step 8: Run managed-block tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.ManagedBlockTests -v
```

Expected: all `ManagedBlockTests` pass.

- [ ] **Step 9: Write failing JSON ownership tests**

Cover recursive object preservation, ordered append, exact-entry adoption,
duplicate suppression, same-identity collision, malformed JSON, scalar root,
safe owned-fragment advancement, missing owned entries, locally edited owned
entries, and unrelated edits after install.

```python
def test_same_matcher_with_different_hook_is_a_collision(self) -> None:
    current = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write|Edit|MultiEdit", "hooks": [{"command": "legacy"}]}
            ]
        }
    }
    with self.assertRaisesRegex(OperationConflict, "same identity"):
        merge_json_fragment(
            current,
            self.toolkit_claude_fragment,
            identities={"/hooks/PostToolUse": ("matcher",)},
        )
```

- [ ] **Step 10: Run JSON tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.JsonMergeTests -v
```

Expected: missing JSON merge behavior.

- [ ] **Step 11: Implement JSON fragment transformation**

Expose a pure operation:

```python
def merge_json_fragment(
    current: object,
    desired: object,
    identities: Mapping[str, tuple[str, ...]],
    owned: object | None = None,
) -> object:
    ...
```

For sync, remove or replace only exact owned entries. A mismatched identity is a
conflict. Preserve existing dictionary insertion order and list order; append
new owned entries after existing unrelated entries. Serialize the resulting
target with two-space indentation, UTF-8, and its existing newline convention.

- [ ] **Step 12: Run JSON tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.JsonMergeTests -v
```

Expected: all `JsonMergeTests` pass.

- [ ] **Step 13: Write failing whole-plan and diff tests**

Assert install/state preconditions, sync/state preconditions, state planned last,
all-or-nothing conflicts, deterministic target ordering, concise statuses, and
literal unified-diff headers without absolute paths.

```python
def test_conflict_suppresses_every_planned_write(self) -> None:
    repo = self.make_consumer(conflicting_contract=True)
    plan = build_plan(self.source_v2, repo, "v0.2.0", "install")
    self.assertTrue(plan.conflicts)
    self.assertEqual(plan.changes, ())
    self.assertFalse((repo / ".agent-handoff-toolkit" / "install-state.json").exists())
```

- [ ] **Step 14: Run whole-plan tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.PlanTests -v
```

Expected: incomplete orchestration or rendering failures.

- [ ] **Step 15: Complete whole-plan orchestration and rendering**

Build the deterministic next `InstalledState` from successfully planned target
results. Render only repository-relative path headers:

```python
def render_plan(plan: InstallPlan) -> str:
    lines = [f"{plan.operation}: {plan.release}"]
    for conflict in plan.conflicts:
        lines.append(f"CONFLICT {conflict.target}: {conflict.code}: {conflict.message}")
    for change in plan.changes:
        lines.append(f"CHANGE {change.relative_target}")
        lines.extend(_unified_diff(change))
    if not plan.conflicts and not plan.changes:
        lines.append("CURRENT")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 16: Run Task 2 tests and full regression suite**

Run:

```powershell
python -m unittest tests.test_installer -v
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 17: Commit Task 2**

```powershell
git add src/agent_handoff_toolkit/operations.py src/agent_handoff_toolkit/installer.py tests/test_installer.py
git commit -m "feat: plan managed repository updates"
```

---

### Task 3: Atomic application and command-line interface

**Files:**
- Modify: `src/agent_handoff_toolkit/installer.py`
- Modify: `src/agent_handoff_toolkit/cli.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: Task 2 `InstallPlan` and exact captured `before` bytes.
- Produces: `apply_plan(plan: InstallPlan) -> None`
- Produces CLI handlers for the four required modes and exit codes.

- [ ] **Step 1: Write failing apply and rollback tests**

Cover atomic creation/replacement, parent creation, state-last ordering,
precondition changes between plan and apply, rollback of created and replaced
files, rollback failure reporting, read-only/locked write failures where the host
supports them, and no temporary-file residue.

```python
def test_apply_refuses_when_target_changed_after_planning(self) -> None:
    plan = build_plan(self.source_v2, self.consumer, "v0.2.0", "install")
    target = self.consumer / "AGENTS.md"
    target.write_text("concurrent edit\n", encoding="utf-8")
    with self.assertRaisesRegex(ApplyError, "changed after planning"):
        apply_plan(plan)
    self.assertEqual(target.read_text(encoding="utf-8"), "concurrent edit\n")
    self.assertFalse((self.consumer / STATE_PATH).exists())
```

- [ ] **Step 2: Run apply tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.ApplyTests -v
```

Expected: `apply_plan` is missing.

- [ ] **Step 3: Implement atomic apply and rollback**

Precheck every target before the first write and recheck each target immediately
before its own write. Write sibling temporary files with exclusive creation,
flush and close them, then `os.replace`. Track original bytes and original
absence. Apply the planned state change last. If any write fails, process prior
targets in reverse order and restore only a target whose bytes still equal this
run's planned output. Preserve and report a different current value. Combine the
primary and rollback errors in `ApplyError` without hiding either.

```python
class ApplyError(RuntimeError):
    pass

def apply_plan(plan: InstallPlan) -> None:
    if plan.conflicts:
        raise ApplyError("cannot apply a plan with conflicts")
    _verify_preconditions(plan.changes)
    applied: list[FileChange] = []
    try:
        for change in plan.changes:
            _verify_change_precondition(change)
            _atomic_replace(change.target, change.after)
            applied.append(change)
    except OSError as error:
        rollback_errors = _rollback(applied)
        raise ApplyError(_format_apply_failure(error, rollback_errors)) from error
```

- [ ] **Step 4: Run apply tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.ApplyTests -v
```

Expected: all `ApplyTests` pass.

- [ ] **Step 5: Write failing CLI subprocess tests**

Use real subprocesses against temporary source and consumer trees. Assert exact
exit-code semantics, required mutually exclusive flags, default source-root
inference, release mismatch refusal, dry-run/check zero-write behavior, apply
state creation, sync current, and errors on conflict.

```python
def test_sync_check_returns_one_for_safe_available_changes(self) -> None:
    self.run_cli("install", "--apply", release="v0.2.0", source=self.source_v2)
    result = self.run_cli("sync", "--check", release="v0.2.1", source=self.source_v21)
    self.assertEqual(result.returncode, 1, result.stderr)
    self.assertIn("CHANGE ", result.stdout)
```

- [ ] **Step 6: Run CLI tests and witness RED**

Run:

```powershell
python -m unittest tests.test_installer.CliTests -v
```

Expected: argparse rejects the new commands.

- [ ] **Step 7: Add lazy-loaded CLI commands**

Extend `_parser` without importing installer modules at module import time.
Import inside the install/sync branch so the vendored hook runtime remains
usable even if installer-only modules are not distributed.

```python
for command in ("install", "sync"):
    subparser = subparsers.add_parser(command)
    subparser.add_argument("--target", type=Path, required=True)
    subparser.add_argument("--source-root", type=Path)
    subparser.add_argument("--release", required=True)

install_mode = install.add_mutually_exclusive_group(required=True)
install_mode.add_argument("--dry-run", action="store_true")
install_mode.add_argument("--apply", action="store_true")

sync_mode = sync.add_mutually_exclusive_group(required=True)
sync_mode.add_argument("--check", action="store_true")
sync_mode.add_argument("--apply", action="store_true")
```

Map errors to exit `2`. For `sync --check`, return `1` only when the plan is
conflict-free and has changes. Print the plan before any apply attempt.

- [ ] **Step 8: Run CLI tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_installer.CliTests -v
```

Expected: all `CliTests` pass.

- [ ] **Step 9: Run Task 3 and full regression verification**

Run:

```powershell
python -m unittest tests.test_installer -v
python -m unittest discover -s tests -v
python -m compileall -q src tests distribution
```

Expected: all commands exit `0`.

- [ ] **Step 10: Commit Task 3**

```powershell
git add src/agent_handoff_toolkit/installer.py src/agent_handoff_toolkit/cli.py tests/test_installer.py
git commit -m "feat: apply managed updates atomically"
```

---

### Task 4: V2 distribution, installed-layout verification, and documentation

**Files:**
- Create: `distribution/consumer-instructions.md`
- Modify: `distribution/manifest.json`
- Modify: `tests/test_distribution.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/consumer-integration.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: all Task 1–3 modules and CLI behavior.
- Produces: manifest v2 payload, canonical dual-host managed block, v0.2.0 version metadata, and documented pilot commands.

- [ ] **Step 1: Write failing distribution and installed-layout tests**

Extend the current real-layout tests to require manifest version 2, toolkit
version `0.2.0`, correct normalized source hashes, managed blocks for both agent
files, JSON identity metadata, deterministic state, successful installed hook and
record commands, and absence of installer-only imports during those commands.

Add an end-to-end temporary consumer flow:

```python
def test_manifest_install_then_sync_check_is_current(self) -> None:
    result = run_source_cli("install", "--apply", target=self.consumer, release="v0.2.0")
    self.assertEqual(result.returncode, 0, result.stderr)
    check = run_source_cli("sync", "--check", target=self.consumer, release="v0.2.0")
    self.assertEqual(check.returncode, 0, check.stderr)
    run_installed_cli(self.consumer, "validate", "handoffs/templates/continuation.md")
```

- [ ] **Step 2: Run distribution tests and witness RED**

Run:

```powershell
python -m unittest tests.test_distribution -v
```

Expected: v1 manifest/version and missing managed instruction source failures.

- [ ] **Step 3: Add the canonical consumer instruction block**

Write concise host-neutral policy that points to the installed contract and
skill, declares the contract the shared minimum for new records, preserves
stricter project overlays, and makes record semantics/final-tail behavior
canonical. Include the exact installed validation and tail commands.

```markdown
Read `docs/agent-handoff/contract.md` and the installed `agent-handoff` skill
before creating or changing a continuation or completion audit.

- Use a continuation only when authorized work remains.
- Create a completion audit when the highest authorized scope completes.
- Resolve every question that gates the next action before finalizing a continuation.
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
```

- [ ] **Step 4: Advance the manifest and versions**

Set `manifest_version` to `2`, `toolkit_version` to `0.2.0`, and
`text_hash` to `utf8-lf-sha256-v1`. Add managed-block artifacts for
`AGENTS.md` and `CLAUDE.md`, JSON array identities for Claude
`/hooks/PostToolUse` by `matcher`, and every updated runtime source hash.
Keep `__version__` and `pyproject.toml` at `0.2.0`.

- [ ] **Step 5: Update consumer and development documentation**

Replace the future-tense v1 boundary with exact release-checkout commands,
exit-code meanings, ownership/conflict semantics, historical-record policy,
generic mature-policy and minimal-policy pilot notes, and rollback limitations.
Keep `AGENTS.md` and `CLAUDE.md` semantically identical and record any resolved
implementation warning required by repository policy.

- [ ] **Step 6: Run distribution tests and witness GREEN**

Run:

```powershell
python -m unittest tests.test_distribution -v
```

Expected: all distribution and installed-layout tests pass.

- [ ] **Step 7: Run complete local verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m ruff check src tests distribution
python -m ruff format --check src tests distribution
python -m compileall -q src tests distribution
python -m build
git diff --check
```

Expected: every command exits `0`, with no warnings that indicate a project
failure.

- [ ] **Step 8: Inspect the final diff and tracked-file set**

Run:

```powershell
git status --short
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
```

Confirm only public generic toolkit material is present, generated package
artifacts are ignored, and no private pilot content or secrets appear.

- [ ] **Step 9: Commit Task 4**

```powershell
git add distribution/consumer-instructions.md distribution/manifest.json tests/test_distribution.py pyproject.toml README.md docs/consumer-integration.md AGENTS.md CLAUDE.md src/agent_handoff_toolkit/__init__.py
git commit -m "feat: publish managed distribution v0.2"
```

---

### Task 5: Whole-branch review and release-readiness audit

**Files:**
- Create after verification: `handoffs/audit-records/2026-09-06-managed-install-sync-v0.2.0-audit.md`
- Modify only if review findings require fixes: files from Tasks 1–4 and their tests.

**Interfaces:**
- Consumes: the complete branch diff and toolkit validator.
- Produces: independent review approval and a canonical completion audit for the toolkit feature scope.

- [ ] **Step 1: Request an independent whole-branch review**

Provide the reviewer the approved spec, this plan, merge-base SHA, head SHA,
complete diff package, and any deferred findings or rulings. Require separate
spec-compliance and code-quality verdicts, with security attention to path
containment, ownership, stale plans, rollback, and accidental data disclosure.

- [ ] **Step 2: Resolve all Critical and Important findings**

Dispatch one fix implementer with the complete findings list, require focused
RED/GREEN evidence, then dispatch one scoped re-review. Do not merge with open
Critical or Important findings.

- [ ] **Step 3: Re-run the complete verification suite**

Run the exact Task 4 Step 7 commands against the final reviewed tree.

- [ ] **Step 4: Create and validate the completion audit**

Render a `completion-audit` record covering the installer/sync feature. Record
the exact commands and observed results, review outcome, release state, and
explicit remaining-code answers for the toolkit unit and the broader consumer
rollout. The audit must not contain a continuation prompt.

Run:

```powershell
python distribution/runner.py validate handoffs/audit-records/2026-09-06-managed-install-sync-v0.2.0-audit.md
```

Expected: `valid:` and exit `0`.

- [ ] **Step 5: Commit the audit**

```powershell
git add handoffs/audit-records/2026-09-06-managed-install-sync-v0.2.0-audit.md
git commit -m "docs: audit managed installer release"
```

- [ ] **Step 6: Hand off for branch integration**

Use the repository's authorized pull-request workflow. The release tag is not
created until the merged `main` commit has passed the Python 3.11–3.14 CI matrix.
