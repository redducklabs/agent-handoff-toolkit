# Stop-Hook Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce schema-v2 authorization lineage and canonical terminal responses with synchronous Claude Code and Codex `Stop` hooks that return compact corrective evidence to the same model session.

**Architecture:** Keep v1 parsing compatible, add focused v2 lineage and lifecycle modules, persist repository-wide authorization chains and per-session state in one locked atomic envelope, and adapt normalized core decisions to each host's hook protocol. Successful lifecycle hooks are silent; tracked failures block, deduplicate feedback, and circuit-break with a visible policy failure.

**Tech Stack:** Python 3.11+ standard library, `unittest`, JSON/Markdown records, Claude Code and Codex command hooks, setuptools 84.0.0, build 1.6.1, Ruff 0.16.7.

**Spec:** `docs/superpowers/specs/2026-09-10-stop-hook-enforcement-design.md`

## Global Constraints

- Preserve v1 validation/rendering compatibility and never scan or rewrite deprecated historical records.
- The core package remains independent of Claude Code, Codex, GitHub, tracker, deployment, and model APIs.
- Informational hooks fail open; tracked `UserPromptSubmit`, `PreToolUse`, and `Stop` lifecycle enforcement fails closed.
- A `Stop` hook cannot retract an already displayed assistant message; it must force corrective continuation or produce a visible policy failure.
- Store no prompt, reply, transcript, credential, secret, or customer content in records, fixtures, runtime state, or logs.
- Successful lifecycle checks emit no model-visible output.
- Optional semantic evaluators are not implemented in this release; any future evaluator cannot authorize transitions and must route simple checks to the cheapest configured model that passes its evaluation threshold.
- Schema-v2 terminal continuation, audit, and decision responses equal the renderer output byte-for-byte after CRLF-to-LF normalization; no handwritten preamble is allowed.
- Record and lifecycle IDs match `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`; digests/HMACs are 64 lowercase hexadecimal characters.
- `scope_definition.title` is at most 160 UTF-8 bytes; `scope_definition.outcome` is at most 1,000 UTF-8 bytes; decision questions and reasons are each at most 400 UTF-8 bytes; hook feedback is at most 1,200 UTF-8 bytes.
- Use only standard-library dependencies in the installed runtime.
- GitHub Actions jobs use `runs-on: redducklabs-runners` as required by repository policy.
- Update `AGENTS.md` and `CLAUDE.md` with byte-identical repository policy text.
- Use `apply_patch` for source edits, never write `.env` files, and never add prompts, replies, or transcripts to fixtures or logs.
- Release version: `0.3.0`; record schema version: `2`; install-state format remains version `1`; source-manifest format remains version `2`.
- Verified toolchain on 2026-09-10: Python 3.14.7 latest stable with supported floor 3.11; CI Python 3.11–3.14; setuptools 84.0.0; build 1.6.1; Ruff 0.16.7; `actions/checkout@v7`; `actions/setup-python@v7`.

---

### Task 1: Canonical lineage primitives

**Files:**
- Create: `src/agent_handoff_toolkit/lineage.py`
- Create: `tests/test_lineage.py`

**Interfaces:**
- Consumes: Python standard-library `hashlib`, `hmac`, `json`, `re`, and `unicodedata`.
- Produces: `canonical_json_bytes(value: object) -> bytes`, `normalize_text(value: str, *, limit: int, label: str) -> str`, `validate_identifier(value: object, *, label: str) -> str`, `validate_hex_digest(value: object, *, label: str) -> str`, `scope_definition_digest(scope: Mapping[str, object]) -> str`, `record_digest(text: str) -> str`, `evidence_hmac(secret: bytes, payload: Mapping[str, object]) -> str`, and `LineageError`.

- [ ] **Step 1: Write failing canonicalization and validation tests**

```python
class CanonicalLineageTests(unittest.TestCase):
    def test_scope_digest_is_key_order_independent_but_text_sensitive(self) -> None:
        scope = {
            "scope_id": "issue-1323",
            "scope_kind": "issue",
            "parent_scope_id": None,
            "scope_definition": {"title": "Florida admission", "outcome": "Ship all authorized work."},
        }
        reordered = {**scope, "scope_definition": {"outcome": "Ship all authorized work.", "title": "Florida admission"}}
        self.assertEqual(scope_definition_digest(scope), scope_definition_digest(reordered))
        changed = copy.deepcopy(scope)
        changed["scope_definition"]["outcome"] += " "
        self.assertNotEqual(scope_definition_digest(scope), scope_definition_digest(changed))

    def test_identifier_digest_and_text_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(LineageError, "identifier"):
            validate_identifier("bad id", label="record identifier")
        with self.assertRaisesRegex(LineageError, "digest"):
            validate_hex_digest("A" * 64, label="record digest")
        with self.assertRaisesRegex(LineageError, "unsafe"):
            normalize_text("bad\u2028text", limit=100, label="title")
```

- [ ] **Step 2: Run the focused tests and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_lineage.py" -v`

Expected: FAIL because `agent_handoff_toolkit.lineage` does not exist.

- [ ] **Step 3: Implement the minimal canonical primitives**

```python
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")

def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")

def scope_definition_digest(scope: Mapping[str, object]) -> str:
    payload = {
        "parent_scope_id": scope.get("parent_scope_id"),
        "scope_definition": validate_scope_definition(scope.get("scope_definition")),
        "scope_id": validate_identifier(scope.get("scope_id"), label="scope identifier"),
        "scope_kind": scope.get("scope_kind"),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

def record_digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()
```

`validate_scope_definition` must require exactly `title` and `outcome`, apply the global byte limits, and reject non-string, blank, untrimmed, CR-containing, or unsafe-control values. `evidence_hmac` must use `hmac.new(secret, canonical_json_bytes(payload), hashlib.sha256).hexdigest()` and reject an empty secret.

- [ ] **Step 4: Add digest/HMAC determinism and Unicode tests**

Test UTF-8 preservation, LF normalization for record bytes, rejection of NaN/non-finite JSON, exact 128-character ID acceptance, 129-character rejection, and distinct HMAC results for different local secrets.

- [ ] **Step 5: Run focused tests, Ruff, formatting, and whitespace checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_lineage.py" -v
python -m ruff check src/agent_handoff_toolkit/lineage.py tests/test_lineage.py
python -m ruff format --check src/agent_handoff_toolkit/lineage.py tests/test_lineage.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 6: Commit the lineage primitives**

```powershell
git add src/agent_handoff_toolkit/lineage.py tests/test_lineage.py
git commit -m "feat: add authorization lineage primitives"
```

---

### Task 2: Schema-v2 records and successor validation

**Files:**
- Modify: `src/agent_handoff_toolkit/records.py`
- Modify: `src/agent_handoff_toolkit/__init__.py`
- Modify: `tests/test_records.py`
- Modify: `tests/test_lineage.py`

**Interfaces:**
- Consumes: Task 1 lineage primitives.
- Produces: v1/v2 schema dispatch; `validate_successor(candidate: Mapping[str, object], predecessor: Mapping[str, object], *, expected_predecessor_path: str, predecessor_sha256: str, approved_transition_hmac: str | None = None) -> list[ValidationIssue]`; `render_terminal_response(record_path: str | os.PathLike[str], text: str) -> str`; public exports for `record_digest`, `scope_definition_digest`, `validate_successor`, and `render_terminal_response`.

- [ ] **Step 1: Add failing v2 field-shape tests**

Build v2 dictionaries in test helper functions rather than committed transcript/prompt fixtures. Require this exact record-level shape:

```python
scope = {
    "scope_id": "issue-1323",
    "scope_kind": "issue",
    "parent_scope_id": None,
    "highest_authorized": True,
    "scope_definition": {
        "title": "Issue 1323",
        "outcome": "Complete all authorized issue work.",
    },
    "remaining_work": True,
    "remaining_code": True,
    "remaining_code_detail": "Implementation remains.",
    "status": "in-progress",
}
scope["scope_definition_digest"] = scope_definition_digest(scope)
record = {
    "schema_version": 2,
    "record_type": "continuation",
    "record_id": "record-002",
    "authorization_id": "auth-001",
    "authorized_root_scope_id": "issue-1323",
    "predecessor": {
        "record_id": "record-001",
        "path": "D:/repo/handoffs/record-001.md",
        "sha256": "0" * 64,
    },
    "authorization_evidence": {
        "kind": "initial-user-turn",
        "user_turn_ref": "turn-user-001",
        "proposal_turn_ref": None,
        "evidence_hmac": "1" * 64,
    },
    "transition": None,
    "active_scopes": [scope],
    # existing timestamp, verification, exact_action, next_session_gates,
    # next_session_prompt, and sections fields remain required by record type
}
```

Test unknown/missing lineage fields, invalid IDs/digests, root mismatch, bad scope-definition digest, non-null initial predecessor in a first record, and malformed transition/evidence objects. Keep current v1 fixtures passing unchanged.

- [ ] **Step 2: Run focused records tests and observe schema-version failures**

Run: `python -m unittest discover -s tests -p "test_records.py" -v`

Expected: new v2 cases FAIL because only schema version 1 is accepted.

- [ ] **Step 3: Add schema dispatch and v2 validation**

Keep `SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})` and route existing behavior unchanged for v1. Add `_validate_v2_lineage_fields` and `_validate_v2_scopes`; do not weaken `_validate_scopes` or v1 type-field checks.

- [ ] **Step 4: Add failing successor-invariant tests**

Cover direct predecessor ID/path/digest equality, authorization ID preservation, inherited scope order and presence, immutable scope fields/digests, approved transition HMAC, atomic old/new authorization IDs, and locked-root completion. Include the exact regressions:

```python
def test_successor_rejects_invented_narrower_completed_root(self) -> None:
    predecessor = make_v2_continuation(root="issue-1323", children=("task-7a",))
    audit = make_v2_audit(root="local-task-7a", predecessor=predecessor)
    predecessor_sha = record_digest(render_record(predecessor))
    self.assertIn("lineage-root", {issue.code for issue in validate_successor(audit, predecessor, expected_predecessor_path=PATH, predecessor_sha256=predecessor_sha)})

def test_successor_rejects_same_id_with_changed_scope_meaning(self) -> None:
    candidate = successor_of(predecessor)
    candidate["active_scopes"][0]["scope_definition"]["outcome"] = "Only finish the local unit."
    candidate["active_scopes"][0]["scope_definition_digest"] = scope_definition_digest(candidate["active_scopes"][0])
    predecessor_sha = record_digest(render_record(predecessor))
    self.assertIn("lineage-definition", issue_codes(validate_successor(candidate, predecessor, expected_predecessor_path=PATH, predecessor_sha256=predecessor_sha)))
```

- [ ] **Step 5: Implement `validate_successor`**

Validate structure first; compare canonical normalized absolute paths without resolving/opening them inside the pure function; compare the candidate's predecessor digest to required `predecessor_sha256` computed by the caller from the original LF-normalized predecessor record bytes; require unfinished inherited scopes to remain; permit immutable changes only when the candidate transition HMAC equals the trusted `approved_transition_hmac`; require a completion audit's completed root to equal the predecessor's locked root.

- [ ] **Step 6: Add v2 whole-response rendering tests**

`render_terminal_response` must return the entire permitted assistant response. For v2, use the existing generated continuation/audit copy with no caller-supplied preamble. Preserve `render_tail` for v1 compatibility. Test CRLF normalization, path escaping, no trailing content, no-action rejection, and exact final link.

- [ ] **Step 7: Implement and export v2 terminal rendering**

```python
def render_terminal_response(record_path: str | os.PathLike[str], text: str) -> str:
    data = parse_markdown(text)
    if data["schema_version"] == 1:
        return render_tail(record_path, text)
    return _render_v2_complete_response(data, _absolute_markdown_path(record_path))
```

Do not accept a summary parameter in v2. Any future summary must be metadata-derived by the renderer.

- [ ] **Step 8: Run record/lineage suites and static checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_lineage.py" -v
python -m unittest discover -s tests -p "test_records.py" -v
python -m ruff check src/agent_handoff_toolkit/records.py src/agent_handoff_toolkit/lineage.py tests/test_records.py tests/test_lineage.py
python -m ruff format --check src/agent_handoff_toolkit/records.py src/agent_handoff_toolkit/lineage.py tests/test_records.py tests/test_lineage.py
git diff --check
```

Expected: all commands exit `0`; v1 fixtures still validate and render byte-equivalent output.

- [ ] **Step 9: Commit schema v2**

```powershell
git add src/agent_handoff_toolkit/records.py src/agent_handoff_toolkit/__init__.py tests/test_records.py tests/test_lineage.py
git commit -m "feat: validate schema v2 lineage"
```

---

### Task 3: Pure lifecycle state machine and decision responses

**Files:**
- Create: `src/agent_handoff_toolkit/lifecycle.py`
- Create: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `ValidationIssue`, `validate_successor`, and `render_terminal_response`.
- Produces: enums `EventName`, `EnforcementMode`, `DecisionKind`, `AuthorityCategory`, and `AffirmationResult`; frozen models `NormalizedEvent`, `LifecycleIssue`, `LifecycleDecision`, `LifecycleMutation`, `DecisionRequest`, `RecordReference`, `AuthorizationProposal`, `ChainState`, `SessionState`, `LifecycleSnapshot`, and `TerminalCandidate`; pure functions `classify_affirmation`, `render_decision_response`, `issue_signature`, `evaluate_pre_tool`, `evaluate_user_prompt`, and `evaluate_stop`. `ChainState.current_record_reference` is `RecordReference | None`; approved-but-unpublished transition proof resides in `ChainState.publication_evidence`, not session proposal state.

- [ ] **Step 1: Write failing model and affirmation tests**

```python
class AffirmationTests(unittest.TestCase):
    def test_accepts_bounded_unambiguous_adjacent_affirmatives(self) -> None:
        for value in ("yes", "yeah, go with 2", "looks right", "approved"):
            self.assertEqual(classify_affirmation(value), AffirmationResult.APPROVE)

    def test_rejects_negative_qualified_or_unbounded_text(self) -> None:
        self.assertEqual(classify_affirmation("yes, but change the root"), AffirmationResult.AMBIGUOUS)
        self.assertEqual(classify_affirmation("no"), AffirmationResult.REJECT)
```

The classifier is deterministic, normalized, conservative, and the sole transition-approval gate. No model API appears in this module.

- [ ] **Step 2: Run focused tests and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_lifecycle.py" -v`

- [ ] **Step 3: Implement frozen lifecycle models and validation**

Use exact decision categories:

```python
class AuthorityCategory(str, Enum):
    MISSING_INPUT = "missing-input"
    MUTUALLY_EXCLUSIVE_CHOICE = "mutually-exclusive-choice"
    EXTERNAL_EFFECT = "external-effect"
    DESTRUCTIVE_OPERATION = "destructive-operation"
    REPOSITORY_APPROVAL = "repository-approval"
```

`NormalizedEvent` includes host, event, session key, turn reference, repository root, transcript reference, `stop_hook_active`, latest assistant message, current user message and reference, tool name, bounded tool input, and capability. Raw message fields are transient and excluded from every serialization method.

`evaluate_user_prompt` accepts keyword-only `affirmation: AffirmationResult | None = None` and `decision_resolved: bool = False`. A real external user turn resets the correction counter, but only explicit trusted `decision_resolved=True` clears a pending decision; Task 6 supplies that evidence.

- [ ] **Step 4: Write failing decision-request tests**

Require a stable ID, authorization ID, category, one blocked exact-action field/value, question HMAC, and reason HMAC. Reject questions offering only continue/stop/handoff. Verify `render_decision_response(question, request)` emits one canonical question response and that `verify_decision_response(message, request, secret)` compares its HMAC without persisting question text.

- [ ] **Step 5: Implement decision request rendering and verification**

Use the exact rendered form:

```text
Authorized work is paused for one required user decision.

Decision needed: {question}
Blocked action field: {blocked_action_field}
Reason: {reason}
```

The renderer rejects extra text and enforces the global byte limits.

- [ ] **Step 6: Write failing four-outcome and response-regression tests**

Cover:

- unfinished executable work without a decision/record blocks with `AHK-STOP-WORK`;
- valid decision response allows and retains the locked root;
- exact valid continuation response allows and advances the record;
- exact valid locked-root audit allows and completes the chain;
- handwritten or contradictory text before a valid tail blocks with `AHK-STOP-RESPONSE`;
- invented child/root audit blocks with `AHK-STOP-ROOT`;
- stale revision blocks with `AHK-STOP-STALE`.

- [ ] **Step 7: Implement the pure event evaluators**

`evaluate_stop` receives an already containment-checked `TerminalCandidate`; it performs no file I/O. Return `LifecycleDecision(ALLOW)` with a proposed immutable state mutation, or `BLOCK/POLICY_FAILURE` with stable issues and no mutation.

- [ ] **Step 8: Add failure-deduplication and circuit tests**

Prove the issue signature uses authorization ID, targeted chain revision, and sorted issue codes; prove cosmetic evidence changes cannot reset the correction count; prove only a compliant stop or external user turn resets it; prove the third blocked stop returns `POLICY_FAILURE`; and prove `stop_hook_active` cannot bypass validation.

- [ ] **Step 9: Run lifecycle/record suites and static checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_lifecycle.py" -v
python -m unittest discover -s tests -p "test_records.py" -v
python -m unittest discover -s tests -p "test_lineage.py" -v
python -m ruff check src/agent_handoff_toolkit/lifecycle.py src/agent_handoff_toolkit/records.py src/agent_handoff_toolkit/lineage.py tests/test_lifecycle.py tests/test_records.py tests/test_lineage.py
python -m ruff format --check src/agent_handoff_toolkit/lifecycle.py src/agent_handoff_toolkit/records.py src/agent_handoff_toolkit/lineage.py tests/test_lifecycle.py tests/test_records.py tests/test_lineage.py
git diff --check
```

- [ ] **Step 10: Commit the pure lifecycle engine**

```powershell
git add src/agent_handoff_toolkit/lifecycle.py tests/test_lifecycle.py
git commit -m "feat: add lifecycle enforcement state machine"
```

---

### Task 4: Atomic authorization registry storage

**Files:**
- Create: `src/agent_handoff_toolkit/lifecycle_storage.py`
- Create: `tests/test_lifecycle_storage.py`

**Interfaces:**
- Consumes: Task 3 frozen lifecycle state models and Task 1 canonical JSON/HMAC validation.
- Produces: `LifecycleStorageError`, `StaleLifecycleState`, `RegistryEnvelope`, `LocalLifecycleStorage`, `resolve_lifecycle_state_root(repo_root: Path) -> Path`, `load_snapshot(session_key: str) -> LifecycleSnapshot`, `load_chain(authorization_id: str) -> ChainState | None`, and `compare_and_swap(session_key: str, expected_chain_revision: int, expected_session_revision: int, mutation: LifecycleMutation) -> LifecycleSnapshot`.

- [ ] **Step 1: Write failing deterministic envelope tests**

Assert exact keys `registry_version`, `revision`, `chains`, and `sessions`; `registry_version == 1`; sorted map keys; no unknown fields; finite JSON only; and absence of known prompt/reply/transcript sentinel strings from rendered bytes.

- [ ] **Step 2: Run the storage suite and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_lifecycle_storage.py" -v`

- [ ] **Step 3: Implement state-root resolution**

Resolve linked worktrees with read-only `git rev-parse --git-common-dir`; verify the result exists and is a directory before using `{common_git_dir}/agent-handoff-toolkit/lifecycle`. Fall back to `%LOCALAPPDATA%/agent-handoff-toolkit/state/{repository_hmac}` on Windows and `$XDG_STATE_HOME/agent-handoff-toolkit/{repository_hmac}` or the standard user-state directory on POSIX. Never use an unresolved environment variable as a destructive target.

- [ ] **Step 4: Write failing lock/CAS/atomicity tests**

Use temporary repositories and multiprocessing workers. Cover one-envelope atomic replacement; per-chain/session CAS; global storage revision increment; unrelated-chain updates after a fresh reload; same-chain stale rejection; temp cleanup; corrupt state; symlink/junction escapes; missing parents; and interrupted publication preserving the old valid envelope.

- [ ] **Step 5: Implement one-envelope locked transactions**

Use `fcntl.flock` on POSIX and `msvcrt.locking` on Windows behind `_exclusive_lock`. Under the lock: reread and validate the current envelope, compare only targeted chain/session revisions, apply the pure mutation, validate/render the entire new envelope, fsync the temporary file, replace atomically, and fsync the parent where supported.

- [ ] **Step 6: Add leases, joins, supersession, and completion tests**

Prove leases do not expire by time; explicit join adds rather than replaces a lease; complete chains reject joins; transitions atomically supersede the old chain and create the new chain; old sessions receive successor reconciliation state; and compliant continuation/audit stops update or release their session lease.

- [ ] **Step 7: Add local-secret and HMAC privacy tests**

Create the HMAC secret with cryptographically secure randomness and owner-only permissions where supported. Assert state contains HMACs but no input strings; different installations produce different HMACs; malformed secrets fail closed everywhere; insecure POSIX mode bits fail closed; Windows verifies containment, non-link/reparse safety, and exclusive creation without claiming POSIX-mode equivalence.

- [ ] **Step 8: Run storage/lifecycle suites and static checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_lifecycle_storage.py" -v
python -m unittest discover -s tests -p "test_lifecycle.py" -v
python -m ruff check src/agent_handoff_toolkit/lifecycle_storage.py src/agent_handoff_toolkit/lifecycle.py tests/test_lifecycle_storage.py tests/test_lifecycle.py
python -m ruff format --check src/agent_handoff_toolkit/lifecycle_storage.py src/agent_handoff_toolkit/lifecycle.py tests/test_lifecycle_storage.py tests/test_lifecycle.py
python -m compileall -q src/agent_handoff_toolkit/lifecycle_storage.py src/agent_handoff_toolkit/lifecycle.py tests/test_lifecycle_storage.py tests/test_lifecycle.py
git diff --check
```

- [ ] **Step 9: Commit lifecycle storage**

```powershell
git add src/agent_handoff_toolkit/lifecycle_storage.py tests/test_lifecycle_storage.py
git commit -m "feat: persist authorization lifecycle state"
```

---

### Task 5: Lifecycle operations and safe bootstrap commands

**Files:**
- Create: `src/agent_handoff_toolkit/lifecycle_operations.py`
- Create: `tests/test_lifecycle_operations.py`
- Modify: `src/agent_handoff_toolkit/cli.py`
- Modify: `src/agent_handoff_toolkit/lifecycle.py`
- Modify: `src/agent_handoff_toolkit/lifecycle_storage.py`
- Modify: `tests/test_lifecycle.py`
- Modify: `tests/test_lifecycle_storage.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `inspect`, `register_root`, `resume`, `join`, `propose_transition`, `request_decision`, and `adopt_v1` service functions; `BootstrapCommand` and `parse_bootstrap_command(command: str, runner_path: Path, challenge: str) -> BootstrapCommand | None`; nested `handoff-toolkit lifecycle` CLI.

- [ ] **Step 1: Write failing exact bootstrap grammar tests**

Accept only:

```text
python .agent-handoff-toolkit/runner.py lifecycle register-root --challenge challenge-001 --scope-id issue-1323 --scope-kind issue --scope-definition-b64 eyJvdXRjb21lIjoiQ29tcGxldGUgYWxsIGF1dGhvcml6ZWQgaXNzdWUgd29yay4iLCJ0aXRsZSI6Iklzc3VlIDEzMjMifQ --expected-session-revision 0
python .agent-handoff-toolkit/runner.py lifecycle resume --challenge challenge-001 --record D:/repo/handoffs/current.md --expected-session-revision 0
python .agent-handoff-toolkit/runner.py lifecycle join --challenge challenge-001 --authorization-id auth-001 --expected-chain-revision 3 --expected-session-revision 0
python .agent-handoff-toolkit/runner.py lifecycle adopt-v1 --challenge challenge-001 --record D:/repo/handoffs/current-v1.md --expected-session-revision 0
```

Reject `;`, `&&`, `||`, pipes, redirection, substitutions, newlines, extra flags, reordered duplicate flags, alternate runners, overlong values, invalid base64url, and a challenge from another turn.

- [ ] **Step 2: Run focused operations tests and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_lifecycle_operations.py" -v`

- [ ] **Step 3: Implement the parser without invoking a shell**

Tokenize the fixed grammar directly; never call `shell=True`, `eval`, PowerShell, or a host shell parser. Decode canonical base64url JSON and recompute the scope-definition digest inside `register_root`.

- [ ] **Step 4: Write failing registration/resume/join tests**

Cover initiating user-turn HMAC binding, challenge single-use, duplicate root-definition collision, canonical handoff reference only, containment-checked explicit record path, stale revision rejection, join of active chain, join of complete chain rejection, and no scan for a newest handoff.

- [ ] **Step 5: Implement register/resume/join operations through storage CAS**

No operation may mutate the envelope directly. Every operation builds a pure `LifecycleMutation` and calls `compare_and_swap` with targeted revisions.

- [ ] **Step 6: Write failing transition/adoption/decision tests**

Test proposal storage without approval; immediate affirmative consumption; negative/ambiguous/non-adjacent response retention; transition HMAC consumption once; v1 adoption proposal plus adjacent approval; rejection of `--confirmed`; decision request via standard input; and proof that question text is absent from state bytes.

- [ ] **Step 7: Implement transition, adoption, and decision operations**

Transitions preserve immutable old/new definitions and atomically supersede the old authorization. `adopt_v1` opens only the selected record after approval and records that v1 semantics were not mechanically proven. `request_decision` prints the canonical response and stores only HMAC/structured fields.

- [ ] **Step 8: Add CLI parser and exit-code tests**

Every state mutation requires expected revisions/challenge. Explicit lifecycle command validation errors exit `1`; unsafe/runtime state errors exit `2`; success emits bounded JSON without message content. `inspect` returns IDs, revisions, modes, and issue codes only.

- [ ] **Step 9: Run operation/CLI/storage suites and static checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_lifecycle_operations.py" -v
python -m unittest discover -s tests -p "test_lifecycle_storage.py" -v
python -m unittest discover -s tests -p "test_lifecycle.py" -v
python -m ruff check src/agent_handoff_toolkit/lifecycle_operations.py src/agent_handoff_toolkit/cli.py tests/test_lifecycle_operations.py
python -m ruff format --check src/agent_handoff_toolkit/lifecycle_operations.py src/agent_handoff_toolkit/cli.py tests/test_lifecycle_operations.py
python -m compileall -q src/agent_handoff_toolkit/lifecycle_operations.py src/agent_handoff_toolkit/cli.py tests/test_lifecycle_operations.py
git diff --check
```

- [ ] **Step 10: Commit lifecycle operations**

```powershell
git add src/agent_handoff_toolkit/lifecycle_operations.py src/agent_handoff_toolkit/cli.py tests/test_lifecycle_operations.py
git commit -m "feat: add lifecycle control commands"
```

---

### Task 6: Host normalization and fail-closed hook enforcement

**Files:**
- Create: `src/agent_handoff_toolkit/hook_adapters.py`
- Create: `tests/test_hook_adapters.py`
- Modify: `src/agent_handoff_toolkit/hooks.py`
- Modify: `src/agent_handoff_toolkit/cli.py`
- Modify: `tests/test_hooks.py`

**Interfaces:**
- Consumes: lifecycle engine, storage, operations, v2 record validation/rendering.
- Produces: `HookExecution(stdout: str, stderr: str, exit_code: int)`, `normalize_event(platform: str, event: str, payload: Mapping[str, object], repo_root: Path) -> NormalizedEvent`, `render_hook_execution(platform: str, event: EventName, decision: LifecycleDecision) -> HookExecution`, candidate extraction/loading, and `run_hook(platform: str, event: str, raw: str, repo_root: Path, storage: LocalLifecycleStorage | None = None) -> HookExecution`.

- [ ] **Step 1: Write failing Claude/Codex normalization tests**

Use in-test dictionaries, not persisted transcript fixtures. Cover exact required fields for `UserPromptSubmit`, `PreToolUse`, and `Stop`; missing/oversized/wrong-type fields; session/turn hashing; bounded tool input; and `last_assistant_message: null`.

- [ ] **Step 2: Run hook adapter tests and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_hook_adapters.py" -v`

- [ ] **Step 3: Implement normalization and tool capability policy**

Host read-only allowlists are explicit constants. Shell/Bash, file edits, MCP tools, and unknown local tools require tracked state. The sole untracked shell exception is a command accepted by `parse_bootstrap_command`; no general command classifier exists.

- [ ] **Step 4: Write failing pre-tool and user-prompt hook tests**

Verify untracked tool-free/read-only activity is silent; first shell/edit/unknown tool blocks with `AHK-PRE-ROOT` and a one-use registration challenge; malformed bootstrap blocks; valid bootstrap runs; canonical `Continue from handoff:` resumes; paraphrases do not; adjacent affirmatives consume pending proposals; and tracked runtime exceptions block with `AHK-HOOK-RUNTIME`.

- [ ] **Step 5: Implement candidate discovery and containment checks**

Extract exactly one absolute record pointer from the canonical final link/block. Reject zero/multiple pointers, angle/control injection, non-Markdown files, paths outside the repository's `handoffs` tree, symlink/junction escape, and a record ID/digest differing from registry state. Never enumerate the handoff directory.

- [ ] **Step 6: Write failing Stop outcome tests**

For both hosts, verify silent compliant allow; blocking JSON `{"decision":"block","reason":"..."}` for unfinished work, response mismatch, narrowed root, stale state, and runtime errors; same-session corrective continuation; third-block visible policy failure; and no raw message/record content in hook feedback.

- [ ] **Step 7: Implement host-specific hook output**

Codex and Claude blocking continuation use exit `0` with structured JSON. Policy failure uses each documented visible stop shape and must not claim compliant completion. Preserve existing advisory SessionStart/PostToolUse string behavior through a compatibility adapter while changing the CLI to honor `HookExecution.exit_code`.

- [ ] **Step 8: Split exception behavior by event/state**

Remove the blanket lifecycle catch in `cli.py`. Informational hooks continue returning success/no output on malformed input. If state is known tracked or a lifecycle event can safely resolve tracked state from the bounded payload, convert exceptions to a blocking runtime decision. If tracked status cannot be determined because state itself is corrupt, fail closed when a registry/session envelope exists.

- [ ] **Step 9: Run all hook/lifecycle suites and static checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_hook_adapters.py" -v
python -m unittest discover -s tests -p "test_hooks.py" -v
python -m unittest discover -s tests -p "test_lifecycle*.py" -v
python -m ruff check src tests
python -m ruff format --check src tests
git diff --check
```

Expected: all commands exit `0`; existing advisory-hook tests remain green with updated explicit expectations.

- [ ] **Step 10: Commit host enforcement**

```powershell
git add src/agent_handoff_toolkit/hook_adapters.py src/agent_handoff_toolkit/hooks.py src/agent_handoff_toolkit/cli.py tests/test_hook_adapters.py tests/test_hooks.py
git commit -m "feat: enforce lifecycle stop hooks"
```

---

### Task 7: Schema-v2 contract, templates, and agent workflow

**Files:**
- Modify: `docs/agent-handoff/contract.md`
- Modify: `templates/continuation.md`
- Modify: `templates/completion-audit.md`
- Modify: `skills/agent-handoff/SKILL.md`
- Modify: `distribution/consumer-instructions.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_distribution.py`

**Interfaces:**
- Consumes: exact schema/CLI/rendering behavior from Tasks 1–6.
- Produces: normative schema-v2 contract, v2 templates, lifecycle-aware skill instructions, and byte-identical repository policies.

- [ ] **Step 1: Add failing documentation contract assertions**

Assert that contract/skill/templates contain lineage, root immutability, contextual transition evidence, four stop outcomes, exact whole-response equality, transient-display limitation, fail-closed lifecycle events, no semantic-truth overclaim, and no model-required enforcement. Assert `AGENTS.md` and `CLAUDE.md` are byte-identical after LF normalization.

- [ ] **Step 2: Run the distribution tests and observe failures against v1 text**

Run: `python -m unittest discover -s tests -p "test_distribution.py" -v`

- [ ] **Step 3: Rewrite the normative contract for schema v2 with a v1 compatibility appendix**

Replace the unconditional automatic-fail-open sentence with the split policy. State that v2 terminal responses are entirely generated and have no handwritten preamble. Preserve the continuation/completion distinction and explicitly state that provenance is not semantic truth.

- [ ] **Step 4: Update both templates to exact v2 metadata**

Include `record_id`, `authorization_id`, `authorized_root_scope_id`, predecessor, authorization evidence, transition, scope definition/digest, and current v2 derived sections. Keep placeholders bounded and renderable after test materialization. Do not alter the historical audit record.

- [ ] **Step 5: Update the skill and consumer instructions**

Require `lifecycle inspect/register-root/resume/join`, explicit candidate validation, renderer-only final responses, legitimate decision requests, and compliance with corrective `Stop` feedback. Explain that the attempted message may be displayed before correction.

- [ ] **Step 6: Update `AGENTS.md` and `CLAUDE.md` in the same patch**

Use byte-identical wording that informational hooks fail open and tracked lifecycle hooks fail closed. Preserve every unrelated instruction and the Claude CLI warning already recorded.

- [ ] **Step 7: Run documentation/distribution/record tests and whitespace checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_distribution.py" -v
python -m unittest discover -s tests -p "test_records.py" -v
git diff --no-index -- AGENTS.md CLAUDE.md
git diff --check
```

Expected: both test commands exit `0`; `git diff --no-index` emits no differences and exits `0`; whitespace check exits `0`.

- [ ] **Step 8: Commit the schema-v2 workflow documentation**

```powershell
git add docs/agent-handoff/contract.md templates/continuation.md templates/completion-audit.md skills/agent-handoff/SKILL.md distribution/consumer-instructions.md AGENTS.md CLAUDE.md tests/test_distribution.py
git commit -m "docs: define enforced schema v2 workflow"
```

---

### Task 8: Distribution, installer upgrade, and current toolchain pins

**Files:**
- Modify: `adapters/claude/settings.fragment.json`
- Modify: `adapters/codex/hooks.fragment.json`
- Modify: `distribution/manifest.json`
- Modify: `src/agent_handoff_toolkit/__init__.py`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/consumer-integration.md`
- Modify: `tests/test_distribution.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: all installed runtime modules and lifecycle CLI/hook commands.
- Produces: v0.3.0 distributable layout, schema-v2 install state, safe hook-array ownership, v0.2.8-to-v0.3.0 sync path, current version documentation, and focused consumer acceptance instructions.

- [ ] **Step 1: Add failing five-event hook-fragment tests**

Require exactly `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop` for both hosts. `Stop` and `UserPromptSubmit` omit matchers where hosts ignore them. `PreToolUse` matches host-supported tool coverage. Assert synchronous command hooks and exact Windows command entries.

- [ ] **Step 2: Add failing installer ownership/upgrade tests**

Add array identities only for hook arrays with a supported stable matcher. Manage matcher-less `Stop` and `UserPromptSubmit` arrays through exact positional owned-fragment semantics. Verify install/sync preserves unrelated consumer hooks, upgrades exact v0.2.8-owned entries, refuses locally modified entries, installs each new runtime module, sets `record_schema_version: 2`, and leaves legacy records opaque.

- [ ] **Step 3: Update host fragments and manifest ownership**

Point lifecycle events at the exact corresponding commands, such as `python .agent-handoff-toolkit/runner.py hook --platform codex --event stop` and `python .agent-handoff-toolkit/runner.py hook --platform claude --event user-prompt-submit`. Add `lineage.py`, `lifecycle.py`, `lifecycle_storage.py`, `lifecycle_operations.py`, and `hook_adapters.py` to managed runtime artifacts. Recompute normalized LF SHA-256 values only after every managed source is final.

- [ ] **Step 4: Bump package/distribution versions**

Set `0.3.0` in `pyproject.toml`, `src/agent_handoff_toolkit/__init__.py`, and `distribution/manifest.json`; keep installer state version 1 and manifest version 2.

- [ ] **Step 5: Update verified CI pins and README versions**

Change `.github/workflows/ci.yml` to use `runs-on: redducklabs-runners` and install `build==1.6.1 ruff==0.16.7 setuptools==84.0.0`. Update the existing runner assertion in `tests/test_distribution.py` to require the repository policy. Add/update README:

```markdown
## Software versions

Last checked: 2026-09-10.

- Minimum supported Python: 3.11
- CI Python versions: 3.11, 3.12, 3.13, 3.14
- Latest stable Python checked: 3.14.7
- GitHub Actions: `actions/checkout@v7`, `actions/setup-python@v7`
- Build frontend: `build==1.6.1`
- Build backend: `setuptools==84.0.0`
- Linter and formatter: `ruff==0.16.7`
```

Link version sources to official Python/PyPI/action repositories. Do not add unverified Claude Code or Codex client version numbers.

- [ ] **Step 6: Update consumer acceptance documentation**

Require host trust/discovery verification, block-cap compatibility, a synthetic bad-stop correction, exact issue-code receipt, corrected stop, no retained content, and `unverified` status when the real host smoke is not observed.

- [ ] **Step 7: Run installer/distribution tests and regenerate hashes until current**

Run focused installer/distribution suites. Use the repository's existing manifest hash routine; do not hand-edit hashes based on stale bytes. Confirm install followed by `sync --check` reports `CURRENT`.

- [ ] **Step 8: Run static checks for distribution changes**

Run Ruff, format check, compileall, JSON parsing for both fragments/manifest, and `git diff --check`.

- [ ] **Step 9: Commit the v0.3.0 distribution**

```powershell
git add adapters distribution src/agent_handoff_toolkit/__init__.py pyproject.toml .github/workflows/ci.yml README.md docs/consumer-integration.md tests/test_distribution.py tests/test_installer.py
git commit -m "feat: distribute enforced lifecycle hooks"
```

---

### Task 9: Real-host acceptance harness

**Files:**
- Create: `src/agent_handoff_toolkit/acceptance.py`
- Create: `tests/test_acceptance.py`
- Modify: `src/agent_handoff_toolkit/cli.py`
- Modify: `distribution/manifest.json`
- Modify: `docs/consumer-integration.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: installed lifecycle runtime and host CLIs.
- Produces: explicit commands such as `handoff-toolkit acceptance --platform codex --scratch D:\scratch\handoff-acceptance` and `AcceptanceResult(platform, discovered, blocked, issue_received, corrected, retained_content, block_cap_compatible)`.

- [ ] **Step 1: Write failing acceptance-result and redaction tests**

Use fake subprocess runners. Require every boolean field; nonzero result if any required property is false; output contains only platform, pass/fail properties, and issue codes; no prompt, reply, transcript, record body, credential, or customer text.

- [ ] **Step 2: Run the focused test and observe the missing module failure**

Run: `python -m unittest discover -s tests -p "test_acceptance.py" -v`

- [ ] **Step 3: Implement an injectable acceptance orchestrator**

Create a temporary scratch repository, install the current distribution, generate synthetic scenario data at runtime, invoke the chosen host with its hook trust enabled, and reduce all captured output in memory to required event/issue-code booleans. Always clean the scratch repository and host-output files in `finally`; never echo raw output.

- [ ] **Step 4: Add fake-host lifecycle tests**

Simulate hook undiscovered, violation not blocked, issue missing from continuation, correction rejected, content retained, incompatible host cap, and the full green path. Assert `unverified` rather than `pass` when a real host is unavailable.

- [ ] **Step 5: Add CLI wiring and installed-runtime coverage**

The command must be opt-in and never run in ordinary public CI. Add `acceptance.py` to the distribution manifest and installed-runner tests.

- [ ] **Step 6: Run the real Claude Code smoke with the cheap model**

Use a clean temporary consumer and `claude -p --model haiku` with only required local tools/settings. Do not save output-format transcripts. Assert the deliberate violation produces the expected issue code in the same session and a corrected stop succeeds. Report only boolean/code evidence.

- [ ] **Step 7: Run the real Codex smoke with the cheap model**

Use a clean temporary consumer and `codex exec --ephemeral --model gpt-5.6-luna --json --dangerously-bypass-hook-trust` inside the already isolated scratch environment. Stream-reduce JSONL in memory; do not write the model output or transcript. Assert the same violation/block/correction properties and delete scratch state afterward.

- [ ] **Step 8: Verify no retained content**

Search only the scratch/runtime paths for the synthetic sentinel after each run; assert it is absent, then remove the exact verified scratch directory. Never search unrelated user or repository data.

- [ ] **Step 9: Run acceptance unit tests and static checks**

Run the focused suite, Ruff, format check, compileall, manifest hash verification, and `git diff --check`.

- [ ] **Step 10: Commit the acceptance harness**

```powershell
git add src/agent_handoff_toolkit/acceptance.py src/agent_handoff_toolkit/cli.py tests/test_acceptance.py distribution/manifest.json docs/consumer-integration.md README.md
git commit -m "test: add lifecycle host acceptance"
```

---

### Task 10: Full regression verification and independent review

**Files:**
- Modify only files required to correct failures found by verification or review.

**Interfaces:**
- Consumes: complete Tasks 1–9 implementation.
- Produces: verified source tree, package artifacts, current manifest hashes, and independent review evidence.

- [ ] **Step 1: Run the complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: exit `0` with no failures or errors.

- [ ] **Step 2: Run byte-compilation**

Run: `python -m compileall -q src tests distribution`

Expected: exit `0` with no output.

- [ ] **Step 3: Run lint and formatting gates**

```powershell
python -m ruff check src tests distribution
python -m ruff format --check src tests distribution
```

Expected: both exit `0` under `pyproject.toml` configuration.

- [ ] **Step 4: Build and inspect the package**

Run: `python -m build`

Expected: exit `0`, producing an sdist and wheel for `0.3.0`. Inspect archive members and confirm all public runtime modules are present and no state, prompt, reply, transcript, credential, scratch, or cache artifact is packaged.

- [ ] **Step 5: Exercise installed commands from a temporary consumer**

Run install dry-run/apply, direct lifecycle hook commands for all five events, render/validate both record types, v1 compatibility, and `sync --check`. Expect safe install, correct block/allow JSON, and final `CURRENT` status.

- [ ] **Step 6: Verify documentation and repository invariants**

Run JSON parsing, manifest hash verification, `git diff --check`, byte-equivalence of `AGENTS.md`/`CLAUDE.md`, placeholder scan, and a repository search proving no committed prompt/reply/transcript/sentinel content or secret-like fixture was introduced.

- [ ] **Step 7: Request independent code review**

Have Claude review the complete implementation diff against the approved spec, focusing on bypasses, host semantics, privacy, CAS/locking, circuit behavior, v1 compatibility, and the two original regressions. Require `VERDICT: APPROVED`; fix and re-review every Critical/High/Important finding.

- [ ] **Step 8: Rerun every affected gate after review fixes**

Do not reuse pre-fix evidence. Rerun the full README verification sequence and both real-host acceptance commands if hook/runtime/distribution behavior changed.

- [ ] **Step 9: Inspect final repository state**

Confirm the branch contains only intended commits and files, the working tree is clean, no `.env` file was created or modified, and no remote operation occurred unless separately authorized.

- [ ] **Step 10: Commit verified review fixes if any**

```powershell
git add src tests adapters distribution docs README.md AGENTS.md CLAUDE.md pyproject.toml .github/workflows/ci.yml
git commit -m "fix: address lifecycle enforcement review"
```

Skip this commit when review required no changes.
