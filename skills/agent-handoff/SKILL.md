---
name: agent-handoff
description: Use when unfinished authorized work of any kind (including code, review, UAT, or decisions) must continue in another session, or when the highest authorized scope (including an epic, feature, rollout, or standalone outcome) is complete.
---

# Agent Handoff

Read `docs/agent-handoff/contract.md` before authoring a record. Create a continuation only for unfinished authorized work. Create a completion audit when the highest authorized outcome is complete. The record must be executable from live state, not merely descriptive.

If the session is tracked — a lifecycle hook has given you a session key,
challenge, and expected revision — begin by running `lifecycle inspect`. Use
`lifecycle register-root` only to establish a new authorized root, `lifecycle
resume` for its direct successor, and `lifecycle join` only for a permitted
session join. Explicitly validate the candidate against the locked authorization
root and predecessor; an older handoff or an informal request does not authorize
a scope-definition or root change. Use an approved transition before changing
either. Never invent lifecycle credentials to satisfy a command: with no tracked
session, author the record with `render`, `validate`, and `render-tail` only.

## Gate: Choose the record type

Identify every active scope and mark exactly one root as `highest_authorized`.

- If concrete work remains in that root, create a continuation from `handoffs/templates/continuation.md`.
- If that root is complete, create a completion audit from `handoffs/templates/completion-audit.md`.
- If a child is complete but its authorized parent still has work, the required record is a parent continuation. A child audit may supplement it, never replace it.

For every scope, distinguish remaining code, remaining review/UAT/decision work, and completed work. Explain both `remaining_code: true` and `false`.

## Gate: Close gating questions

Settle every question whose answer could change the first action, target, constraints, or completion condition before writing a continuation. Encode the answers under User decisions and leave `next_session_gates` empty. If the user is unavailable or declines a gating decision, the record is not a valid continuation.

## Gate: Reconcile live state

Inspect live branch, HEAD, index, untracked files, remote, tracker, and rollout state, and preserve newer inherited work before any checkout, pull, merge, rebase, clean, stash, or reset. Report material conflicts and do not repeat completed work. Reconciliation is preflight; the record still names the concrete action that follows it. The contract states the full sequence.

## Gate: Validate the record

A schema-v2 record stores each fact once, in its visible metadata block. Fill in
the template's metadata and its narrative sections; no section restates
verification, the exact next action, the scope list, or the next-session prompt.
Record one verification entry per gate the next session would rerun, not one per
invocation.

Author the metadata and sections as JSON and let the renderer write the document,
then validate it:

```text
python .agent-handoff-toolkit/runner.py render <record>.json --output handoffs/<record>.md
python .agent-handoff-toolkit/runner.py validate handoffs/<record>.md
```

That JSON is the metadata fields at the top level plus a sibling `sections` map
from heading to body text. `docs/agent-handoff/mechanics.md` gives its exact
shape and how to compute each scope's `scope_definition_digest`.

Fix every reported error before calling the record valid. Validation proves structure and internal consistency only. You remain responsible for factual accuracy, current hierarchy, verification evidence, and whether the action is correct. A skipped check is `not-run`, never `pass`. For a tracked lifecycle session, validate the direct candidate record and its lineage before stopping.

## Gate: Render the final response tail

Your entire terminal message is the renderer's output for the record, copied verbatim with nothing before or after it. Do not retype, summarize, or explain it.

```text
python .agent-handoff-toolkit/runner.py render-tail <record-path>
```

That command emits `render_terminal_response`, the exact output `Stop` enforces; for schema v1 it emits the established v1 tail.

If progress genuinely requires user authority, register a legitimate decision request rather than asking whether to continue or stop. A decision request must name the blocked exact action and recognized authority category; it does not change the authorized root.

Comply with corrective Stop feedback. Blocking feedback names the failed checks after `failed=`. The attempted message may already be displayed before correction, but that display is not a compliant terminal outcome. Make the minimal correction, then send only the renderer-only terminal response required by the current tracked state.

## Stop conditions

Do not claim a valid handoff when gating questions remain, explicit validation fails, live-state conflicts are unresolved, or the exact first action is not executable. Do not turn a completed highest-authorized scope into a continuation just to preserve context.
