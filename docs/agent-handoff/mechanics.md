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

Tracked sessions have five permitted stop outcomes: continue working, await a
legitimate decision request, create a valid continuation, complete the
authorized root with an audit, or end the turn on the canonical progress line.

`Stop` runs at every turn end, not at the end of a session, so requiring a
record at every one of them made "I have opened the pull request, watching CI"
cost a full continuation. The progress line is the alternative:

```text
In progress: "<root title>". Last handoff: <path or none yet>. Say "continue" to keep going, or ask for a handoff.
```

Nothing else record-less is accepted, and the comparison is exact. The
guarantee it leaves is: every turn end is a record, an audit, a decision
response, or that line, and a session that *ends* on that line is reported at
`SessionEnd`. That is weaker on paper than "every turn end is a record", and
the evidence is that the stronger rule was being paid for in tens of thousands
of tokens of record text per task per day.

The line is published by `lifecycle inspect` as `progress_response`, not in
blocking feedback. It contains the declared goal in the user's own words, and
feedback carries issue codes, the corrective action and bounded identifiers
only — never authored text.

`SessionEnd` is an informational hook and fails open. It emits a
`systemMessage` only when the session is `TRACKED` and its last stop was a
progress line, naming the tracked work and how to resume it. Everything else,
including an unreadable state file, produces no message at all. Executable work remaining is not a decision
request. A decision request must name one bounded question, blocked action, and
recognized authority category; it cannot change the root or scope definition.

A user turn whose **first line** is `Track: <goal>` registers that goal as the
tracked root of the session, when the session is still `OPEN` or `ONE_OFF`. The
root's `scope_id` is the goal slugged, with a short digest of the goal appended
so that the same goal always names the same root: a later session declaring it
joins that chain rather than forking a parallel one. Its kind is `epic` and its
immutable definition is the user's own words as both title and outcome. The rest
of the prompt is the work. The hook reports `AHK-TRACKED` on success and
`AHK-TRACK-FAILED failed=<check>` otherwise, and the session stays untracked
when it fails. The toolkit supplies the encoding and never the meaning: here the
user supplied the meaning in their own turn, which is also the strongest
authorization evidence the design has — the root is bound to the turn that asked
for it.

### Record paths are locators, not identity

A chain crosses checkouts: the same repository is one worktree here, another
there, and `/mnt/d/...` under WSL, so an absolute path recorded in one of them
names a file that does not exist in another. Wherever a stored record path is
read or compared, the record's basename under this checkout's `handoffs/` is
used and the record's SHA-256 is the evidence. A record whose digest matches
the one the caller already expects is that record, whichever checkout wrote
the path; one whose digest differs is refused with the same code as before,
and the path the retry resolved to is reported after `candidate=`.

A session resumes a tracked chain when the **first line** of its prompt is
`Continue from handoff: <absolute path>`. Everything after that line is the
user's instruction for the session and is not read for the resume decision.
The evidence is the record the pointer names: its digest must equal the chain's
`current_record_reference.sha256`, and its root and scope digests must match the
chain, exactly as `resume` has always required. The prompt body was never
evidence, and requiring it to match the renderer byte for byte meant one extra
space left the session untracked with nothing reported to anyone.

A resume that succeeds returns `AHK-RESUMED` as model context, naming the
tracked root and the record. `AHK-RESUMED` and `AHK-TRACKED` both carry the
bound `inspect` command after `Command:`, so a newly tracked session has its
session key, challenge and expected revision without spending a tool call on a
denial to learn them. The denial path is unchanged and still issues them. A resume that fails returns `AHK-RESUME-FAILED`
with the failing check after `failed=` — `candidate-outside-handoffs`,
`record-invalid`, `chain-inactive`, `record-digest` or `chain-stale` — and the
session stays untracked. Silence is not a permitted outcome for a prompt that
carried a pointer.

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
That rule applies to a `TRACKED` session; a session in `OPEN` or `ONE_OFF`
mode fails open at both events by construction, because it has registered no
root and so has no lifecycle state to fail closed on. An untracked tool-free
informational response may fail open. Successful lifecycle checks emit no
routine model context.

## Runtime faults are not policy decisions

A malfunction of the toolkit is not a decision about the work. `AHK-HOOK-RUNTIME`
marks a fault the hook could not evaluate past — a malformed payload, state that
could not be read, an unexpected exception — and how it is reported depends
entirely on what the session had declared when it happened.

A fault blocks only when the session's own state was read and shows a declared
mode outside `OPEN` and `ONE_OFF`. That is the fail-closed guarantee above, and
it is unchanged. In every other case — a session that declared nothing, or one
whose mode could not be determined because reading state is what failed — the
fault is reported as a bare top-level `systemMessage` carrying no permission
decision and no stop decision, so the host behaves exactly as it would with no
hook installed. An unknown mode is not a tracked mode: state that could not be
read cannot show that a session declared anything, and a session that declared
nothing is ungated by construction, so there is no policy for the fault to
enforce. Blocking there only removed the session's way out, denying it the
tools its own repair required.

Every `AHK-HOOK-RUNTIME` report, blocking or advisory, names its failing stage
and the exception class after `failed=`, alongside the platform error number
where the exception carries one. Both are bounded identifiers fixed in source —
a stage label and a Python class name — so this detail cannot carry turn content
any more than the validator codes it sits beside.

The hook command's exit status never signals a fault. Exit 2 is the host's block
signal at `PreToolUse`, `UserPromptSubmit` and `Stop`, indistinguishable from a
deliberate denial, so a decision is always rendered on the structured channel
and the status stays silent. The one boundary that still fails closed without
knowing the mode is a runtime that cannot be imported at all: the module that
would report the mode is the one failing, and a missing owned runtime file is
install corruption rather than a transient fault.

## Locating the runner

Each managed hook command finds `.agent-handoff-toolkit/runner.py` by walking
upward from the hook process's working directory, and does nothing at all when
no install is found there. Naming the runner by a path relative to that working
directory does not work: hosts run hook commands in the directory the agent is
working in, so any session below the repository root failed to start Python,
and a missing script file exits 2 — which every gated event reads as a block.

The walk is also the only form that is correct across checkouts. A worktree
carries its own copy of the install, so the walk runs the runner pinned by the
checkout the agent is actually working in. `CLAUDE_PROJECT_DIR` is deliberately
not used: Claude Code documents that it stays at the project root the session
started in even after the agent enters a worktree, so locating the runner
through it would run one checkout's pinned release against another's. Codex
documents no equivalent variable, and the walk needs none on either host.

The runner still takes the repository it operates on from its working
directory, unchanged.

## State across installed releases

Every worktree of a repository shares one lifecycle registry, so state written
by one installed release is read by every other. Loading tolerates that drift
in both directions: a field the writing release added and this one does not
know is dropped, and a field this release knows and the writer omitted takes
its own declared default. Fields carrying no default — identities, revisions
and digests — stay required and are validated as before.

Host payload field names are the exception. Lifecycle state is the toolkit's
record of its own decisions and never carries a prompt, a reply or a
transcript, so state naming one of those fields is refused rather than quietly
trimmed: a content channel through the registry is a defect to report, not
drift to tolerate.

## Recovering a broken hook runtime

`lifecycle inspect`, `register-root`, `resume` and `join` require a session key,
challenge and expected revision, and those are issued only through the
`PreToolUse` denial. A session whose hook flow is failing therefore has no path
to a challenge, no path to registering a root, and no path to any lifecycle
command — the toolkit is unavailable exactly when a session needs it.

`lifecycle doctor` is the out-of-band entry point. It takes no session binding
because it decides nothing and changes nothing, and for the same reason it is
the one lifecycle subcommand the control interception does not intercept. It
reports where state resolves, whether it opens, whether its lock is reachable,
which runner is installed and which one is running, and — given a raw host
session ID — that session's enforcement mode. The derived session key and the
local HMAC secret never appear in its output.

## Enforcement modes and the write/stop advisories

A session begins in `OPEN`. Nothing it does is gated: shell commands, file
edits, MCP calls, web fetches, subagents, and todo lists all run untouched.
The one interception that remains in `OPEN` is the toolkit's own control
commands (`python <runner> lifecycle …`), denied and repaired exactly as
described below under "Registering the first root". A tool call that is
neither a control command nor a call to a known writing tool is decided
before any state is read: there is nothing for the hook to decide about it in
any mode, so it costs no state open, no lock and no subprocess. That interception is not
a gate on work; it is the control plane, and the only channel by which a
session learns its session key, challenge, and expected revision, because
`UserPromptSubmit` returns silently on the normal path.

A session moves to `TRACKED` only by registering a root, and to `ONE_OFF` only
by running `lifecycle one-off`, a subcommand taking the same session binding
as the other lifecycle commands. `one-off` grants no authority — no
authorization ID, no chain — it only records that the session decided its
work needs no handoff. Both `OPEN` and `ONE_OFF` stay ungated for everything
but control commands. The legacy state value `"untracked"` loads as `OPEN`.

Two advisories make undeclared drift visible without gating anything:

- **`AHK-DECLARE`** fires at most once per session, in `OPEN` only, on the
  first call to a known file-writing tool — `Write`, `Edit`, `MultiEdit`, and
  `NotebookEdit` on Claude Code; `apply_patch` on Codex. Not `Bash`, not an
  MCP tool, not any other tool name: classifying shell commands as read-only
  or not is fragile, and treating `git status` as a trigger would defeat the
  purpose. The hook emits `hookSpecificOutput.additionalContext` and no
  `permissionDecision` of any kind, so the host's own permission flow runs
  exactly as it would with no hook installed. It deliberately does not send
  `permissionDecision: "allow"`: on a real host that value does not merely
  decline to block, it also satisfies the permission gate, which would have
  let the first repository write of every untracked session proceed without
  the approval the user would otherwise be asked for. An advisory must not
  grant an approval nobody gave it. It is delivered as model context rather
  than as a `systemMessage` because it is addressed to the model — it hands
  the session two commands to choose between, and a message the model never
  sees cannot be acted on. Whether a given host delivers that field to the
  model is a property of the host, so the acceptance run reports it as
  `advisory_seen`: the scripted session is asked to repeat the notice's code,
  and the observer records only whether it appeared. The message states that
  this is the first repository edit with no registered root and names both
  commands in their plain form — `lifecycle register-root` and
  `lifecycle one-off` — which the control interception binds. It names no
  session key, challenge or absolute path, so its length is fixed: an earlier
  form carried two 64-hex values and the runner path twice, which pushed it
  past the feedback bound in repositories with long paths and silenced it
  entirely. It never fires again for that session, and never fires for a
  session that has already declared one-off.
- **`AHK-NO-HANDOFF`** fires at most once per session, at `Stop` for a session
  still in `OPEN` or `ONE_OFF`, when both hold: `git status --porcelain` is
  non-empty, and its digest differs from the digest recorded at the session's
  first `UserPromptSubmit`. `Stop` runs at every turn end rather than at the
  end of a session, so repeating the note told the user nothing new and cost
  them the same sentence every time the agent stopped talking. The flag is
  committed with the notice, which is also what lets every later `Stop` in
  that session skip the `git status` subprocess entirely. Both facts come from one `git status` read, so the note
  costs one subprocess per `Stop`. The first condition alone would fire on
  work left uncommitted before the session began; the second alone would fire
  on a session that committed away pre-existing changes. Together they mean
  the repository changed while the session was open and is ending with work
  uncommitted, which is what the note says — not that the session made the
  change. The mechanism cannot establish that: a person saving a file in
  their editor mid-turn satisfies both conditions too. A declared one-off
  still receives this note — `one-off` suppresses `AHK-DECLARE`, not
  `AHK-NO-HANDOFF`. The note is user-facing text and reads as such: it asks
  the user to request a handoff, and does not hand the model a command.

Neither advisory blocks, and neither can error: a missing `git` binary, a
directory that is not a repository, a `git` invocation that times out or
returns unparseable output, an unreadable state file, or an unexpected
exception in either advisory path all produce no message and no error.

**The guarantee this supports.** The toolkit does not guarantee that work
needing a handoff produces one. It guarantees that declared tracked work
follows the lifecycle, and it reports undeclared work that ends unfinished. A
session enforces nothing until it registers a root.

## Host input

The hook parses a small, known set of fields: the session, the repository
location, the turn reference, the event flags, and — for a tool call — the
shell command. Each is validated where it is read. Everything else in a hook
payload is the work's own content, and the hook passes it through unexamined.

That includes the whole of a tool call's input. A written file, a pasted log, a
patch: their size, shape and characters belong to the tool call being made, not
to the lifecycle. A tab is a control character; so is a form feed, and an escape
sequence in captured output. Rejecting any of them rejects Go source, Makefiles
and ordinary terminal output, and proves nothing, because no host content ever
reaches hook feedback. Feedback carries issue codes, the corrective action, and
values that match the identifier pattern — never turn content. In a tracked
session the tool hook returns before it reads tool input at all, except to
recognize a control command.

One bound applies to the whole hook input. The structural bounds beneath it —
nesting depth, array length, object size — exist to stop pathological parsing
and are set where that begins, not where ordinary usage lives. A shell command
is separately bounded where it is parsed, and a rejection there names whether
the length, the type or the alphabet was at fault.

The host's report of the turn's text — the final assistant message on `Stop`,
the prompt on `UserPromptSubmit` — is turn content on the same terms. It is
absent or null when a turn ended on a tool call, empty when the turn emitted no
text, and otherwise whatever the model or the user wrote. None of that is a
runtime fault; on `UserPromptSubmit` treating it as one rejects the user's own
message. Line endings are normalized so that a later exact comparison is made
on one representation, and trailing newlines are normalized away with them:
`render-tail` prints the response followed by one, so a host reporting the
message as it was printed differs from the renderer by that byte alone, and
was told the toolkit had malfunctioned over it. The text is otherwise kept
verbatim so it is never trimmed into something that could pass as the rendered
response — leading text and interior whitespace still belong to the message,
and a candidate whose body differs is a policy correction, not a fault. Content that
says nothing reads as absent, which every later check already handles. Only a
structurally invalid value — a wrong type — remains a normalization failure.

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
decision. `register-root` is itself a control command, so it still goes
through the same interception described above: it needs the session key,
challenge, and expected revision bound into it, and the definition it carries
must be canonicalized and encoded in the fixed form the parser accepts. A
plain-text attempt supplies the machine that does both. Attempt `register-root`
with the semantic slots as plain text:

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
