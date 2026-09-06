# Agent handoff contract

This document is normative for schema version 1.

## Record decision

A continuation is valid only when concrete work remains within the highest authorized scope and the next session has an executable first action. Completing a child scope while authorized parent work remains requires a continuation for that parent. A child completion audit may also be useful, but it never substitutes for the parent continuation.

Completion of the highest authorized epic, feature, rollout, or explicitly standalone outcome requires a completion audit. A completion audit must not contain an exact next action or a next-session prompt.

## Active scopes

Every active scope declares `scope_id`, `scope_kind`, `parent_scope_id`, `highest_authorized`, `remaining_work`, `remaining_code`, `remaining_code_detail`, and `status`. Scope kind is `unit`, `issue`, `phase`, `epic`, `rollout`, or `standalone`.

Exactly one active scope is highest-authorized. Every non-root parent reference resolves within the scope list, and the graph is acyclic. The highest-authorized scope has no parent.

A continuation requires `remaining_work: true` at the highest-authorized scope. A completion audit requires `remaining_work: false` there. `remaining_code_detail` must explain both `true` and `false` answers. Code, review/UAT/decision work, and completed work remain distinct.

## Question gate

A continuation's `next_session_gates` list must be empty. A question is a gate when its answer could change the first action, its target, constraints, or completion condition. Resolve those questions in the current session before rendering the continuation. A later decision may be recorded only with an explicit trigger and only when it cannot affect the first action.

If the user is unavailable or declines a gating decision, do not present the record as a valid continuation.

## Live-state reconciliation

Repository, remote, tracker, and rollout facts in a record are timestamped snapshots, not authority. The next-session prompt must direct the agent to:

1. Read repository instructions, governing design/plan/ADRs, and the record.
2. Inspect live branch, HEAD, index, untracked files, remote state, tracker hierarchy, and rollout state relevant to the work.
3. Preserve newer inherited state before checkout, pull, merge, rebase, clean, stash, or reset.
4. Report material conflicts between the record and live state.
5. Continue without repeating completed work.

Reconciliation is preflight, not the exact next action. The continuation also names the concrete action after successful reconciliation, including its target, constraints, and completion condition.

## Continuation sections

A continuation contains exactly these level-two sections, in order:

1. Objective
2. Authoritative references
3. User decisions
4. Repository state
5. Completed work
6. Verification evidence
7. Incomplete work and risks
8. Exact next action
9. External effects
10. Remaining code by active scope
11. Next-session prompt

The document begins with a machine-readable metadata comment containing schema version, record type, timestamp, scope declarations, question gates, verification classifications, exact-action fields, and the stored next-session prompt.

## Completion-audit sections

An audit begins with this exact sentinel:

> Audit record — not a handoff. Do not use this file to start or continue a session.

It contains exactly these level-two sections, in order:

1. Completed objective
2. Authoritative references
3. User decisions
4. Final repository state
5. Completed work
6. Verification evidence
7. Known risks or separately tracked follow-ups
8. External effects

Its metadata identifies the completed highest-authorized scope and authorization basis. It must not contain continuation-only fields or sections.

## Verification evidence

Every verification entry has a command or check, a result of `pass`, `fail`, or `not-run`, and evidence or a reason. Never convert `not-run` into `pass`. A failing check remains visible until it is rerun successfully or explicitly carried as a known risk.

## Final response

For a continuation, the response tail is generated from the stored prompt and contains exactly:

1. The prompt in a fenced copy/paste block.
2. An absolute clickable Markdown link to the continuation as the final non-whitespace line.

The record stores repository-relative paths; the response renderer resolves the host-specific absolute path. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt.

## Enforcement boundary

The validator enforces record type, section shape, scope consistency, verification classifications, empty gates, and actionable field presence. It cannot prove that recorded facts are true, that all source code was inspected, that tracker hierarchy is current, or that the chosen action is correct. The skill requires those checks; tooling must not claim otherwise.

Automatic host hooks are advisory and fail open. Explicit CLI commands fail visibly and return a non-zero status for invalid input.
