---
name: agent-handoff
description: Use when unfinished authorized work of any kind (including code, review, UAT, or decisions) must continue in another session, or when the highest authorized scope (including an epic, feature, rollout, or standalone outcome) is complete.
---

# Agent Handoff

Read `docs/agent-handoff/contract.md` before authoring a record. Create a continuation only for unfinished authorized work. Create a completion audit when the highest authorized outcome is complete. The record must be executable from live state, not merely descriptive.

## Gate: Decide whether this work is tracked

A session is not tracked by default, and nothing forces the decision. Register
a root only when the work spans more than one session, will be handed off, or
the user expects to resume it later. Otherwise do nothing: conversation,
investigation, ticket creation, and a fix that finishes within this session
need no lifecycle at all, and no handoff record follows from them. If the
toolkit offers an `AHK-DECLARE` or `AHK-NO-HANDOFF` notice, decide against that
same rule — register a root if the work turns out to need continuity, or run
`lifecycle one-off` to record that it does not. Neither notice blocks; treat
either as a prompt to decide, not as an error to fix.

A user prompt whose first line is `Track: <goal>` registers that goal as the
tracked root; the rest of that prompt is the work. You do not need to infer
tracking when the user has declared it.

If the session is tracked — a lifecycle hook has given you a session key,
challenge, and expected revision — begin by running `lifecycle inspect`. Use
`lifecycle register-root` only to establish a new authorized root, `lifecycle
resume` for its direct successor, and `lifecycle join` only for a permitted
session join. Explicitly validate the candidate against the locked authorization
root and predecessor; an older handoff or an informal request does not authorize
a scope-definition or root change, and this toolkit gives you no way to make one
inside a chain. If the goal itself has changed, say so and let the user declare
the new one. Never invent lifecycle credentials to satisfy a command: with no
tracked session, author the record with `render`, `validate`, and `render-tail`
only.

## Historical records and project overlays

Every handoff record that existed before the current pinned release was adopted
in this repository is a deprecated historical artifact by policy. Do not open,
read, review, validate, migrate, summarize, reconcile, or rewrite those records,
and do not resolve questions from or mark individual legacy files. The contract
applies to new or materially replaced records only. It is the shared minimum:
project overlays may be stricter but cannot loosen or contradict the record-type
decision, the single-copy metadata rule, or the final-response requirements.

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

When the record continues an existing chain, add
`--successor-of handoffs/<predecessor>.md`. The renderer then copies the
lineage and the scope definitions from that predecessor, and your JSON carries
only `record_id`, `timestamp`, the per-scope progress fields, `verification`,
`exact_action`, `next_session_prompt` and the sections.

Budgets the validator enforces, for schema v2: at most eight verification
entries; `check` at most 120 characters and `evidence` or `reason` at most 160;
`remaining_code_detail` at most 240 characters; any one section at most 200
words; the whole record at most 1,200 words. `render` fails on them too, so a
record that will not fit is rejected while you are writing it.

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

A tracked turn that is simply not finished does not need a record. End it on
the exact `progress_response` from `lifecycle inspect` and nothing else; any
other record-less message is blocked. A session that ends on that line is
reported to the user, so use it to pause, not to conclude.

If progress genuinely requires user authority, register a legitimate decision request rather than asking whether to continue or stop. A decision request must name the blocked exact action and recognized authority category; it does not change the authorized root.

Comply with corrective Stop feedback. Blocking feedback names the failed checks after `failed=`. The attempted message may already be displayed before correction, but that display is not a compliant terminal outcome. Make the minimal correction, then send only the renderer-only terminal response required by the current tracked state.

## Stop conditions

Do not claim a valid handoff when gating questions remain, explicit validation fails, live-state conflicts are unresolved, or the exact first action is not executable. Do not turn a completed highest-authorized scope into a continuation just to preserve context.
