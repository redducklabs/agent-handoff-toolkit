# Agent Handoff Toolkit Implementation Plan

**Goal:** Deliver a tested, public v1 contract and cross-agent toolkit without mutating consumer repositories.

**Architecture:** A dependency-free Python package owns schemas, Markdown parsing/rendering, validation, response-tail generation, and context milestones. Claude Code and Codex adapters only translate hook payloads. Versioned distribution artifacts carry the skill and configuration fragments.

**Tech stack:** Python 3.11+ standard library, `unittest`, GitHub Actions.

## Task 1: Package and schema tests

Create `pyproject.toml`, package modules, and tests for exact section sets, record dispatch, active-scope chains, highest-scope state, verification classifications, empty continuation gates, and continuation/audit mutual exclusion. Write failing tests first, then the smallest implementation that passes.

```python
@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

def validate_markdown(text: str) -> list[ValidationIssue]: ...
```

## Task 2: Deterministic rendering and CLI

Add fixtures and tests for JSON-to-Markdown rendering, parse/render round trips, explicit non-zero validation failures, and exact response tails for Windows/POSIX absolute paths, spaces, and Unicode. Implement `validate`, `render`, and `render-tail` commands.

```python
def render_record(data: Mapping[str, object]) -> str: ...
def render_tail(record_path: Path, text: str) -> str: ...
```

## Task 3: Context health and host hooks

Test 50/60/70 milestone selection, compaction re-arming, atomic session-keyed state, malformed-input fail-open behavior, Claude file-path payloads, and Codex `apply_patch` add/update/delete directives. Implement the shared policy core and thin adapters.

```python
def observe_context(session_id: str, percentage: float, state_dir: Path) -> str | None: ...
def run_hook(platform: str, event: str, raw: str, repo_root: Path) -> str: ...
```

## Task 4: Skill and distribution artifacts

Add one canonical `skills/agent-handoff/SKILL.md`, Claude and Codex hook fragments, record templates, and a distribution manifest. Test that the skill contains the mandatory decision, question, reconciliation, validation, and final-tail gates and that manifest hashes match managed artifacts.

## Task 5: CI, documentation, review, and integration

Run unit tests and byte-compilation locally on Windows. Add CI on the organization's required `redducklabs-runners` fleet across supported Python versions using current stable official actions; the fleet label does not guarantee a particular operating-system matrix. Perform independent requirements and code-quality reviews, resolve findings, scan tracked content for obvious secrets, push the feature branch, open a pull request, and monitor every check to green before requesting integration.
