# Agent handoff contract

This document is normative for schema version 2 and is what an author reads
before writing a record. `mechanics.md` carries the exact formats the toolkit
enforces — lineage fields, canonical JSON, the generated response, lifecycle
enforcement, the enforcement boundary, and the schema version 1 compatibility
appendix. The validator rejects a record that violates them, so an author does
not need to reproduce them from memory.

## Record decision

A continuation is valid only when concrete work remains within the highest authorized scope and the next session has an executable first action. Completing a child scope while authorized parent work remains requires a continuation for that parent. A child completion audit may also be useful, but it never substitutes for the parent continuation.

Completion of the highest authorized epic, feature, rollout, or explicitly standalone outcome requires a completion audit. A completion audit must not contain an exact next action or a next-session prompt.

## Active scopes

Every active scope declares `scope_id`, `scope_kind`, `parent_scope_id`, `highest_authorized`, `remaining_work`, `remaining_code`, `remaining_code_detail`, and `status`. Scope kind is `unit`, `issue`, `phase`, `epic`, `rollout`, or `standalone`. Each schema-v2 scope additionally carries an immutable `scope_definition` and `scope_definition_digest`; `mechanics.md` defines them and the root immutability they protect.

Status is `pending`, `in-progress`, `blocked`, or `complete`. A scope with status `complete` has neither remaining work nor remaining code. The highest-authorized scope of a continuation has a nonterminal status (`pending`, `in-progress`, or `blocked`); the highest-authorized scope of a completion audit has status `complete`.

Exactly one active scope is highest-authorized. Every non-root parent reference resolves within the scope list, and the graph is acyclic. The highest-authorized scope has no parent.

A continuation requires `remaining_work: true` at the highest-authorized scope. A completion audit requires `remaining_work: false` there. `remaining_code: true` implies `remaining_work: true` for the same scope. A true `remaining_work` or `remaining_code` value on a descendant requires the corresponding value to be true on every ancestor. `remaining_code_detail` must explain both `true` and `false` answers. Code, review/UAT/decision work, and completed work remain distinct.

State `remaining_code_detail` as one sentence naming what remains and where. A scope is a unit of authority, not a progress narrative; the narrative belongs in **Completed work** and **Incomplete work and risks**.

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

## Metadata is the only copy

A schema-v2 record begins with one visible fenced `json agent-handoff-metadata`
block holding schema version, record type, timestamp, lineage, scope
declarations, question gates, verification classifications, exact-action fields,
and the stored next-session prompt. A completion audit places its sentinel
before that block.

That block is the sole copy of every structured fact. A schema-v2 record
contains no narrative restatement of verification, the exact next action, the
scope list, or the next-session prompt, and no section repeats a value that
already has a metadata field. Schema-v1 records keep their metadata comment and
their derived sections; see the compatibility appendix in `mechanics.md`.

## Continuation sections

A schema-v2 continuation contains exactly these level-two sections, in order:

1. Objective
2. Authoritative references
3. User decisions
4. Repository state
5. Completed work
6. Incomplete work and risks
7. External effects

**User decisions** holds settled decisions and the constraints they impose; other
fields refer to a decision by its short label rather than restating it.
**Repository state** is branch, HEAD, worktree path, and remote or pull-request
status — the reader re-inspects live state anyway. **Incomplete work and risks**
lists only what no scope's `remaining_code_detail` already states: risks,
blockers, and cross-scope conflicts.

## Completion-audit sections

An audit begins with this exact sentinel:

> Audit record — not a handoff. Do not use this file to start or continue a session.

It contains exactly these level-two sections, in order:

1. Completed objective
2. Authoritative references
3. User decisions
4. Final repository state
5. Completed work
6. Known risks or separately tracked follow-ups
7. External effects

Its metadata identifies the completed highest-authorized scope and authorization basis. It must not contain continuation-only fields or sections.

## Verification evidence

Every verification entry has a command or check, a result of `pass`, `fail`, or `not-run`, and evidence or a reason. Never convert `not-run` into `pass`. A failing check remains visible until it is rerun successfully or explicitly carried as a known risk.

Record one entry per gate the next session would rerun, not one per invocation.
Omit any entry a broader entry already covers: a passing full-suite run subsumes
the individual suites inside it. Keep `check` at or under 120 characters and
`evidence` or `reason` at or under 160 characters. The entry states the gate and
its outcome, not a log of the session that ran it.

## Exact next action

Each `exact_action` field is either non-empty text or a non-empty list of text
items, preserving schema-v1 compatibility. Every item is trimmed, single-line,
and free of unsafe control or line-separator characters, Markdown fences, and
handoff-document structure. Each `exact_action` item must not be a no-action
assertion. `constraints` names the governing decisions and prohibitions; it does
not restate **User decisions** in full.

The `next_session_prompt` contains only essential blockers, settled decisions,
and validation gates not already represented by `exact_action`. It must be
non-empty, use LF line endings, contain no unsafe control or line-separator
characters or leading or trailing whitespace, and use at most six non-empty
lines, 120 words, and 1,200 characters. It must not contain a Markdown fence or
handoff-document structure.

## Final response

A continuation response says `This session is stopped because authorized work
remains` and `What you need to do: Start a new session from the continuation
handoff below`, carries a `Continue from handoff` block with the absolute path,
names the exact next action, target, constraints, and completion gate, lists
only the stored essential blockers, decisions, and validation gates, and ends
with an absolute clickable Markdown link. It must not reproduce the handoff
document. Completion responses label their link **Audit record (not a handoff)**
and generate no restart prompt.

The renderer is the only source of a schema-v2 terminal response: there is no
handwritten preamble, and the normalized terminal message must have byte-exact
equality with the rendered response. Do not retype any of it. `mechanics.md`
states the exact generated shape and its budget.

## Enforcement boundary

The validator enforces record type, section shape, scope consistency, lineage,
verification classifications, empty gates, and actionable field presence. It
does not mechanically prove that recorded facts are true, arbitrary
natural-language scope interpretation is correct, all source code was inspected,
tracker hierarchy is current, or completion is semantically true. The skill
requires those checks; tooling must not claim otherwise. `mechanics.md` states
the enforcement model, the permitted stop outcomes, and the hook failure
policies.
