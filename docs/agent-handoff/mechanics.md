# Agent handoff mechanics

This document is normative for the formats and enforcement the toolkit applies.
It is the reference for tooling, tests, and anyone diagnosing a blocked session.
An author writing a record needs `contract.md`; the validator enforces
everything below, so an author does not reproduce it from memory.

It states what the toolkit does, not why. The reasoning behind each rule, and
the failures each was written against, live in the toolkit repository's
`docs/design/mechanics-rationale.md` and are deliberately not distributed: a
session diagnosing a block needs the rule, not its history.

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

`transition` is `null` in every record of an ordinary chain, and no record in
any consumer has ever carried one. A transition exists only to re-root a live
chain under explicit, adjacent user approval; the toolkit repository's
`docs/design/mechanics-rationale.md` states how that approval is established. If
the goal itself has changed, the plainer path is to say so and let the user
declare the new one.

## Final response

For a continuation, the response tail contains exactly:

1. `Stopping here. Work remains on "<root title>".`, naming the authorized
   root's immutable definition.
2. `Progress: <complete> of <total> scopes complete.`, counted over
   `active_scopes` and omitted when there is only one scope.
3. `What you need to do: start a new session and paste the block below.`
4. One fenced `text` block beginning with `Continue from handoff` and the absolute handoff path for use in the new session.
5. The exact action, target, constraints, and completion gate from metadata, labelled `Next action`, `Where`, `Constraints` and `Done when`.
6. The stored essential blockers, decisions, and validation gates, under `Also:`.
7. An absolute clickable Markdown link to the continuation as the final non-whitespace line.

A completion audit's response is three lines and the link: what was completed
and its outcome, the verification tally (`<p> passed, <f> failed, <n> not
run`), and `Nothing further is required of you.` A failing entry is still
counted there; the contract forbids hiding one, and a tally that omitted it
would report a completion the evidence does not support. A bare link said
none of this.

A decision request's response keeps its four-line structure, which the HMAC
verification depends on, and reads `Paused: I need one decision from you.`
and `This blocks: <blocked action field>`.

The complete generated tail, including its fence and link, is limited to 300 words and 2,400 characters. It must not reproduce the handoff document. The detailed record remains the source of truth; the tail is only a concise pointer and executable start. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt. The tail joins `exact_action` list items with `; `.

A schema-v2 terminal response is entirely generated: the renderer is the only
source, there is no handwritten preamble, and the normalized terminal message
must have byte-exact equality with the rendered response. For a continuation, the
response is the canonical generated continuation response; for an audit it is only
the canonical audit link. A no-action statement such as `None`, `Nothing to do`,
or `No action required` is valid only when the highest authorized scope is complete
and the response links an audit rather than a continuation.

## Lifecycle enforcement

A tracked session has five permitted stop outcomes: keep working, await a
legitimate decision request, create a valid continuation, complete the
authorized root with an audit, or end the turn on the canonical progress line:

```text
In progress: "<root title>". Last handoff: <path or none yet>. Say "continue" to keep going, or ask for a handoff.
```

Nothing else record-less is accepted and the comparison is exact. A session
that *ends* on that line is reported at `SessionEnd`, an informational hook
that fails open. The line is published by `lifecycle inspect` as
`progress_response`, never in blocking feedback.

Executable work remaining is not a decision request. A decision request names
one bounded question, blocked action, and recognized authority category; it
cannot change the root or scope definition.

A user turn whose **first line** is `Track: <goal>` registers that goal as the
tracked root, when the session is still `OPEN` or `ONE_OFF`. The root's
`scope_id` is the goal slugged with a short digest of the goal appended, so the
same goal always names the same root and a later session declaring it joins
that chain. Its kind is `epic` and its immutable definition is the user's own
words as both title and outcome. The rest of the prompt is the work. The hook
reports `AHK-TRACKED` or `AHK-TRACK-FAILED failed=<check>`.

A session resumes a tracked chain when the **first line** of its prompt is
`Continue from handoff: <absolute path>`; everything after it is the user's
instruction. The evidence is the record the pointer names: its digest must
equal the chain's `current_record_reference.sha256`, and its root and scope
digests must match the chain. A resume reports `AHK-RESUMED` or
`AHK-RESUME-FAILED failed=<check>` — `candidate-outside-handoffs`,
`record-invalid`, `chain-inactive`, `record-digest` or `chain-stale`. Silence is
not a permitted outcome for a prompt that carried a pointer.

`AHK-RESUMED` and `AHK-TRACKED` both carry the bound `inspect` command after
`Command:`, so a newly tracked session has its session key, challenge and
expected revision without spending a tool call on a denial.

A stored record path is a locator, not identity. The same repository is a
different worktree elsewhere and `/mnt/d/...` under WSL, so wherever such a
path is read or compared, the record's basename under this checkout's
`handoffs/` is used and its SHA-256 is the evidence. A digest that does not
match is refused with the same code as before, and the path the retry resolved
to is reported after `candidate=`.

`Stop` verifies the direct candidate, lineage, locked root, open-decision state,
and the renderer's complete response. An attempted assistant message may already
be displayed before `Stop` runs; the hook cannot retract that display, but it
returns corrective feedback and requires a corrected response or a visibly
failed policy outcome.

Blocking feedback names the issue code, the corrective action, and — after
`failed=` — the validator's own codes for the checks that failed. Those codes
are a closed vocabulary of identifiers. The adapter emits no other issue text:
no prompt, reply, or transcript content can reach the host through it.

Informational hooks fail open. Tracked lifecycle hooks fail closed on
`UserPromptSubmit` and `Stop`, including corrupt tracked state, unreadable
candidates, missing owned runtime files, and lifecycle validation exceptions.
A session in `OPEN` or `ONE_OFF` fails open at both events by construction: it
has registered no root and so has no lifecycle state to fail closed on.
Successful lifecycle checks emit no routine model context.

A runtime fault is a malfunction, not a decision about the work.
`AHK-HOOK-RUNTIME` blocks only a session whose own state was read and shows a
declared mode outside `OPEN` and `ONE_OFF`; otherwise it is a bare
`systemMessage` carrying no decision, so the host behaves as it would with no
hook installed. Every report names its failing stage and exception class after
`failed=`. A hook command's exit status never signals a fault.

## Recovering a broken hook runtime

`lifecycle inspect`, `register-root`, `resume` and `join` require a session key,
challenge and expected revision, and a session whose hook flow is failing has no
path to any of them. `lifecycle doctor` is the out-of-band entry point. It takes
no session binding because it decides nothing and changes nothing, and for the
same reason it is the one lifecycle subcommand the control interception does not
intercept. It reports where state resolves, whether it opens, whether its lock is
reachable, which runner is installed and which one is running, and — given a raw
host session ID — that session's enforcement mode. The derived session key and
the local HMAC secret never appear in its output.

## Enforcement modes and the write/stop advisories

A session begins in `OPEN`. Nothing it does is gated: shell commands, file
edits, MCP calls, web fetches, subagents, and todo lists all run untouched. The
one interception that remains is the toolkit's own control commands
(`python <runner> lifecycle …`), which is also how a session learns its session
key, challenge, and expected revision. A tool call that is neither a control
command nor a call to a known writing tool is decided before any state is read.

A session moves to `TRACKED` by registering a root, and to `ONE_OFF` by running
`lifecycle one-off`, which grants no authority and only records that the
session decided its work needs no handoff. Both `OPEN` and `ONE_OFF` stay
ungated for everything but control commands. The legacy state value
`"untracked"` loads as `OPEN`.

Two advisories make undeclared drift visible without gating anything. Neither
blocks, neither carries a `permissionDecision` of any kind, and neither can
error: any failure in either path produces no message at all.

- **`AHK-DECLARE`** fires at most once per session, in `OPEN` only, on the
  first call to a known file-writing tool — `Write`, `Edit`, `MultiEdit` and
  `NotebookEdit` on Claude Code, `apply_patch` on Codex. Not `Bash`, not an MCP
  tool. It is delivered as `hookSpecificOutput.additionalContext`, because it
  is addressed to the model: it names both lifecycle commands in their plain
  form, which the control interception binds. That delivery is observed on
  Claude Code — an acceptance run reports `advisory_seen=pass`, meaning the
  scripted session received the notice and repeated its code. It is unverified
  on Codex. It names no session key,
  challenge or absolute path. It never fires for a session that has already
  declared one-off.
- **`AHK-NO-HANDOFF`** fires at most once per session, at `Stop`, for a session
  still in `OPEN` or `ONE_OFF`, when both hold: `git status --porcelain` is
  non-empty, and its digest differs from the digest recorded at the session's
  first `UserPromptSubmit`. Both facts come from one `git status`. Together they
  mean the repository changed while the session was open and is ending with
  work uncommitted — not that the session made the change; the mechanism cannot
  establish that. It is user-facing text and asks the user to request a
  handoff. A declared one-off still receives it.

**The guarantee this supports.** The toolkit does not guarantee that work
needing a handoff produces one. It guarantees that declared tracked work
follows the lifecycle, and it reports undeclared work that ends unfinished. A
session enforces nothing until it registers a root.

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

## Scaffolding a successor

`render <record.json> --successor-of <predecessor.md>` copies the lineage a
successor never chooses: `schema_version`, `authorization_id`,
`authorized_root_scope_id`, `authorization_evidence`, `transition`, the
`predecessor` reference (its record id, normalized path and SHA-256), and each
scope's `scope_definition` and `scope_definition_digest`. The author supplies
`record_id`, `timestamp`, the per-scope progress fields, `verification`,
`exact_action`, `next_session_prompt` and the sections.

Root immutability is unchanged, because the definitions are copied from the
predecessor and never recomputed from author input. Supplying one of those
fields with a different value is rejected (`successor-inherited`), as is naming
a scope the predecessor does not hold (`successor-scope`); either needs an
approved transition. `validate_successor` still runs at `Stop`.

## Lifecycle commands and untracked authoring

`lifecycle inspect`, `register-root`, `resume`, and `join` act on a host-tracked
session and require the session key, challenge, and expected revision that a
tracked `UserPromptSubmit` supplies. They are the entry point whenever the
session is tracked. Authoring a record outside a tracked session — no session key
or challenge is available — uses `render`, `validate`, and `render-tail` only;
lifecycle credentials are never fabricated to satisfy a command.

## Registering the first root

A session chooses whether to register a root; no tool is denied to force the
decision. `register-root` is a control command, so it needs the session key,
challenge and expected revision bound into it, and the definition it carries
must be canonicalized and encoded in the fixed form the parser accepts. A
pre-root session has no shell with which to do that, so it attempts the command
with the semantic slots as plain text:

```
python <runner> lifecycle register-root --scope-id <id> --scope-kind <kind> \
  --scope-title "<title>" --scope-outcome "<outcome>"
```

The denial validates those slots, encodes the definition, and returns the
complete bound command after `Command:`. Run that verbatim. Nothing about what
may execute changes: the returned command is accepted only because it satisfies
the same fixed-token parser, which the hook verifies before offering it.

The three semantic slots are the author's. Derive them from the initiating user
request; the toolkit supplies the encoding, never the meaning. A definition
whose encoding will not fit the bounded feedback channel is rejected as
`definition-too-long`. A pre-root denial names the check that rejected the
attempt after `failed=`, using the same closed vocabulary as blocking `Stop`
feedback — `definition-b64-alphabet`, `definition-json-noncanonical`,
`scope-kind` and the rest. A cause the hook can repair on its own, such as a
stale challenge, is corrected in the returned command instead of being named.
