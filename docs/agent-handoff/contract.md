# Agent handoff contract

This document is normative for schema version 2. Schema-v2 records are the current
format; the schema version 1 compatibility appendix defines limited treatment for
historical records.

## Record decision

A continuation is valid only when concrete work remains within the highest authorized scope and the next session has an executable first action. Completing a child scope while authorized parent work remains requires a continuation for that parent. A child completion audit may also be useful, but it never substitutes for the parent continuation.

Completion of the highest authorized epic, feature, rollout, or explicitly standalone outcome requires a completion audit. A completion audit must not contain an exact next action or a next-session prompt.

## Active scopes

Every active scope declares `scope_id`, `scope_kind`, `parent_scope_id`, `highest_authorized`, `remaining_work`, `remaining_code`, `remaining_code_detail`, and `status`. Scope kind is `unit`, `issue`, `phase`, `epic`, `rollout`, or `standalone`.

Status is `pending`, `in-progress`, `blocked`, or `complete`. A scope with status `complete` has neither remaining work nor remaining code. The highest-authorized scope of a continuation has a nonterminal status (`pending`, `in-progress`, or `blocked`); the highest-authorized scope of a completion audit has status `complete`.

Exactly one active scope is highest-authorized. Every non-root parent reference resolves within the scope list, and the graph is acyclic. The highest-authorized scope has no parent.

A continuation requires `remaining_work: true` at the highest-authorized scope. A completion audit requires `remaining_work: false` there. `remaining_code: true` implies `remaining_work: true` for the same scope. A true `remaining_work` or `remaining_code` value on a descendant requires the corresponding value to be true on every ancestor. `remaining_code_detail` must explain both `true` and `false` answers. Code, review/UAT/decision work, and completed work remain distinct.

## Schema-v2 lineage and root immutability

Every schema-v2 record contains `record_id`, `authorization_id`,
`authorized_root_scope_id`, `predecessor`, `authorization_evidence`, and
`transition`. `record_id` identifies this record; `authorization_id` identifies
its chain. `authorized_root_scope_id` identifies the sole locked,
highest-authorized root and is immutable for a non-transition successor.

`predecessor` is `null` only for an initial authorization; otherwise it contains
the direct predecessor's record ID, normalized absolute path, and SHA-256 digest.
`authorization_evidence` records the bounded user-turn reference and evidence
HMAC for the initial authorization, v1 adoption, or approved transition. A
successor must directly reference the live predecessor; no directory scan or
newest-record heuristic is authorization.

Each schema-v2 scope additionally contains an immutable `scope_definition`
(`title` and `outcome`) and matching `scope_definition_digest`. The digest covers
the scope ID, kind, parent, and normalized definition. Status and remaining-work
fields remain progress fields, but a successor cannot alter an inherited scope's
definition, kind, parent, or digest without an approved transition.

A transition records old and new authorization IDs and roots plus the proposal
turn, adjacent approval turn, and evidence HMAC. A model command cannot assert
approval. The immediately following user response must approve the exact proposal;
ambiguous, qualified, negative, or non-adjacent replies leave the proposal pending.
This root immutability preserves inherited authority while allowing explicit,
approved transition evidence.

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
handoff-document structure. Each `exact_action` item must not be a no-action
assertion. The tail joins list items with `; `.

The validator normalizes record-document line endings to LF for parsing, regenerates every derived section from metadata, and requires exact equality with the visible section. A contradictory prose summary is invalid even when each representation would be valid in isolation.

## Final response

For a continuation, the response tail contains exactly:

1. `This session is stopped because authorized work remains.`
2. `What you need to do: Start a new session from the continuation handoff below.`
3. One fenced `text` block beginning with `Continue from handoff` and the absolute handoff path for use in the new session.
4. The exact action, target, constraints, and completion gate from metadata.
5. Only the stored essential blockers, decisions, and validation gates.
6. An absolute clickable Markdown link to the continuation as the final non-whitespace line.

The complete generated tail, including its fence and link, is limited to 300 words and 2,400 characters. It must not reproduce the handoff document. The detailed record remains the source of truth; the tail is only a concise pointer and executable start. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt.

A schema-v2 terminal response is entirely generated: the renderer is the only
source, there is no handwritten preamble, and the normalized terminal message
must have byte-exact equality with the rendered response. For a continuation, the
response is the canonical generated continuation response; for an audit it is only
the canonical audit link. A no-action statement such as `None`, `Nothing to do`,
or `No action required` is valid only when the highest authorized scope is complete
and the response links an audit rather than a continuation.

## Lifecycle enforcement

Tracked sessions have four permitted stop outcomes: continue working, await a
legitimate decision request, create a valid continuation, or complete the
authorized root with an audit. Executable work remaining is not a decision
request. A decision request must name one bounded question, blocked action, and
recognized authority category; it cannot change the root or scope definition.

`Stop` verifies the direct candidate, lineage, locked root, open-decision state,
and the renderer's complete response. An attempted assistant message may already
be displayed before `Stop` runs. The hook cannot retract that transient
display, but it returns corrective continuation feedback and requires a corrected
response or a visibly failed policy outcome.

Informational hooks fail open. Tracked lifecycle hooks fail closed on
`UserPromptSubmit` and `Stop`, including corrupt tracked state, unreadable
candidates, missing owned runtime files, and lifecycle validation exceptions.
An untracked tool-free informational response may fail open. Successful lifecycle
checks emit no routine model context.

## Enforcement boundary

The validator enforces record type, section shape, scope consistency, lineage,
verification classifications, empty gates, and actionable field presence. It does
not mechanically prove that recorded facts are true, arbitrary natural-language
scope interpretation is correct, all source code was inspected, tracker hierarchy
is current, or completion is semantically true. The skill requires those checks;
tooling must not claim otherwise.

No model call is required for enforcement. Deterministic lifecycle state, lineage,
validation, rendering, and bounded transcript-reference checks are the hard gate;
optional evaluators cannot approve a transition or replace evidence. Explicit CLI
validation fails visibly and returns a non-zero status for invalid input.

## Schema version 1 compatibility appendix

Schema-v1 records remain parseable and render with their established v1 tail. They
do not acquire invented lineage or root evidence. A lifecycle command may adopt a
v1 record only through the explicit v1-adoption path and corresponding
authorization evidence; new and materially replaced records use schema version 2.
This appendix preserves historical compatibility without weakening schema-v2 root
immutability or tracked lifecycle enforcement.
