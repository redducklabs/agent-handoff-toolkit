# Agent handoff mechanics

This document is normative for the formats and enforcement the toolkit applies.
It is the reference for tooling, tests, and anyone diagnosing a blocked session.
An author writing a record needs `contract.md`; the validator enforces
everything below, so an author does not reproduce it from memory.

It states what the toolkit does; the reasoning lives in the toolkit
repository's `docs/design/mechanics-rationale.md`, which is not distributed.

## Metadata block forms

A schema-v2 record begins with a visible fenced block opened by
```` ```json agent-handoff-metadata ```` and closed by a bare ```` ``` ````
line. A completion audit places its sentinel and a blank line before that block.
A rendered metadata
object never contains a bare fence line, because JSON escapes every newline.

Records that predate the current schema are historical: never opened,
validated or migrated.

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

`transition` is `null` in every record of an ordinary chain. A transition
exists only to re-root a live chain under explicit, adjacent user approval,
established as `docs/design/mechanics-rationale.md` states. A changed goal is
plainer to declare anew. Peers on the superseded chain move to its successor
keeping their own mode and pending decision.

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
counted there; the contract forbids hiding one.

A decision request's response keeps its four-line structure, which the HMAC
verification depends on, and reads `Paused: I need one decision from you.`
and `This blocks: <blocked action field>`.

The complete generated tail, including its fence and link, is limited to 300 words and 2,400 characters. It must not reproduce the handoff document. Completion responses label their link **Audit record (not a handoff)** and do not generate a restart prompt. The tail joins `exact_action` list items with `; `.

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
authorized root with an audit, or end the turn on a line that `lifecycle
inspect` publishes as `progress_responses`, keyed by reason:

```text
background-work: Waiting on background work for "<root title>". This session resumes when it reports. Last handoff: <path or none yet>.
ci: Waiting on CI for "<root title>". This session resumes when it reports. Last handoff: <...>.
other-session: Waiting on another session for "<root title>". This session resumes when it replies. Last handoff: <...>.
reply: Replied to your message about "<root title>". Last handoff: <...>. Say "continue" to keep going, or ask for a handoff.
```

Nothing else record-less is accepted and the comparison is exact. A wait line
ends any turn; `reply` ends only a turn the user started, else `AHK-STOP-WORK`
names `failed=reply-host-turn`. `UserPromptSubmit`
records the turn's origin: a prompt is host-started when, stripped, it begins
with `<task-notification>`, `Another Claude session sent a message:`, or a
`<cross-session-message` or `<agent-message` element. State recorded before
origins existed reads as unknown and accepts `reply`. A host-started prompt is
not a user turn: it never answers a pending decision, breaks a proposal's
adjacency, resets the correction circuit, or reenters a completed session. `AHK-USER-CLARIFY` names the
replies that resolve a decision. A session that *ends* on any of these lines is
reported at `SessionEnd`, an informational hook that fails open. The lines are
never in blocking feedback.

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
instruction. The pointer names a record in this checkout's `handoffs/`, or
directly in the `handoffs/` of another worktree sharing this repository's git
common directory, where it is read under the same guards. The evidence is the
record: its digest must equal the chain's `current_record_reference.sha256`,
and its root and scope digests must match the chain. A resume reports
`AHK-RESUMED` or `AHK-RESUME-FAILED failed=<check>` — `record-invalid`,
`chain-inactive`, `record-digest`, `chain-stale`, or
`candidate-outside-handoffs`, which tells the user to open the session in the
checkout holding the record. Silence is not a permitted outcome for a prompt
that carried a pointer.

`AHK-RESUMED` and `AHK-TRACKED` both carry the bound `inspect` command after
`Command:`, so a newly tracked session needs no denial to learn its
credentials.

A stored record path is a locator, not identity. Where one is read, the
record's basename under this checkout's `handoffs/` is used, or the path itself
when it is in another worktree of this repository; its SHA-256 is the evidence.
A mismatched digest is refused with the same code, naming the resolved path
after `candidate=`.

`Stop` first reconciles a session whose chain a peer advanced, so the accepted
lines name the chain's current record. A block on such a session is an
uncounted `AHK-STOP-STALE` naming that record's basename after `current=`.

`Stop` verifies the direct candidate, lineage, locked root, open-decision state,
and the renderer's complete response. A final message without a renderer-owned
link offers no candidate, whatever other links or pointer text it carries. A
renderer link that does not qualify is a counted policy block,
`AHK-STOP-POINTER`, with `failed=` `pointer-ambiguous`,
`pointer-block-mismatch`, `pointer-audit-restart`, `pointer-noncanonical`,
`pointer-missing` or `pointer-outside-handoffs`; a candidate must be in this
checkout's own `handoffs/`, which the last names after `root=`. An attempted
assistant message may already be displayed before `Stop` runs; the hook
returns corrective feedback and requires a corrected response or a visibly
failed policy outcome.

Blocking feedback names the issue code, the corrective action, and — after
`failed=` — the validator's own codes for the checks that failed. Those codes
are a closed vocabulary of identifiers. The adapter emits no other issue text:
no prompt, reply, or transcript content can reach the host through it.

Informational hooks fail open. Tracked lifecycle hooks fail closed on
`PreToolUse` and `Stop`, including corrupt tracked state, unreadable
candidates, missing owned runtime files, and lifecycle validation exceptions.
A session in `OPEN` or `ONE_OFF` fails open by construction: it has registered
no root and so has no lifecycle state to fail closed on. Successful lifecycle
checks emit no routine model context.

`UserPromptSubmit` is never a decision point, for any cause: a blocked prompt
erases the user's message and starts no turn. It reports instead, naming the
issue on `additionalContext` for the agent and one sentence on `systemMessage`
for the user.

Its bookkeeping is best effort: each step is attempted, a failed step is
named, and the turn proceeds. Enrollment is the exception: a `Track:` or
`Continue from handoff:` declaration is registered only when the turn that
carried it was observed, because authorization binds to the stored turn
reference. A declaration that could not be enrolled says the session is
untracked.

A runtime fault is a malfunction, not a decision about the work. Once the
adapter has loaded, `AHK-HOOK-RUNTIME` blocks only a session whose own state
was read and shows a declared mode outside `OPEN` and `ONE_OFF`, and never at
`UserPromptSubmit`; otherwise it is a bare `systemMessage` carrying no
decision, so the host behaves as it would with no hook installed.

Install corruption is the exception: when the package or adapter cannot be
imported, or dispatch fails outright, nothing can read the declared mode, so
`PreToolUse` is denied and `Stop` blocked regardless of mode. `UserPromptSubmit` is delivered even
there.

Every report names its failing stage and exception class after `failed=`; the
bootstrap boundary distinguishes an import that produced no runtime from a
dispatch that failed after one loaded. A hook command's exit status never
signals a fault. The state lock is awaited for at most 15 seconds, then fails
as `LockTimeout` before anything is read or written.

Hook input is read from stdin as UTF-8 bytes, whatever the console code page;
input that is not UTF-8 is `AHK-HOOK-RUNTIME` at stage `decode-input`.

A hook's repository root is the checkout holding the payload `cwd` (its
nearest ancestor with `.git`) when that is a worktree of the same repository,
sharing its git common directory; otherwise it is the hook process's
directory. The located runner hands a hook, once, to that worktree's
`.agent-handoff-toolkit/runner.py`, and serves the hook itself if that runner
gives no single response.

## Recovering a broken hook runtime

Every lifecycle command but `doctor` needs session credentials, and a session
whose hook flow is failing has no path to them. `lifecycle doctor` is the
out-of-band entry point: it takes no session binding and decides and changes
nothing. It reports where state resolves, whether it opens, whether its lock is
reachable, which runner is installed and which one is running, and — given a raw host session ID —
that session's enforcement mode. The derived session key and
the local HMAC secret never appear in its output.

## Enforcement modes and the write/stop advisories

A session begins in `OPEN`. Nothing it does is gated. The
one interception that remains is the toolkit's own control commands
(`python <runner> lifecycle …`), which is also how a session learns its session
key, challenge, and expected revision. A tool call that is neither a control
command nor a call to a known writing tool is decided before any state is
read, except as `AHK-CONTEXT-HIGH` states.

A session moves to `TRACKED` by registering a root, and to `ONE_OFF` by running
`lifecycle one-off`, which grants no authority and only records that the
session decided its work needs no handoff. Both `OPEN` and `ONE_OFF` stay
ungated for everything but control commands. The legacy state value
`"untracked"` loads as `OPEN`.

Three advisories gate nothing. None blocks, carries a `permissionDecision`, or
can error: any failure produces no message.

- **`AHK-DECLARE`** fires at most once per session, in `OPEN` only, on the
  first call to a known file-writing tool — `Write`, `Edit`, `MultiEdit` and
  `NotebookEdit` on Claude Code, `apply_patch` on Codex. Not `Bash`, not an MCP
  tool. It is sent on `hookSpecificOutput.additionalContext`, naming both
  lifecycle commands in plain form, which the control interception binds. Its
  delivery is verified on
  Claude Code by an acceptance run (`advisory_seen=pass`) and unverified on
  Codex. It names no session key,
  challenge or absolute path. It never fires for a session that has already
  declared one-off.
- **`AHK-NO-HANDOFF`** fires at most once per session, at `Stop`, for a session
  still in `OPEN` or `ONE_OFF`, when both hold: `git status --porcelain` is
  non-empty, and its digest differs from the digest recorded at the session's
  first `UserPromptSubmit`. Both come from one `git status`, and mean the
  repository changed while the session was open, not that the session changed
  it. It asks the user, on `systemMessage`, to request a handoff. A declared
  one-off still receives it.
- **`AHK-CONTEXT-HIGH`** serves a `TRACKED` or `AWAITING_DECISION` session on
  Claude Code. `PreToolUse` reads numbers only from the last 512 KiB of
  `transcript_path`: the newest main-thread usage (`input_tokens`
  plus both cache counts), against `AHK_CONTEXT_WINDOW_TOKENS`, else 1,000,000
  when the model id carries `[1m]` or the tail shows a count above 200,000,
  else 200,000. At 80% it asks once, on `additionalContext`, for the
  continuation handoff, and re-arms after a reading below 50% or a compaction.
  An ordinary call opens state for it only while one of the last five readings
  is below 80%. It is never sent at `Stop`, which has no non-blocking model
  channel.

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

## Authoring input for `render`

`render` reads one JSON object and writes the record document. That object is the
record's metadata fields at the top level plus a sibling `sections` map from
section heading to body text. `sections` is not part of the emitted metadata
block, and the renderer emits the sections in canonical order regardless of the
order supplied. `--output` refuses, as `AHK-INPUT`, to overwrite a file whose
digest is any chain's current record; without readable state it is unguarded.

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

The definitions are copied, never recomputed from author input. Supplying one of those
fields with a different value is rejected (`successor-inherited`), as is naming
a scope the predecessor does not hold (`successor-scope`); either needs an
approved transition. `validate_successor` still runs at `Stop`.

## Lifecycle commands and untracked authoring

Control commands need the credentials the interception binds into them. It
finds `python <runner> lifecycle` anywhere in a shell command outside quotes;
only `doctor` and `--help`/`-h` pass. A compound command is denied with the
bound command, to run alone. On Claude Code, the session's own `inspect`, wrong
only in its challenge or revision, is replaced by the bound form through
`updatedInput`, permission unchanged; Codex is denied with it. A CLI
`AHK-INPUT` or `AHK-STATE-STALE` says to run the command alone and lists the
scope kinds.

Authoring a record outside a tracked session uses `render`, `validate`, and
`render-tail` only; lifecycle credentials are never fabricated.

## Registering the first root

A session chooses whether to register a root; no tool is denied to force the
decision. `register-root` is a control command, so it needs the session key,
challenge and expected revision bound into it, and the definition it carries
must be canonicalized and encoded in the fixed form the parser accepts. A
pre-root session attempts the command with the semantic slots as plain text:

```
python <runner> lifecycle register-root --scope-id <id> --scope-kind <kind> \
  --scope-title "<title>" --scope-outcome "<outcome>"
```

The denial validates those slots, encodes the definition, and returns the
complete bound command after `Command:`, verified against the fixed-token
parser; run it verbatim. A plain `lifecycle one-off` is returned bound.

The three semantic slots are the author's. Derive them from the initiating user
request; the toolkit supplies the encoding, never the meaning. A definition
whose encoding will not fit the bounded feedback channel is rejected as
`definition-too-long`. A pre-root denial names the check that rejected the
attempt after `failed=`, using the same closed vocabulary as blocking `Stop`
feedback — `definition-b64-alphabet`, `definition-json-noncanonical`,
`scope-kind` and the rest; a `scope-kind` denial lists the valid kinds. A cause
the hook can repair on its own, such as a stale challenge, is corrected in the
returned command instead of being named.
