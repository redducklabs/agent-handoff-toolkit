# Enforcement Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the toolkit gating work that needs no handoff — sessions run ungated by default, the agent declares tracked work, and undeclared drift is reported at the end of a session rather than prevented during it.

**Architecture:** Two new enforcement modes (`OPEN`, `ONE_OFF`) replace the gating role of `UNTRACKED`. `PreToolUse` stops denying work and instead emits a one-time non-blocking advisory on the first repository write. `Stop` emits a non-blocking note when the session ends with unfinished repository work, detected by comparing a `git status --porcelain` digest against a baseline taken at the first `UserPromptSubmit`. The toolkit continues to intercept and repair its own `lifecycle …` control commands, which is how a session discovers its credentials.

**Tech Stack:** Python 3.11+, stdlib only (`subprocess`, `hashlib`, `json`, `dataclasses`), `unittest` + `pytest` for tests, `ruff` for lint and format.

**Spec:** `docs/superpowers/specs/2026-09-14-enforcement-scope-design.md`

## Global Constraints

- Target release: **v0.4.0**. Version strings live in `pyproject.toml`, `src/agent_handoff_toolkit/__init__.py`, `distribution/manifest.json` (`toolkit_version`), `src/agent_handoff_toolkit/acceptance.py`, `tests/test_acceptance.py`, `tests/test_distribution.py`, `docs/consumer-integration.md`, `README.md`.
- `tests/test_distribution.py::test_manifest_hashes_every_managed_artifact` asserts that **every** `vX.Y.Z` string in `README.md` and `docs/consumer-integration.md` equals the new release. A stale reference in either file fails the build.
- There is **no manifest regeneration script**. Any edit to a managed source file leaves `distribution/manifest.json` stale and fails the distribution tests. Recompute affected `sha256` values after every source change, normalising CRLF to LF before hashing.
- Hook output must keep **exit code 0** on every advisory path. Exit code 2 blocks unconditionally regardless of the JSON returned.
- No AI attribution in commits or pull requests.
- On Windows, export `MSYS_NO_PATHCONV=1` before any `git` command containing a `rev:path` argument.
- Before reporting success, run all five commands from `README.md`: `python -m unittest discover -s tests`, `python -m compileall -q src tests distribution`, `python -m ruff check src tests distribution`, `python -m ruff format --check src tests distribution`, `python -m build`.
- Keep `AGENTS.md` and `CLAUDE.md` semantically identical.

---

### Task 1: Add `OPEN` and `ONE_OFF` modes with backward-compatible state loading

`_model` in `lifecycle_storage.py` requires the persisted field set to match the dataclass exactly, so both the new enum values and any new fields must be handled before anything else can use them.

**Files:**
- Modify: `src/agent_handoff_toolkit/lifecycle.py:39-43` (enum), `src/agent_handoff_toolkit/lifecycle.py:549-562` (`SessionState` fields)
- Modify: `src/agent_handoff_toolkit/lifecycle_storage.py:76-83` (`_model`), `src/agent_handoff_toolkit/lifecycle_storage.py:615` (default session)
- Test: `tests/test_lifecycle_storage.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EnforcementMode.OPEN` (value `"open"`), `EnforcementMode.ONE_OFF` (value `"one-off"`); `SessionState.write_advisory_emitted: bool = False`; `SessionState.worktree_baseline: str | None = None`. `EnforcementMode("untracked")` continues to resolve, to `OPEN`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_lifecycle_storage.py`:

```python
    def test_legacy_session_state_loads_as_open(self):
        """State written before v0.4.0 lacks the new fields and names the old mode."""

        from agent_handoff_toolkit.lifecycle_storage import _model

        legacy = {
            "session_key": "a" * 64,
            "targeted_revision": 3,
            "mode": "untracked",
            "authorization_id": None,
            "chain_revision": None,
            "pending_decision_reference": None,
            "pending_transition_reference": None,
            "correction_cycle_count": 0,
            "last_issue_signature": None,
            "current_external_user_turn_reference": None,
            "bootstrap_challenge": None,
            "pending_correction_hmac": None,
        }
        session = _model(legacy, SessionState)
        self.assertIs(session.mode, EnforcementMode.OPEN)
        self.assertFalse(session.write_advisory_emitted)
        self.assertIsNone(session.worktree_baseline)

    def test_new_modes_round_trip_through_storage(self):
        for mode in (EnforcementMode.OPEN, EnforcementMode.ONE_OFF):
            with self.subTest(mode=mode):
                self.assertIs(EnforcementMode(mode.value), mode)
```

Ensure `SessionState` and `EnforcementMode` are imported in that test module; add them to the existing import block if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_lifecycle_storage.py -k "legacy_session_state or new_modes" -v`
Expected: FAIL with `AttributeError: OPEN` or `ValueError: 'untracked' is not a valid EnforcementMode`.

- [ ] **Step 3: Add the enum members**

In `src/agent_handoff_toolkit/lifecycle.py`, replace the `EnforcementMode` body:

```python
class EnforcementMode(str, Enum):
    # A session enforces nothing until it declares tracked work. OPEN is the
    # default; ONE_OFF records that the session was asked and chose not to
    # track. UNTRACKED is the pre-v0.4.0 spelling of OPEN and is accepted only
    # when loading state written by an earlier release.
    OPEN = "open"
    ONE_OFF = "one-off"
    UNTRACKED = "untracked"
    TRACKED = "tracked"
    AWAITING_DECISION = "awaiting-decision"
    COMPLETE = "complete"
```

- [ ] **Step 4: Add the two session fields**

In `src/agent_handoff_toolkit/lifecycle.py`, append to `SessionState` after `pending_correction_hmac`:

```python
    write_advisory_emitted: bool = False
    worktree_baseline: str | None = None
```

In the same class's `__post_init__`, after the existing `pending_correction_hmac` check, add:

```python
        if not isinstance(self.write_advisory_emitted, bool):
            raise ValueError("write_advisory_emitted must be boolean")
        if self.worktree_baseline is not None:
            _digest(self.worktree_baseline, "worktree_baseline")
```

- [ ] **Step 5: Make `_model` tolerant of legacy state**

In `src/agent_handoff_toolkit/lifecycle_storage.py`, replace the opening of `_model`:

```python
def _model(value, model):
    if not isinstance(value, dict):
        raise LifecycleStorageError("state contains unknown or missing fields")
    names = {item.name for item in fields(model)}
    if model is SessionState:
        # State written before v0.4.0 has neither the advisory flag nor the
        # worktree baseline, and spells the default mode "untracked". Filling
        # the defaults here keeps an existing session loadable; anything else
        # unknown is still rejected.
        value = {key: item for key, item in value.items() if key in names}
        value.setdefault("write_advisory_emitted", False)
        value.setdefault("worktree_baseline", None)
    if set(value) != names:
        raise LifecycleStorageError("state contains unknown or missing fields")
    value = dict(value)
    if model is SessionState:
        mode = EnforcementMode(value["mode"])
        value["mode"] = EnforcementMode.OPEN if mode is EnforcementMode.UNTRACKED else mode
```

Delete the now-superseded `value["mode"] = EnforcementMode(value["mode"])` line that followed.

- [ ] **Step 6: Default new sessions to OPEN**

In `src/agent_handoff_toolkit/lifecycle_storage.py:615`, change `EnforcementMode.UNTRACKED` to `EnforcementMode.OPEN`.

- [ ] **Step 7: Replace remaining UNTRACKED comparisons**

Run `grep -rn "EnforcementMode.UNTRACKED" src/` and change every comparison to accept both new modes. The call sites are `hook_adapters.py` (lines near 595, 617, 813), `lifecycle.py` (near 615, 903, 948), and `lifecycle_operations.py` `_bootstrap` (near 328). Each becomes:

```python
_UNGATED = frozenset({EnforcementMode.OPEN, EnforcementMode.ONE_OFF})
```

defined once in `lifecycle.py` and imported where needed, with `session.mode is EnforcementMode.UNTRACKED` becoming `session.mode in _UNGATED`. In `lifecycle_operations.py:328`, `_bootstrap` must accept `OPEN` and `ONE_OFF`:

```python
        if snapshot.session.mode not in _UNGATED:
            raise ValueError("bootstrap requires a session with no registered root")
```

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_lifecycle_storage.py tests/test_lifecycle.py -q`
Expected: PASS. If other suites fail, that is Task 2's work — do not fix them here.

- [ ] **Step 9: Resync the manifest and commit**

```bash
python -c "
import hashlib, json
from pathlib import Path
m = Path('distribution/manifest.json'); raw = m.read_text(encoding='utf-8')
for a in json.loads(raw)['artifacts']:
    p = Path(*a['source'].split('/'))
    d = hashlib.sha256(p.read_bytes().decode('utf-8').replace('\r\n','\n').replace('\r','\n').encode()).hexdigest()
    if d != a['sha256']: raw = raw.replace(a['sha256'], d); print('resynced', a['source'])
m.write_text(raw, encoding='utf-8', newline='')
"
git add -A
git commit -m "feat: add OPEN and ONE_OFF modes with backward-compatible state loading"
```

---

### Task 2: Stop gating work in ungated modes

**Files:**
- Modify: `src/agent_handoff_toolkit/hook_adapters.py:584-640` (`_pre_tool`)
- Modify: `src/agent_handoff_toolkit/hook_adapters.py` (`_READ_ONLY` usage, `_ACTIONS`)
- Test: `tests/test_host_input.py`, `tests/test_hook_adapters.py`

**Interfaces:**
- Consumes: `EnforcementMode.OPEN`, `EnforcementMode.ONE_OFF`, `_UNGATED` from Task 1.
- Produces: `_pre_tool` returns `HookExecution()` for every non-control tool call in an ungated mode.

- [ ] **Step 1: Write the failing test**

Create `tests/test_enforcement_scope.py`:

```python
"""Ungated sessions run their work without interference."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.hook_adapters import (  # noqa: E402
    HookExecution,
    run_lifecycle_hook,
)
from agent_handoff_toolkit.lifecycle_storage import LocalLifecycleStorage  # noqa: E402

from test_hook_adapters import payload  # noqa: E402


class UngatedSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        self.storage = LocalLifecycleStorage(self.root, state_root=self.root / "state")
        for name in ("handoffs", ".agent-handoff-toolkit", ".git"):
            (self.root / name).mkdir()
        (self.root / ".agent-handoff-toolkit" / "runner.py").write_text(
            "# owned runner\n"
        )

    def invoke(self, event="PreToolUse", **changes):
        return run_lifecycle_hook(
            "claude",
            event,
            json.dumps(payload(self.root, event, **changes)),
            self.root,
            self.storage,
        )

    def test_a_session_with_no_root_is_never_gated(self):
        """Every one of these was denied with AHK-PRE-ROOT before v0.4.0."""

        self.invoke("UserPromptSubmit")
        for label, tool, tool_input in (
            ("git status", "Bash", {"command": "git status"}),
            ("create a ticket", "Bash", {"command": "gh issue create -t x"}),
            ("run the tests", "Bash", {"command": "pytest -q"}),
            ("read-only MCP query", "mcp__db__query", {"sql": "select 1"}),
            ("fetch a page", "WebFetch", {"url": "https://example.invalid"}),
            ("search the web", "WebSearch", {"query": "x"}),
            ("launch a subagent", "Task", {"prompt": "x"}),
            ("write a todo list", "TodoWrite", {"todos": []}),
            ("read a file", "Read", {"file_path": "a"}),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    self.invoke(tool_name=tool, tool_input=tool_input),
                    HookExecution(),
                    f"{label} must not be gated",
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enforcement_scope.py -v`
Expected: FAIL — every subtest denied with `AHK-PRE-ROOT`.

- [ ] **Step 3: Thread the raw session id into `_pre_tool`**

`_pre_tool` currently takes `(event, snapshot, root, storage)` and has no access to the raw session id, which `_commit` requires. Task 4 needs to commit from inside it, so widen the signature now.

At `src/agent_handoff_toolkit/hook_adapters.py:888`, change the call to:

```python
            return _pre_tool(event, snapshot, root, storage, raw_id)
```

- [ ] **Step 4: Rewrite the gate in `_pre_tool`**

In `src/agent_handoff_toolkit/hook_adapters.py`, replace the opening of `_pre_tool` down to and including the `AHK-PRE-ROOT` block that fires when `current_external_user_turn_reference is None`:

```python
def _pre_tool(event, snapshot, root, storage, raw_id):
    session = snapshot.session
    command = (
        event.tool_input.get("command")
        if event.tool_name in _SHELL[event.host]
        else None
    )
    control = (
        isinstance(command, str)
        and re.match(r"^python \S+ lifecycle(?: |$)", command) is not None
    )
    # Work is never gated. The toolkit intercepts only its own control
    # commands, which is how a session discovers its session key, challenge and
    # revision: UserPromptSubmit returns silently, so this denial is the sole
    # carrier of those credentials.
    if not control:
        return HookExecution()
    if session.current_external_user_turn_reference is None:
        return render_hook_execution(
            event.host,
            event.event,
            LifecycleDecision(DecisionKind.BLOCK, (_issue("AHK-PRE-ROOT"),)),
        )
```

Leave everything below that point unchanged — the runner check, capability derivation, `_repair_bootstrap`, `_form_register_root`, the `AHK-CONTROL-BINDING` branch for tracked sessions, and the reason assembly all still apply to control commands.

- [ ] **Step 5: Run the new test**

Run: `python -m pytest tests/test_enforcement_scope.py -v`
Expected: PASS.

- [ ] **Step 6: Rewrite the tests that asserted the old gate**

Run `python -m pytest tests/ -q` and expect failures in `tests/test_hook_adapters.py`. Rewrite each rather than deleting it:

- `test_untracked_mutations_require_exact_current_bootstrap_and_owned_runner` — drop the loop asserting `Bash`, `Write`, `apply_patch`, `mcp__fs__read` and `unknown` are denied, and assert instead that each returns `HookExecution()`. Keep every assertion about the bound control command, the malicious variants, and the feedback text: those still hold.
- Any test asserting `AHK-PRE-ROOT` for a non-control tool — change it to assert the call is allowed, and move the `AHK-PRE-ROOT` assertion onto a control command with no user turn yet.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS except the distribution manifest tests.

- [ ] **Step 8: Prove tracked enforcement and control repair are unchanged**

The spec names this the heaviest regression risk. Add to `tests/test_enforcement_scope.py`:

```python
    def test_a_tracked_session_still_enforces_and_repairs_control_commands(self):
        import base64

        from agent_handoff_toolkit.lifecycle import EnforcementMode
        from agent_handoff_toolkit.lifecycle_operations import LifecycleService
        from agent_handoff_toolkit.lineage import canonical_json_bytes

        self.invoke("UserPromptSubmit")
        runner = (self.root / ".agent-handoff-toolkit" / "runner.py").as_posix()
        # The plain-slot form added in v0.3.2 still round-trips through the hook.
        attempt = (
            f'python {runner} lifecycle register-root --scope-id issue-1'
            ' --scope-kind issue --scope-title "A tracked scope"'
            ' --scope-outcome "The tracked scope is complete."'
        )
        reason = json.loads(
            self.invoke(tool_name="Bash", tool_input={"command": attempt}).stdout
        )["hookSpecificOutput"]["permissionDecisionReason"]
        formed = reason.split("Command: ", 1)[1].strip()
        self.assertIn("--scope-definition-b64", formed)
        self.assertEqual(
            self.invoke(tool_name="Bash", tool_input={"command": formed}),
            HookExecution(),
        )
        # Register for real, then confirm a tracked stop still blocks.
        service = LifecycleService(self.storage, "session-1")
        snapshot = self.storage.load_snapshot("session-1")
        definition = {"title": "A tracked scope", "outcome": "The tracked scope is complete."}
        encoded = (
            base64.urlsafe_b64encode(canonical_json_bytes(definition)).decode().rstrip("=")
        )
        service.register_root(
            challenge=snapshot.session.bootstrap_challenge,
            scope_id="issue-1",
            scope_kind="issue",
            scope_definition_b64=encoded,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertIs(
            self.storage.load_snapshot("session-1").session.mode,
            EnforcementMode.TRACKED,
        )
        blocked = self.invoke("Stop", last_assistant_message="I am done.")
        self.assertIn("AHK-STOP-WORK", json.loads(blocked.stdout)["reason"])
```

Run: `python -m pytest tests/test_enforcement_scope.py -k tracked -v`
Expected: PASS.

- [ ] **Step 9: Resync the manifest and commit**

Use the resync snippet from Task 1 Step 9, then:

```bash
git add -A
git commit -m "feat: stop gating work in sessions with no registered root"
```

---

### Task 3: `lifecycle one-off` declaration

**Files:**
- Modify: `src/agent_handoff_toolkit/lifecycle_operations.py:40-60` (`_FLAGS`), and add `LifecycleService.one_off`
- Modify: `src/agent_handoff_toolkit/cli.py:112-168` (parser), `src/agent_handoff_toolkit/cli.py` (dispatch)
- Test: `tests/test_enforcement_scope.py`

**Interfaces:**
- Consumes: `EnforcementMode.ONE_OFF`, `_UNGATED`, `LifecycleService._bootstrap` from Task 1.
- Produces: `LifecycleService.one_off(*, challenge, expected_session_revision) -> dict`, returning `{"mode": "one-off"}`. `_FLAGS["one-off"] == ("session-key", "challenge", "expected-session-revision")`.

- [ ] **Step 1: Write the failing test**

`tests/test_lifecycle_operations.py` has no shared bootstrapped-service helper, so add this to `tests/test_enforcement_scope.py`, whose `setUp` already builds a consumer and storage. Add `from agent_handoff_toolkit.lifecycle import EnforcementMode` and `from agent_handoff_toolkit.lifecycle_operations import LifecycleService` to that module's imports.

```python
    def test_one_off_records_the_declaration_and_grants_nothing(self):
        self.invoke("UserPromptSubmit")
        service = LifecycleService(self.storage, "session-1")
        snapshot = self.storage.load_snapshot("session-1")
        result = service.one_off(
            challenge=snapshot.session.bootstrap_challenge,
            expected_session_revision=snapshot.session.targeted_revision,
        )
        self.assertEqual(result, {"mode": "one-off"})
        session = self.storage.load_snapshot("session-1").session
        self.assertIs(session.mode, EnforcementMode.ONE_OFF)
        self.assertIsNone(session.authorization_id)
        self.assertIsNone(session.chain_revision)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enforcement_scope.py -k one_off -v`
Expected: FAIL with `AttributeError: 'LifecycleService' object has no attribute 'one_off'`.

- [ ] **Step 3: Add the flag tuple**

In `src/agent_handoff_toolkit/lifecycle_operations.py`, add to `_FLAGS`:

```python
    "one-off": ("session-key", "challenge", "expected-session-revision"),
```

- [ ] **Step 4: Add the service method**

In `src/agent_handoff_toolkit/lifecycle_operations.py`, add to `LifecycleService` next to `register_root`:

```python
    def one_off(self, *, challenge, expected_session_revision):
        """Record that the session was asked to declare and chose not to track.

        This grants no authority. It suppresses the write-time advisory only;
        a session that ends with unfinished work is still reported at Stop.
        """

        snapshot = self._bootstrap(challenge, expected_session_revision)
        session = replace(
            snapshot.session,
            mode=EnforcementMode.ONE_OFF,
            targeted_revision=snapshot.session.targeted_revision + 1,
        )
        self._commit(snapshot, session)
        return {"mode": "one-off"}
```

- [ ] **Step 5: Wire the CLI**

In `src/agent_handoff_toolkit/cli.py::_lifecycle_parser`, after the `adopt` parser:

```python
    one_off = commands.add_parser(
        "one-off", help="declare that this session's work needs no handoff"
    )
```

Add `one_off` to the existing `for command in (register, resume, join, adopt):` tuple so it gains `--session-key`, `--challenge` and `--expected-session-revision`.

In `_lifecycle_main`, add `"one-off"` to the set on the line reading `if operation not in {"register-root", "resume", "join", "adopt-v1"}:`, and add a dispatch branch alongside `inspect`:

```python
        elif operation == "one-off":
            result = service.one_off(**args)
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_enforcement_scope.py tests/test_lifecycle_operations.py -q`
Expected: PASS.

- [ ] **Step 7: Resync the manifest and commit**

```bash
git add -A
git commit -m "feat: add the lifecycle one-off declaration"
```

---

### Task 4: Advisory on the first repository write

**Files:**
- Modify: `src/agent_handoff_toolkit/hook_adapters.py` (`_pre_tool`, new `_WRITE_TOOLS`)
- Test: `tests/test_enforcement_scope.py`

**Interfaces:**
- Consumes: `SessionState.write_advisory_emitted` from Task 1; `_control_command` and `_form_register_root` from the existing module.
- Produces: `_WRITE_TOOLS: dict[str, frozenset[str]]`; `_pre_tool` emits `permissionDecision: "allow"` with a `systemMessage` once per session.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_enforcement_scope.py`:

```python
    def write(self, name="a.py"):
        return self.invoke(
            tool_name="Write",
            tool_input={"file_path": str(self.root / name), "content": "x = 1\n"},
        )

    def test_the_first_repository_write_advises_once_and_allows(self):
        self.invoke("UserPromptSubmit")
        first = self.write()
        self.assertEqual(first.exit_code, 0)
        payload_out = json.loads(first.stdout)
        self.assertEqual(
            payload_out["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        message = payload_out["systemMessage"]
        self.assertIn("lifecycle one-off", message)
        self.assertIn("lifecycle register-root", message)
        # It fires once, then never again.
        self.assertEqual(self.write("b.py"), HookExecution())

    def test_bash_never_triggers_the_advisory(self):
        self.invoke("UserPromptSubmit")
        self.assertEqual(
            self.invoke(tool_name="Bash", tool_input={"command": "git commit -m x"}),
            HookExecution(),
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enforcement_scope.py -k advis -v`
Expected: FAIL — the first write returns `HookExecution()` with empty stdout.

- [ ] **Step 3: Declare the write-tool set**

In `src/agent_handoff_toolkit/hook_adapters.py`, beside `_SHELL`:

```python
# Tools that write repository files. Shell is deliberately absent: classifying
# a command as read-only or not is fragile, and making `git status` an advisory
# trigger would defeat the purpose. A missed trigger costs an advisory, not a
# guarantee - the Stop note is the backstop and reads the worktree directly.
_WRITE_TOOLS = {
    "claude": frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"}),
    "codex": frozenset({"apply_patch"}),
}
```

- [ ] **Step 4: Emit the advisory**

In `_pre_tool`, replace the `if not control: return HookExecution()` line added in Task 2 with:

```python
    if not control:
        if (
            session.mode is EnforcementMode.OPEN
            and not session.write_advisory_emitted
            and event.tool_name in _WRITE_TOOLS[event.host]
            and session.current_external_user_turn_reference is not None
        ):
            return _write_advisory(event, snapshot, root, storage)
        return HookExecution()
```

Add the helper above `_pre_tool`:

```python
def _write_advisory(event, snapshot, root, storage):
    """Allow the write and say, once, that nothing is tracking this session.

    Never blocks and never errors: an advisory that can fail the tool call is
    worse than no advisory. Any failure here leaves the call untouched.
    """

    try:
        session = snapshot.session
        runner = root / ".agent-handoff-toolkit" / "runner.py"
        capability = storage.control_capability(session)
        message = (
            "AHK-DECLARE: This session is changing the repository with no "
            "registered root, so nothing will carry the work to a next session. "
            "If the work ends here, run:\n"
            + _control_command(runner, session, capability, "one-off")
            + "\nIf it continues past this session, run register-root with the "
            "scope title and outcome as plain text:\n"
            + _control_command(
                runner,
                session,
                capability,
                "register-root",
                (
                    ("scope-id", "{scope_id}"),
                    ("scope-kind", "{scope_kind}"),
                    ("scope-title", '"{title}"'),
                    ("scope-outcome", '"{outcome}"'),
                ),
            )
            + "\nNeither is required; this notice appears once."
        )
        if len(message.encode()) > MAX_REASON_BYTES:
            return HookExecution()
        _commit(
            storage,
            event.session_key,
            snapshot,
            LifecycleMutation(
                replace(
                    session,
                    write_advisory_emitted=True,
                    targeted_revision=session.targeted_revision + 1,
                )
            ),
        )
        return HookExecution(
            stdout=json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    },
                    "systemMessage": message,
                },
                separators=(",", ":"),
            )
        )
    except Exception:
        # Advisory only. A failure here must not disturb the tool call.
        return HookExecution()
```

Note: `_commit` takes the raw session id, not the derived key. Read the call site in `_user_prompt` and pass the same value it passes; adjust the `_write_advisory` signature to receive it if `event.session_key` is the derived key rather than the raw id.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_enforcement_scope.py -q`
Expected: PASS.

- [ ] **Step 6: Resync the manifest and commit**

```bash
git add -A
git commit -m "feat: advise once on the first repository write"
```

---

### Task 5: Worktree baseline and the `Stop` note

**Files:**
- Create: `src/agent_handoff_toolkit/repository_state.py`
- Modify: `src/agent_handoff_toolkit/hook_adapters.py` (`_user_prompt`, `run_lifecycle_hook` Stop branch)
- Modify: `distribution/manifest.json` (new managed artifact)
- Test: `tests/test_enforcement_scope.py`, `tests/test_repository_state.py`

**Interfaces:**
- Consumes: `SessionState.worktree_baseline` from Task 1.
- Produces: `repository_state.worktree_digest(root: Path) -> str | None` — the sha256 of `git status --porcelain` output, or `None` when it cannot be determined; `repository_state.WORKTREE_TIMEOUT_SECONDS = 5`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repository_state.py`:

```python
"""Reading the worktree must never raise and never hang."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_handoff_toolkit.repository_state import worktree_digest  # noqa: E402


class WorktreeDigestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        os.environ["GIT_CEILING_DIRECTORIES"] = self.root.parent.as_posix()
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)

    def test_clean_and_dirty_trees_differ(self):
        clean = worktree_digest(self.root)
        self.assertIsNotNone(clean)
        (self.root / "a.txt").write_text("x")
        self.assertNotEqual(worktree_digest(self.root), clean)

    def test_returns_none_outside_a_repository(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        os.environ["GIT_CEILING_DIRECTORIES"] = other.as_posix()
        self.assertIsNone(worktree_digest(other))

    def test_never_raises_when_git_is_unusable(self):
        for failure in (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired("git", 5),
            OSError("boom"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "agent_handoff_toolkit.repository_state.subprocess.run",
                    side_effect=failure,
                ):
                    self.assertIsNone(worktree_digest(self.root))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repository_state.py -v`
Expected: FAIL with `ModuleNotFoundError: agent_handoff_toolkit.repository_state`.

- [ ] **Step 3: Write the module**

Create `src/agent_handoff_toolkit/repository_state.py`:

```python
"""Read the repository's working-tree state for advisory purposes only.

Nothing here gates anything, so nothing here may raise. Every failure - no git
binary, not a repository, a timeout, unreadable output - is reported as "cannot
tell", and the caller stays silent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

WORKTREE_TIMEOUT_SECONDS = 5


def worktree_digest(root: Path) -> str | None:
    """Digest the porcelain status, or None when it cannot be determined."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            timeout=WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return hashlib.sha256(completed.stdout).hexdigest()


def is_dirty(root: Path) -> bool | None:
    """Whether the tree has any change, or None when it cannot be determined."""

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            timeout=WORKTREE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())
```

- [ ] **Step 4: Run the module tests**

Run: `python -m pytest tests/test_repository_state.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing Stop-note test**

Add to `tests/test_enforcement_scope.py`. The class `setUp` creates `.git` as a bare directory, so this test needs a real repository — initialise one in the test itself with `subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)` after removing the placeholder `.git` directory.

```python
    def test_stop_notes_unfinished_work_and_never_blocks(self):
        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        self.invoke("UserPromptSubmit")
        # Nothing changed: silent.
        self.assertEqual(self.invoke("Stop"), HookExecution())
        # The session dirties the tree.
        (self.root / "changed.py").write_text("x = 1\n")
        output = self.invoke("Stop")
        self.assertEqual(output.exit_code, 0)
        body = json.loads(output.stdout)
        self.assertIn("AHK-NO-HANDOFF", body["systemMessage"])
        self.assertNotIn("decision", body)
        self.assertNotIn("continue", body)

    def test_stop_is_silent_when_the_tree_was_already_dirty(self):
        import shutil
        import subprocess

        shutil.rmtree(self.root / ".git")
        subprocess.run(["git", "init", "--quiet"], cwd=self.root, check=True)
        (self.root / "preexisting.py").write_text("x = 1\n")
        self.invoke("UserPromptSubmit")
        self.assertEqual(self.invoke("Stop"), HookExecution())
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m pytest tests/test_enforcement_scope.py -k stop -v`
Expected: FAIL — `Stop` returns `HookExecution()` in the dirty case.

- [ ] **Step 7: Record the baseline**

In `src/agent_handoff_toolkit/hook_adapters.py::_user_prompt`, immediately after the early `if not event.external_user_turn: return HookExecution()` guard, record the baseline once per session:

```python
    if session.worktree_baseline is None:
        digest = worktree_digest(root)
        if digest is not None:
            snapshot = _commit(
                storage,
                raw_id,
                snapshot,
                LifecycleMutation(
                    replace(
                        snapshot.session,
                        worktree_baseline=digest,
                        targeted_revision=snapshot.session.targeted_revision + 1,
                    )
                ),
            )
            session = snapshot.session
```

`_user_prompt` must take `root`; it already receives it as its last parameter. Import `worktree_digest` at the top of the module.

- [ ] **Step 8: Emit the note at Stop**

In `run_lifecycle_hook`, in the branch that handles `EventName.STOP`, after `decision = evaluate_stop(...)` and before `_publish_decision`, add:

`_publish_decision` commits `decision.mutation` even for an `ALLOW`, so the note must not short-circuit it. Publish first, then replace a silent result:

```python
    output = _publish_decision(platform, event_kind, decision, storage, raw_id, snapshot)
    if (
        event_kind is EventName.STOP
        and output == HookExecution()
        and snapshot is not None
        and snapshot.session.mode in _UNGATED
    ):
        note = _no_handoff_note(snapshot, root)
        if note is not None:
            return note
    return output
```

This replaces the existing `return _publish_decision(...)` at the end of `run_lifecycle_hook`. `root` is in scope only inside the `try` block, so hoist `root = Path(repo_root).resolve()` above it, or recompute it here — the latter is cheaper to reason about and cannot raise on a path already resolved once.

Add the helper:

```python
def _no_handoff_note(snapshot, root):
    """Report unfinished work at the end of an untracked session.

    Both conditions must hold: the tree is dirty now, and it differs from the
    baseline taken at the session's first user turn. The first alone fires on
    work the user left in place beforehand; the second alone fires on a session
    that cleaned the tree by committing pre-existing changes.
    """

    try:
        baseline = snapshot.session.worktree_baseline
        if baseline is None:
            return None
        current = worktree_digest(root)
        if current is None or current == baseline or not is_dirty(root):
            return None
        return HookExecution(
            stdout=json.dumps(
                {
                    "systemMessage": (
                        "AHK-NO-HANDOFF: This session changed the repository and "
                        "is ending with the work unfinished, with no handoff "
                        "record. If someone continues this, register a root and "
                        "render a continuation."
                    )
                },
                separators=(",", ":"),
            )
        )
    except Exception:
        # Advisory only; never disturb the stop.
        return None
```

- [ ] **Step 9: Run the tests**

Run: `python -m pytest tests/test_enforcement_scope.py tests/test_repository_state.py -q`
Expected: PASS.

- [ ] **Step 10: Register the new file as a managed artifact**

`src/agent_handoff_toolkit/repository_state.py` is vendored into consumers, so it must appear in `distribution/manifest.json` alongside the other `src/agent_handoff_toolkit/*.py` entries. Insert this object into `artifacts`, keeping the file's existing ordering, then resync hashes with the snippet from Task 1:

```json
{
  "source": "src/agent_handoff_toolkit/repository_state.py",
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "install": {
    "mode": "copy",
    "transform": "none",
    "targets": [
      ".agent-handoff-toolkit/src/agent_handoff_toolkit/repository_state.py"
    ]
  }
}
```

The placeholder `sha256` is replaced by the resync step; the distribution test fails until it is.

- [ ] **Step 11: Run the full suite and commit**

Run: `python -m pytest tests/ -q`
Expected: PASS.

```bash
git add -A
git commit -m "feat: report unfinished work at the end of an untracked session"
```

---

### Task 6: Documentation and skill

**Files:**
- Modify: `docs/agent-handoff/mechanics.md`, `distribution/consumer-instructions.md`, `skills/agent-handoff/SKILL.md`, `README.md`
- Test: `tests/test_distribution.py` (manifest hashes)

**Interfaces:**
- Consumes: the behaviour built in Tasks 2-5.
- Produces: no code.

- [ ] **Step 1: Amend `mechanics.md`**

Replace the paragraph beginning "Informational hooks fail open. Tracked lifecycle hooks fail closed on `UserPromptSubmit` and `Stop`" so it scopes the fail-closed rule to tracked sessions, and add a section stating the guarantee in the terms the spec sets out:

> The toolkit does not guarantee that work needing a handoff produces one. It
> guarantees that declared tracked work follows the lifecycle, and it reports
> undeclared work that ends unfinished. A session enforces nothing until it
> registers a root.

Document the two advisories by code — `AHK-DECLARE` and `AHK-NO-HANDOFF` — and state that neither blocks.

- [ ] **Step 2: Amend `consumer-instructions.md`**

State that a session runs ungated until it registers a root, that `lifecycle one-off` records a deliberate decision not to track, and that neither advisory blocks.

- [ ] **Step 3: Rewrite the relevant part of `SKILL.md`**

The skill currently assumes the agent must register a root before acting. Replace that with the decision rule: register a root when the work spans more than one session, will be handed off, or the user expects to resume it later. Otherwise do nothing — conversation, investigation, ticket creation and single-session fixes need no lifecycle at all.

- [ ] **Step 4: Verify no stale release strings**

Run: `grep -rn "v0\.3\.[0-9]" README.md docs/consumer-integration.md`
Expected: no output after Task 7; at this point note any hits for Task 7 to fix.

- [ ] **Step 5: Resync the manifest and commit**

```bash
git add -A
git commit -m "docs: describe enforcement that begins only when work is declared tracked"
```

---

### Task 7: Release v0.4.0

**Files:**
- Modify: every file listed under Global Constraints
- Test: `tests/test_distribution.py`, `tests/test_acceptance.py`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: a build of `agent_handoff_toolkit-0.4.0`.

- [ ] **Step 1: Bump every version string**

```bash
python - <<'PY'
from pathlib import Path
OLD, NEW = "0.3.3", "0.4.0"
for name in ("pyproject.toml","distribution/manifest.json","docs/consumer-integration.md",
             "src/agent_handoff_toolkit/__init__.py","src/agent_handoff_toolkit/acceptance.py",
             "tests/test_acceptance.py","tests/test_distribution.py","README.md"):
    p = Path(name); t = p.read_text(encoding="utf-8")
    if OLD not in t: raise SystemExit(f"{name}: no {OLD}")
    p.write_text(t.replace(OLD, NEW), encoding="utf-8", newline="")
    print("bumped", name)
PY
```

- [ ] **Step 2: Write the release narrative in `README.md`**

Add a paragraph under `## Status`, above the v0.3.3 one, stating that v0.4.0 narrows enforcement to declared tracked work: a session runs ungated until it registers a root, the first repository write says so once, and a session that ends with unfinished work is reported rather than prevented. Say plainly that the guarantee is now about declared work.

- [ ] **Step 3: Resync the manifest**

Use the snippet from Task 1 Step 9.

- [ ] **Step 4: Run every verification command**

```bash
python -m unittest discover -s tests
python -m compileall -q src tests distribution
python -m ruff check src tests distribution
python -m ruff format --check src tests distribution
python -m build
```

Expected: `OK`, clean, clean, clean, and `Successfully built agent_handoff_toolkit-0.4.0`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: release v0.4.0"
```

---

### Task 8: Verify against a real host before tagging

The spec records one unsettled question: whether a non-blocking `systemMessage` behaves as documented, and where `additionalContext` belongs per event. The `acceptance` smoke harness exists to prove a host honours hook output.

**Files:**
- Modify: `src/agent_handoff_toolkit/acceptance.py` if the harness needs a case for the advisory paths
- Test: manual, against a real host

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: evidence that the advisories reach the host without blocking.

- [ ] **Step 1: Run the host smoke check**

Run the `acceptance` command against a disposable consumer installed from the v0.4.0 release source, per `README.md`.

- [ ] **Step 2: Confirm both advisories**

In a real tracked-host session with no registered root: make one repository write and confirm the `AHK-DECLARE` message appears without the call being denied; then end the turn with the tree dirty and confirm `AHK-NO-HANDOFF` appears without the turn being blocked.

- [ ] **Step 3: Record the result**

If the host ignores a bare `systemMessage` on `Stop`, switch that path to `{"hookSpecificOutput": {"hookEventName": "Stop", "preventStop": false}, "systemMessage": …}` and re-verify. Record whichever form the host honoured in `mechanics.md`, since it is now part of the contract.

- [ ] **Step 4: Commit any correction**

```bash
git add -A
git commit -m "fix: use the hook output form the host actually honours"
```

---

## Not in this plan

- Re-pinning the three consumer repositories. That follows the release and uses the procedure already established for v0.3.2 and v0.3.3.
- The host permission classifier that refused the bound `register-root` command.
- Any change to record structure, validation, rendering, or the continuation/completion-audit distinction.
