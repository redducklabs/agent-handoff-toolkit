---
name: agent-handoff
description: Use when unfinished authorized work of any kind (including code, review, UAT, or decisions) must continue in another session, or when the highest authorized scope (including an epic, feature, rollout, or standalone outcome) is complete.
---

# Agent Handoff

Read `docs/agent-handoff/contract.md` before authoring a record. Create a continuation only for unfinished authorized work. Create a completion audit when the highest authorized outcome is complete. The record must be executable from live state, not merely descriptive.

## Gate: Choose the record type

Identify every active scope and mark exactly one root as `highest_authorized`.

- If concrete work remains in that root, create a continuation from `handoffs/templates/continuation.md`.
- If that root is complete, create a completion audit from `handoffs/templates/completion-audit.md`.
- If a child is complete but its authorized parent still has work, the required record is a parent continuation. A child audit may supplement it, never replace it.

For every scope, distinguish remaining code, remaining review/UAT/decision work, and completed work. Explain both `remaining_code: true` and `false`.

## Gate: Close gating questions

Before writing a continuation, ask and settle every question whose answer could change the first action, target, constraints, or completion condition. Encode the answers under User decisions and leave `next_session_gates` empty.

If the user is unavailable or declines a gating decision, do not present the record as a valid continuation. A later decision is allowed only when an explicit trigger is recorded and it cannot affect the first action.

## Gate: Reconcile live state

Read repository instructions, governing designs/plans/ADRs, and the record. Inspect the live branch, HEAD, index, untracked files, remote state, tracker hierarchy, and rollout state relevant to the work. Preserve newer inherited work before checkout, pull, merge, rebase, clean, stash, or reset. Report material conflicts and do not repeat completed work.

Record snapshot timestamps and name the concrete first action after reconciliation, including its target, constraints, and completion condition. Reconciliation itself is preflight, not that action.

## Gate: Validate the record

Write the record, then run:

```text
python .agent-handoff-toolkit/runner.py validate <record-path>
```

Fix every reported error before calling the record valid. Validation proves structure and internal consistency only. You remain responsible for checking factual accuracy, current hierarchy, verification evidence, and whether the action is correct. A skipped check is `not-run`, never `pass`.

## Gate: Render the final response tail

Run:

```text
python .agent-handoff-toolkit/runner.py render-tail <record-path>
```

Append the output verbatim. For a continuation it contains the stored prompt in a fenced copy/paste block and an absolute clickable link as the final line. For an audit it labels the link **Audit record (not a handoff)** and emits no restart prompt. Put nothing after the generated link.

## Stop conditions

Do not claim a valid handoff when gating questions remain, explicit validation fails, live-state conflicts are unresolved, or the exact first action is not executable. Do not turn a completed highest-authorized scope into a continuation just to preserve context.
