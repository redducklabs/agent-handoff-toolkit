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

An `AHK-CONTEXT-HIGH` notice in a tracked session means the context window is
at least 80% full. Write the continuation now, while there is room to do it
well, and then keep working; it blocks nothing and is not shown again until
context falls below half.

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

## Gate: Assess the candidate with optional Jev

Before final rendering, run optional review for this newly authored candidate.
Read `.agent-handoff-toolkit/jev-experiment.md` for setup and the evidence schema.
When enabled, derive observable acceptance criteria from the immutable authorized outcome;
prepare explicit supporting evidence in a temporary file outside the repository,
then delete it after review. Never weaken the goal or criteria to obtain a pass.
Use the version-3 verification case for detailed feature implementation: discover affected
files and callers through repository searches, account for unchanged shared code,
and link each feature, component and acceptance condition to relevant evidence.
Anchor small coverage obligations to exact spans of the unchanged authorized
outcome and feature requirements; account for every source and preserve qualifiers.
Inspect semantic completeness independently: exact spans prove text accounting
only. Assess each full feature requirement as well as its narrower conditions.
Where feasible, copy each whole original requirement exactly into an assessed
condition; only whole-source identity avoids a model equivalence judgment. Its
implementation still needs proof. Preserve all mapped source contexts. This is
evidence-case version 3; record schema and lifecycle authority remain unchanged.
An inadequate condition inventory requires correction or more evidence; it does
not alone establish missing code. Keep coverage and behavior findings distinct.
Changing every listed file is not a requirement; implementing the behavior is.
Declare each current implementation claim as `implemented` or `pending`.
Selected goals, narrative and evidence leave the machine; full prompts/transcripts
are not collected. Do not include secrets or customer data.

```text
python .agent-handoff-toolkit/runner.py review <record-path> --evidence <temporary-evidence.json>
```

Inspect JSON status, not exit alone: fail, inconclusive and unavailable exit 1. Disabled
adds no routine notice. Error requires correcting local input/configuration.
Pass means supported by supplied evidence, never mechanically verified truth.
Keep results outside factual verification, metadata and the renderer-owned terminal
response; optional advice never changes lifecycle authority.

Read `implementation.state`: `implemented`, `work-remains`, or `cannot-determine`.
This describes supplied implementation evidence, not mechanically proven truth.
For fail, inspect each localized finding and improve the work or unsupported claim.
For inconclusive, gather evidence or independently inspect the affected condition.
Low-confidence answers of either sign are uncertain, never definite defects.
An honest continuation may pass while `implementation.state` is `work-remains`;
its `progress` findings describe pending implementation, not defects in the record.
Continue that work. Future conditions may lack proof; a completion audit cannot.
After at most two corrective reruns, independently examine remaining findings.
Confirmed unfinished work means keep working or create an executable continuation,
never a completion audit. Unresolved ambiguity or insufficient evidence remains
inconclusive. Suspected classifier errors require independent examination
of the actual criterion and evidence; defensible disagreement gets one risk
sentence while retaining the reported assessment and following the deterministic contract.

For unavailable, add one short risk sentence and validate/render normally without
another review for that diagnostic-only edit. Never loop on outages. Internal
faults are toolkit defects, not presumed service outages. Substantive changes
require fresh assessment; no result is cached as acceptance.

## Gate: Render the final response tail

Your entire terminal message is the renderer's output for the record, copied verbatim with nothing before or after it. Do not retype, summarize, or explain it.

```text
python .agent-handoff-toolkit/runner.py render-tail <record-path>
```

That command emits `render_terminal_response`, the exact output `Stop` enforces.

While executable work remains, keep working; a turn never ends as an idle
pause. A tracked turn that cannot continue yet does not need a record. One
that waits on background work, CI or another session ends on the exact
`background-work`, `ci` or `other-session` line from `progress_responses` in
`lifecycle inspect`, and the session resumes when that work reports. One that
answers the user's own message may end on the `reply` line, which is refused
after a turn the host started, such as a task notification or another
session's message. Any other record-less message is blocked. A session that
ends on one of these lines is reported to the user, so use them to wait, not
to conclude.

If progress genuinely requires user authority, register a legitimate decision request rather than asking whether to continue or stop. A decision request must name the blocked exact action and recognized authority category; it does not change the authorized root.

Comply with corrective Stop feedback. Blocking feedback names the failed checks after `failed=`. The attempted message may already be displayed before correction, but that display is not a compliant terminal outcome. Make the minimal correction, then send only the renderer-only terminal response required by the current tracked state.

## Stop conditions

Do not claim a valid handoff when gating questions remain, explicit validation fails, live-state conflicts are unresolved, or the exact first action is not executable. Do not turn a completed highest-authorized scope into a continuation just to preserve context.
