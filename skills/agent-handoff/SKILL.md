---
name: agent-handoff
description: Use when unfinished authorized work of any kind (including code, review, UAT, or decisions) must continue in another session, or when the highest authorized scope (including an epic, feature, rollout, or standalone outcome) is complete.
---

# Agent Handoff

Read `docs/agent-handoff/contract.md` before authoring a record. Create a continuation only for unfinished authorized work. Create a completion audit when the highest authorized outcome is complete. The record must be executable from live state, not merely descriptive.

For schema v2, begin by running `lifecycle inspect`. Use `lifecycle register-root`
only to establish a new authorized root, `lifecycle resume` for its direct
successor, and `lifecycle join` only for a permitted session join. Explicitly
validate the candidate against the locked authorization root and predecessor; an
older handoff or an informal request does not authorize a scope-definition or root
change. Use an approved transition before changing either.

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

Keep `next_session_prompt` to essential blockers, settled decisions, and
validation gates not already represented by `exact_action`. Use no more than six
non-empty lines or 120 words. It must not reproduce the handoff document or
contain a Markdown fence. Keep every `exact_action` item trimmed and on one line;
it also must not contain a Markdown fence or handoff-document structure.

## Gate: Validate the record

Write the record, then run:

```text
python .agent-handoff-toolkit/runner.py validate <record-path>
```

Fix every reported error before calling the record valid. Validation proves structure and internal consistency only. You remain responsible for checking factual accuracy, current hierarchy, verification evidence, and whether the action is correct. A skipped check is `not-run`, never `pass`.

For a tracked lifecycle session, validate the direct candidate record and its
lineage before stopping. The renderer is the only source of the terminal response;
do not add a handwritten preamble or a separate final summary.

## Gate: Render the final response tail

For a tracked schema-v2 record, use the complete response from
`render_terminal_response(record_path, record_text)`. The renderer-only response
must be the entire terminal message: put nothing before or after it. Do not use a
tail plus explanatory prose for schema v2.

Use `render-tail` only for schema-v1 compatibility:

```text
python .agent-handoff-toolkit/runner.py render-tail <record-path>
```

For a continuation, any prose before the generated tail may summarize only what
was done and what remains. The generated tail supplies the sole user-action
statement: `This session is stopped because authorized work remains.` followed
by `What you need to do: Start a new session from the continuation handoff
below.` It then provides a fenced block for the new session beginning with
`Continue from handoff`, the absolute record path, the exact next action, and only essential
blockers, decisions, and validation gates. Append that output verbatim and put
nothing after its final link.

For an audit, the generated tail labels the link **Audit record (not a handoff)**
and emits no restart prompt. Only a completed highest-authorized scope may be
described with `None`, `Nothing to do`, or `No action required`.

If progress genuinely requires user authority, register a legitimate decision
request rather than asking whether to continue or stop. A decision request must
name the blocked exact action and recognized authority category; it does not change
the authorized root.

Comply with corrective Stop feedback. The attempted message may already be
displayed before correction, but that display is not a compliant terminal
outcome. Continue with the minimal correction, then send only the renderer-only
terminal response required by the current tracked state.

## Stop conditions

Do not claim a valid handoff when gating questions remain, explicit validation fails, live-state conflicts are unresolved, or the exact first action is not executable. Do not turn a completed highest-authorized scope into a continuation just to preserve context.
