# Jev Implementation Evidence Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this approved design inline. Steps use checkbox syntax for tracking.

**Goal:** Assess implemented features, remaining implementation, and insufficient evidence without treating uncertain judgments as definite defects.

**Architecture:** A versioned, explicit verification case links components and acceptance conditions to supplied evidence. Narrow semantic judgments are combined in code into feature and outcome assessments. Existing host adapters use the same voluntary review CLI; no hook or lifecycle decision depends on Jev.

**Tech Stack:** Python 3.11+, standard library, existing TypeSafe Choice transport.

**Spec:** The human-approved design and clarification in this chat: file/component, integration, feature, and authorized-outcome verification; symmetric uncertainty; an independently labeled 80-case development/held-out comparison; both hosts; STS before other consumers.

## Global Constraints

- Preserve schema-v2 continuation versus completion-audit and the immutable authorized outcome.
- No automatic source uploads, transcripts, customer data, or credentials in cases, fixtures, reports, or logs.
- Active configuration and credential location remain ignored and machine-specific.
- Retain the validated Choice wire contract and conservative rounding handling.
- Jev remains advisory; no semantic truth claim or new completion gate.
- Source edits use apply_patch; all README verification and WSL tests run before publishing.

## Review Focus

- Clean shared implementation or intentionally unchanged files must not imply missing work.
- Honest pending work in a continuation is progress, not a false defect finding.
- Missing caller, alternate path, or acceptance condition must not disappear behind passed tests.
- Stale file evidence, unreferenced evidence, and uncertainty must not produce supported completion.
- Untrusted evidence must not control questions, configuration, credentials, or lifecycle state.

### Task 1: Direct questions and symmetric uncertainty

**Files:** src/agent_handoff_toolkit/review_rubrics.py, review.py; tests/test_review_rubrics.py, test_review.py.

**Interfaces:** Keep legacy criteria/evidence input readable. Return pass/fail/inconclusive with fixed findings and an implementation-state summary.

- [ ] Add a regression asserting a negative answer at confidence 0.445 is inconclusive, not deficient; watch it fail.
- [ ] Build direct per-condition instructions containing the condition and resolved evidence, not positional reference chains. Test that unrelated evidence is excluded from the question's selected evidence.
- [ ] Preserve fixed record coverage and continuation consistency checks, removing an authoritative holistic completion verdict.
- [ ] Implement confidence-first reduction, preserving confidence rounding safeguards. Missing proof differs from demonstrated unfinished work.
- [ ] Run focused review tests, then the suite; commit tested changes.

### Task 2: Hierarchical verification cases

**Files:** new src/agent_handoff_toolkit/verification_case.py; review.py, review_rubrics.py; new tests/test_verification_case.py.

**Interfaces:** Version-2 evidence extends criteria/evidence with explicit feature/component ownership, expected locations and discovery evidence. Optional path/digest bindings are checked locally against project_root, with no implicit uploads. Fixed reports expose IDs and locations, not source text.

- [ ] Add failing tests for omitted/duplicate ownership, unknown references, traversal/symlink escape, stale digest, and unchanged shared components.
- [ ] Implement strict bounded schema, local bindings, and direct component/coverage questions.
- [ ] Aggregate implementation states in code; any confirmed required unfinished component keeps its feature unfinished, uncertainty prevents supported completion, and completed children cannot establish an uncovered root.
- [ ] Test pending continuation work versus contradictory completion claims and fixed report privacy.
- [ ] Run focused and full tests; commit tested changes.

### Task 3: Evaluation and author workflow

**Files:** new evaluation module and opt-in CLI; synthetic labeled corpus; tests; docs/jev-experiment.md and shared author skill; distribution/manifest.json.

**Interfaces:** Evaluate development and held-out partitions without storing raw requests/responses; output fixed reports, confusion counts, abstention coverage, latency and model/digests. Existing review command stays shared across Claude Code and Codex.

- [ ] Add failing metric tests for missed defects, false defects, abstention, disabled/unavailable service and duplicate/leaking groups.
- [ ] Prepare 80 synthetic cases (40 development, 40 held-out), each partition with 16 clean, 16 incomplete, 8 insufficient-evidence cases; freeze independent labels before live calls.
- [ ] Implement opt-in comparison and option-order/context sensitivity checks without extra automatic retries.
- [ ] Update workflow guidance to discover affected locations and construct sanitized evidence cases; manually synchronize manifest hashes.
- [ ] Run development evaluation, record changes and freeze candidate before opening held-out results. Qualification requires every planted critical omission detected, at most one false defect among 16 clean held-out cases, and no supported insufficient-evidence case; report uncertainty separately.

### Task 4: Both hosts and STS qualification

**Files:** host boundary tests; ignored qualification reports; release metadata/docs only after qualification.

- [ ] Run installed-command tests and available real-host acceptance on both Claude Code and Codex; distinguish host discovery from direct command success.
- [ ] Construct sanitized cases from STS engineering evidence in an isolated STS worktree; no other chat messages or active checkout repair.
- [ ] Freeze independent STS labels before assessment; compare old/new evaluator and controlled defective variants without changing actual implementation.
- [ ] Run full README commands and WSL POSIX suite, obtain fresh independent review, and address important findings with RED/GREEN tests.
- [ ] Publish a reviewed PR and release only after qualification; keep other consumer updates paused until STS is validated.

## Execution decision

The human explicitly approved implementation of this design. Execute inline without another design or plan confirmation; routine implementation choices are within that authorization. If the live benchmark does not qualify, report it honestly and continue bounded diagnosis rather than weakening labels or thresholds.
