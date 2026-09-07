# Agent handoff contract

This document is normative for schema version 1.

## Record decision

A continuation is valid only when concrete work remains within the highest authorized scope and the next session has an executable first action. Completing a child scope while authorized parent work remains requires a continuation for that parent. A child completion audit may also be useful, but it never substitutes for the parent continuation.

Completion of the highest authorized epic, feature, rollout, or explicitly standalone outcome requires a completion audit. A completion audit must not contain an exact next action or a next-session prompt.

## Active scopes

Every active scope declares `scope_id`, `scope_kind`, `parent_scope_id`, `highest_authorized`, `remaining_work`, `remaining_code`, `remaining_code_detail`, and `status`. Scope kind is `unit`, `issue`, `phase`, `epic`, `rollout`, or `standalone`.

Status is `pending`, `in-progress`, `blocked`, or `complete`. A scope with status `complete` has neither remaining work nor remaining code. The highest-authorized scope of a continuation has a nonterminal status (`pending`, `in-progress`, or `blocked`); the highest-authorized scope of a completion audit has status `complete`.

Exactly one active scope is highest-authorized. Every non-root parent reference resolves within the scope list, and the graph is acyclic. The highest-authorized scope has no parent.

A continuation requires `remaining_work: true` at the highest-authorized scope. A completion audit requires `remaining_work: false` there. `remaining_code: true` implies `remaining_work: true` for the same scope. A true `remaining_work` or `remaining_code` value on a descendant requires the corresponding value to be true on every ancestor. `remaining_code_detail` must explain both `true` and `false` answers. Code, review/UAT/decision work, and completed work remain distinct.

## Question gate

A continuation's `next_session_gates` list must be empty. A question is a gate when its answer could change the first action, its target, constraints, or completion condition. Resolve those questions in the current session before rendering the continuation. A later decision may be recorded only with an explicit trigger and only when it cannot affect the first action.

If the user is unavailable or declines a gating decision, do not present the record as a valid continuation.

## Live-state reconciliation

Repository, remote, tracker, and rollout facts in a record are timestamped snapshots, not authority. The agent continuing from the referenced handoff must:

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

## Canonical derived sections

Structured metadata is authoritative for facts that also appear in narrative sections. The renderer generates these sections; authors do not maintain separate prose copies:

- **Verification evidence** is the complete `verification` list for both record types.
- **Exact next action** is the continuation's complete `exact_action` object.
- **Remaining code by active scope** is the continuation's complete `active_scopes` list.
- **Next-session prompt** is the continuation's exact `next_session_prompt` value in a fenced `text` block. It contains only essential blockers, settled decisions, and validation gates not already represented by `exact_action`.

The structured sections use deterministic JSON with UTF-8 characters preserved, keys sorted, two-space indentation, LF line endings, and a fenced `json` block long enough not to collide with content. The prompt must be non-empty, use LF line endings, contain no unsafe control or line-separator characters or leading or trailing whitespace, and use at most six non-empty lines, 120 words, and 1,200 characters. It must not contain a Markdown fence or handoff-document structure. The renderer does not strip or otherwise normalize it.

Each `exact_action` field is either non-empty text or a non-empty list of text
items, preserving schema-v1 compatibility. Every item is trimmed, single-line,
and free of unsafe control or line-separator characters, Markdown fences, and
handoff-document structure. The tail joins list items with `; `.

The validator normalizes record-document line endings to LF for parsing, regenerates every derived section from metadata, and requires exact equality with the visible section. A contradictory prose summary is invalid even when each representation would be valid in isolation.

## Final response

For a continuation, the response tail contains exactly:

1. One fenced `text` block beginning with `Continue from handoff` and the absolute handoff path.
2. The exact action, target, constraints, and completion gate from metadata.
3. Only the stored essential blockers, decisions, and validation gates.
4. An absolute clickable Markdown link to the continuation as the final non-whitespace line.

The complete generated tail, including its fence and link, is limited to 300 words and 2,400 characters. It must not reproduce the handoff document. The detailed record remains the source of truth; the tail is only a concise pointer and executable start. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt.

## Enforcement boundary

The validator enforces record type, section shape, scope consistency, verification classifications, empty gates, and actionable field presence. It cannot prove that recorded facts are true, that all source code was inspected, that tracker hierarchy is current, or that the chosen action is correct. The skill requires those checks; tooling must not claim otherwise.

Automatic host hooks are advisory and fail open. Explicit CLI commands fail visibly and return a non-zero status for invalid input.
