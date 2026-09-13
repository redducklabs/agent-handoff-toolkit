# Agent handoff mechanics

This document is normative for the formats and enforcement the toolkit applies.
It is the reference for tooling, tests, and anyone diagnosing a blocked session.
An author writing a record needs `contract.md`; the validator enforces
everything below, so an author does not reproduce it from memory.

## Metadata block forms

A schema-v2 record begins with a visible fenced block opened by
```` ```json agent-handoff-metadata ```` and closed by a bare ```` ``` ````
line. A completion audit places its sentinel and a blank line before that block.
The block stays visible so a reviewer reading rendered Markdown still sees
verification, the exact next action, and the scope list. A rendered metadata
object never contains a bare fence line, because JSON escapes every newline.

A schema-v1 record begins with the `<!-- agent-handoff-metadata` comment and
closes it with `-->`. Each schema version has exactly one canonical form; a
record that declares one version and uses the other form is invalid, and a
record carrying a visible block plus a metadata comment is invalid because the
comment would be invisible in rendered Markdown while claiming to be metadata.

The visible block is recognized only at a record's fixed metadata position: the
start of a continuation, or directly after a completion audit's sentinel and its
blank line. The same text anywhere else is ordinary body content and cannot
displace the real block, so a narrative section may quote it.

Metadata is deterministic JSON with UTF-8 characters preserved, keys sorted,
two-space indentation, and LF line endings. Any other fenced block the renderer
emits uses a fence long enough not to collide with its content. The validator
normalizes record-document line endings to LF for parsing. The renderer does not
strip or otherwise normalize the stored next-session prompt.

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

## Final response

For a continuation, the response tail contains exactly:

1. `This session is stopped because authorized work remains.`
2. `What you need to do: Start a new session from the continuation handoff below.`
3. One fenced `text` block beginning with `Continue from handoff` and the absolute handoff path for use in the new session.
4. The exact action, target, constraints, and completion gate from metadata.
5. Only the stored essential blockers, decisions, and validation gates.
6. An absolute clickable Markdown link to the continuation as the final non-whitespace line.

The complete generated tail, including its fence and link, is limited to 300 words and 2,400 characters. It must not reproduce the handoff document. The detailed record remains the source of truth; the tail is only a concise pointer and executable start. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt. The tail joins `exact_action` list items with `; `.

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

Blocking feedback names the issue code, the corrective action, and — after
`failed=` — the validator's own codes for the checks that failed. Those codes
are a closed vocabulary of identifiers, appended only when the whole line still
fits the feedback budget, so naming them never costs another issue its code or
its action. The adapter emits no other issue text: free-form summaries never
reach the host, and no prompt, reply, or transcript content can.

Informational hooks fail open. Tracked lifecycle hooks fail closed on
`UserPromptSubmit` and `Stop`, including corrupt tracked state, unreadable
candidates, missing owned runtime files, and lifecycle validation exceptions.
An untracked tool-free informational response may fail open. Successful lifecycle
checks emit no routine model context.

The host's report of the turn's final assistant text is turn content, not a
lifecycle field. It is absent or null when the turn ended on a tool call, empty
when the turn emitted no text, and otherwise whatever prose and spacing the
model produced. None of that is a runtime fault. A value the lifecycle cannot
use is read as no terminal response at all, which every later check already
blocks on; it is never trimmed or truncated into something that could pass as
the rendered response. Only a structurally invalid value — a wrong type, or a
control or line-separator character — remains a normalization failure.

A blocked `Stop` always advances the correction count, including one that
failed before the session registered a root. A session without a chain signs
that count with its own session key. Without this a pre-root session whose
`Stop` kept failing could never arm `AHK-STOP-CIRCUIT`, leaving it unable to act
and unable to reach any legal terminal outcome.

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

A schema-v1 continuation contains exactly these level-two sections, in order:
Objective, Authoritative references, User decisions, Repository state, Completed
work, Verification evidence, Incomplete work and risks, Exact next action,
External effects, Remaining code by active scope, Next-session prompt. A
schema-v1 audit contains Completed objective, Authoritative references, User
decisions, Final repository state, Completed work, Verification evidence, Known
risks or separately tracked follow-ups, External effects.

In schema v1 the renderer derives four of those sections from metadata and the
validator requires exact equality with the visible section: **Verification
evidence** is the complete `verification` list, **Exact next action** is the
complete `exact_action` object, **Remaining code by active scope** is the
complete `active_scopes` list, and **Next-session prompt** is the exact
`next_session_prompt` value in a fenced `text` block. A contradictory prose
summary is invalid even when each representation would be valid in isolation.
Schema v2 removes these sections because each one was a second copy of a
metadata field.
## Authoring input for `render`

`render` reads one JSON object and writes the record document. That object is the
record's metadata fields at the top level plus a sibling `sections` map from
section heading to body text. `sections` is not part of the emitted metadata
block, and the renderer emits the sections in canonical order regardless of the
order supplied.

```json
{
  "schema_version": 2,
  "record_type": "continuation",
  "timestamp": "2026-09-11T12:00:00Z",
  "record_id": "record-001",
  "authorization_id": "auth-001",
  "authorized_root_scope_id": "root-scope",
  "predecessor": null,
  "authorization_evidence": { "kind": "initial-user-turn", "user_turn_ref": "turn-001", "proposal_turn_ref": null, "evidence_hmac": "<64 hex>" },
  "transition": null,
  "active_scopes": [ { "...": "one entry per scope" } ],
  "next_session_gates": [],
  "verification": [ { "check": "...", "result": "pass", "evidence": "..." } ],
  "exact_action": { "action": "...", "target": "...", "constraints": "...", "completion_condition": "..." },
  "next_session_prompt": "- ...",
  "sections": { "Objective": "...", "...": "one entry per required section" }
}
```

A completion audit replaces `exact_action`, `next_session_prompt`, and
`next_session_gates` with `completed_scope_id` and `authorization_basis`, and
uses the audit section list.

Compute each scope's `scope_definition_digest` with
`agent_handoff_toolkit.lineage.scope_definition_digest(scope)`, which covers the
scope's `scope_id`, `scope_kind`, `parent_scope_id`, and `scope_definition` under
the canonical JSON rules above. The digest is authored, never recomputed by the
renderer: recomputing it for an altered definition would defeat root immutability.

## Lifecycle commands and untracked authoring

`lifecycle inspect`, `register-root`, `resume`, and `join` act on a host-tracked
session and require the session key, challenge, and expected revision that a
tracked `UserPromptSubmit` supplies. They are the entry point whenever the
session is tracked. Authoring a record outside a tracked session — no session key
or challenge is available — uses `render`, `validate`, and `render-tail` only;
lifecycle credentials are never fabricated to satisfy a command.

## Registering the first root

Before a root exists, every mutation-capable tool is denied, so the session has
no shell with which to canonicalize and encode a scope definition. The denial
itself supplies the machine. Attempt `register-root` with the semantic slots as
plain text:

```
python <runner> lifecycle register-root --scope-id <id> --scope-kind <kind> \
  --scope-title "<title>" --scope-outcome "<outcome>"
```

The denial validates those slots, encodes the definition, and returns the
complete bound command — session key, challenge, revision and
`--scope-definition-b64` already filled in — after `Command:`. Run that command
verbatim. Nothing about what may execute changes: the returned command is
accepted only because it satisfies the same fixed-token parser as before, and
the hook verifies that before offering it.

The three semantic slots are still the author's. Derive them from the
initiating user request; the toolkit supplies the encoding, never the meaning.
Because the feedback channel is bounded, a definition whose encoding will not
fit is rejected as `definition-too-long` rather than silently dropped.

A pre-root denial names the check that rejected the attempt after `failed=`,
using the same closed-vocabulary convention as blocking `Stop` feedback —
`definition-b64-alphabet`, `definition-json-noncanonical`, `scope-kind` and the
rest. A cause the hook can repair on its own, such as a stale challenge, is
corrected in the returned command instead of being named.
