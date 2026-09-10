# Stop-Hook Enforcement and Authorization Lineage Design

## Goal

Make the toolkit enforce continuation and completion-audit behavior when an agent
attempts to finish a turn. A noncompliant stop must return concise, actionable
evidence to the same model session so the model can correct itself before the
conversation settles on a terminal result.
The implementation must prevent an inherited authorized scope from being silently
narrowed, require canonical generated response tails, preserve user-approved scope
changes, and add no model-token/advisory output on successful checks.

## Problem statement

Schema version 1 standardizes records after an agent chooses a record type and
declares its active scopes. Its validator proves structure and internal
consistency, but it cannot establish whether the agent selected the truthful
highest-authorized scope. The installed hooks are advisory and run only at session
start and after selected file-edit tools. They cannot stop an agent from omitting a
record, inventing a narrower root, or ending with handwritten instructions instead
of the canonical rendered tail.

The resulting trust cycle is invalid: the actor that may misunderstand the
authorization boundary is also treated as the authority for the boundary being
validated.

## Design principles

1. **Enforce before accepting a terminal result.** `Stop`, not `SessionEnd`, is the
   lifecycle boundary. Both supported hosts record and may display the attempted
   assistant message before `Stop` runs; the hook cannot retract that transient
   text. It can prevent the turn from remaining terminal, return corrective
   evidence, and require a compliant follow-up. `SessionEnd` remains suitable only
   for cleanup or audit telemetry.
2. **Preserve inherited authority.** A continuation locks its authorized root and
   scope lineage for the resumed session. A model cannot replace that root merely
   by declaring different metadata.
3. **Make authorization contextual.** An immediately following affirmative user
   response can approve the exact scope proposal in the preceding assistant turn.
   Materially ambiguous, major, destructive, or security-sensitive changes require
   a clearer follow-up.
4. **Keep the core portable.** The core consumes normalized lifecycle events and
   transcript evidence. Host adapters own Claude Code and Codex payload/output
   differences. The core does not call host, GitHub, tracker, or model APIs.
5. **Spend tokens only on failure.** Successful lifecycle checks emit no model
   context. Failures report compact issue codes and the minimum corrective action.
6. **Do not overstate verification.** Lineage proves provenance and consistency;
   it does not mechanically prove that arbitrary natural-language scope
   interpretation or completion claims are semantically true.

## Host behavior

Both Claude Code and Codex support synchronous `Stop` hooks that can continue the
same model session with a blocking reason. Each distributed host configuration
will add `UserPromptSubmit`, `PreToolUse`, and `Stop` entries in addition to
existing session and authoring hooks.

The pre-tool gate applies to shell commands, file edits, MCP tools, and other local
function tools that are capable of mutation. A small host-specific allowlist may
exempt canonical tools whose contracts are intrinsically read-only. Shell commands
are never treated as read-only by lexical command parsing: the session must adopt
or register an authorized root before any shell command. Unknown tool contracts
fail closed and require registration. This prevents alternate write mechanisms
from bypassing tracked-mode entry.

The adapters normalize host payloads into core events containing only the fields
the core needs:

- host and event name;
- session and turn identifiers;
- working directory;
- transcript reference;
- `stop_hook_active`;
- latest assistant-message text for `Stop`;
- current user-message reference for `UserPromptSubmit`.

Host-specific output adapters map a core continuation decision to the supported
blocking JSON shape. A successful decision exits silently. A policy violation
returns `decision: "block"` and a concise reason. An enforcement-runtime error in
a tracked session also blocks and identifies the runtime failure; it never silently
allows the stop.

## Schema-v2 record identity and lineage

Schema version 2 adds stable identity and authorization lineage to both record
types. Every record contains:

- `record_id`: a unique stable identifier;
- `authorization_id`: the authorization chain this record belongs to;
- `authorized_root_scope_id`: the locked highest-authorized root;
- `predecessor`: either `null` for an initial authorization or an object containing
  the predecessor record ID, normalized absolute path, and SHA-256 digest;
- `authorization_evidence`: the source user-turn reference and digest, or an
  approved transition reference;
- `transition`: `null` unless this record changes the authorized root; otherwise it
  records the old root, proposed new root, proposal-turn reference, approving
  user-turn reference, and evidence digests.

Each active scope adds an immutable `scope_definition` and
`scope_definition_digest`. The digest covers the scope ID, kind, parent ID, and
normalized definition. Status, remaining-work fields, verification, and
remaining-code explanation remain mutable progress fields. A non-transition
successor must retain every inherited immutable scope field and digest. Changing a
scope's kind, parent, or meaning requires an approved transition even when its ID
is unchanged.

`authorized_root_scope_id` must identify the sole root and the
`highest_authorized` scope. A successor without an approved transition must retain
the predecessor's `authorization_id`, root ID, and complete inherited scope chain.
Inherited unfinished scopes cannot disappear or become descendants of a newly
invented root. A completion audit must complete the locked root, not a local child.

A transition creates a new `authorization_id` while retaining the predecessor
link. This makes authorized root changes explicit and auditable without treating
the new record's self-description as its own authority.

Schema-v1 records remain valid under the v1 validator and remain opaque when
classified as deprecated history. A specifically selected active v1 continuation
may be adopted into v2 through an explicit command that validates the selected
record and establishes the initial v2 authorization chain. The upgrade process
does not scan or rewrite unrelated legacy records.

## Authorization registry and runtime session state

Lifecycle enforcement uses a repository-level authorization registry plus a
small, atomic per-session state object. Neither stores prompt, reply, transcript,
credential, or customer content.

The shared registry contains authorization IDs, immutable root/scope-definition
digests, current record identities and digests, monotonically increasing
revisions, completion state, and active session leases. It may contain multiple
unrelated authorization chains in one repository. A root/scope-definition digest
can have only one current revision. A second session must explicitly join the same
chain and use compare-and-swap against its live revision; it cannot independently
fork or complete stale authority. Conflicting updates are rejected and returned to
the model for live-state reconciliation.

Per-session state is limited to:

- state schema version and hashed repository/session keys;
- enforcement mode: `untracked`, `tracked`, `awaiting-decision`, or `complete`;
- authorization ID, locked root ID, immutable scope digests, and registry revision;
- source and candidate record IDs, paths, and digests;
- pending scope-transition or decision-request references and digests;
- last failure issue signature and consecutive count.

The storage interface is injected into the core. The local adapter defaults to
repository-private state beneath Git metadata when available and a platform cache
keyed by the repository digest otherwise. State and registry publication use
exclusive temporary files, flush/fsync, file locking, revision checks, and atomic
replacement. Corrupt, unreadable, or concurrently changed tracked state blocks the
operation.

Session start does not copy the transcript into state. `UserPromptSubmit` recognizes
only a canonical `Continue from handoff: <absolute-path>` reference or an explicit
`lifecycle resume` operation when associating a new session with an existing chain.
It validates the referenced v2 continuation and live registry revision before
entering tracked mode. It never silently treats a paraphrase as lineage evidence.

For a fresh task, `PreToolUse` blocks every shell command and every non-allowlisted
local tool until the model registers the root. Registration enters tracked mode,
creates or joins the repository registry entry through compare-and-swap, and binds
the chain to the initiating user-turn reference and digest. An untracked session
may use only intrinsic read-only inspection tools or return a tool-free
informational response.

Binding proves which user turn authorized the initial task, not that code perfectly
interpreted arbitrary natural language. Identifiers and explicit scope named in
the initiating turn are checked where deterministic extraction is possible.
Optional semantic evaluation may flag suspected misinterpretation but is not the
sole hard gate.

## Contextual authorization transitions

The model proposes a root-scope transition through an explicit lifecycle command
before asking the user. The proposal records the current root, proposed root,
immutable old/new scope definitions, reason, and assistant-turn reference. It does
not authorize the change.

The immediately following user turn may approve only that exact proposal. A
conservative local classifier accepts common unambiguous affirmatives. A negative,
ambiguous, qualified, non-adjacent, or unrelated response leaves the proposal
pending. Risk is not self-declared and does not lower or raise the scope-transition
proof: every root or immutable-definition change requires the same adjacent
proposal-and-affirmation evidence. Separate repository security and external-effect
policies may require more explicit authorization; when they do, the model must ask
a narrower follow-up before registering the transition.

The host adapter reads the referenced transcript turns transiently, verifies their
roles and adjacency, computes evidence digests, and discards their contents. The
runtime state and record retain only references and digests. A transition cannot
be approved from an assistant turn, from a user turn preceding the proposal, or by
the model invoking a command that asserts approval without matching transcript
evidence.

## Stop-gate state machine

A tracked session may reach one of four permitted stop outcomes:

### Continue working

If executable authorized work remains and there is no registered user gate or
valid continuation, the hook blocks the stop. Its reason tells the model to perform
the current exact action. This prevents a model from turning ordinary unfinished
work into a user choice between continuing and stopping.

### Await a user decision

When progress genuinely requires user input, the model registers a decision
request containing the question, the affected action fields, and why execution
cannot continue safely without the answer. The hook permits only the canonical
generated decision-request response and retains the locked scope. Operational
questions that merely ask whether to continue, stop, or create a handoff are not
valid decision requests.

A decision request has a bounded schema with a stable ID, one question, the blocked
exact-action field and value, and one enumerated authority category: missing
essential input, mutually exclusive user choice, external effect, destructive
operation, or repository-mandated approval. It cannot change the root, scope
definition, or remaining-work state. The renderer generates the entire permitted
assistant response, and `Stop` requires byte-exact equality. Requests outside these
structural categories are rejected; semantic uncertainty remains subject to
optional advisory evaluation and user review.

The next `UserPromptSubmit` event resolves or retains the request. No continuation
may be finalized while a decision capable of changing its first action remains
open.

### Create a continuation

At a legitimate context or operational boundary, the candidate continuation must:

- pass schema-v2 validation;
- be the direct successor of the active record/state;
- retain the locked root or include an approved transition;
- preserve all inherited unfinished scopes;
- contain an executable exact action and no open question gate;
- match the complete byte-exact output of the schema-v2 response renderer in
  `last_assistant_message`.

The hook allows the stop only after every check passes and then atomically advances
the active record pointer.

### Complete the authorized root

A completion audit must pass schema-v2 validation, directly succeed the active
record/state, and complete the locked root. It cannot substitute a completed child
or newly invented standalone scope. The final assistant message must end with the
complete byte-exact rendered audit response and contain no continuation action. The
hook atomically marks the authorization chain complete only after the stop passes.

Untracked informational sessions continue to stop normally. Once a session has
substantively mutated the workspace or adopted/registered authorization state, it
cannot revert itself to untracked mode.

## Canonical response verification

The renderer remains the only source of terminal continuation, audit, and decision
responses. Schema v2 does not permit a handwritten preamble. Stop validation
regenerates the complete expected assistant message from the candidate record or
decision state and requires byte-exact equality after line-ending normalization.
Generated record responses may include a bounded metadata-derived summary, but no
free prose. Validation rejects:

- missing or handwritten tails;
- altered user-action statements;
- a continuation paired with a no-action assertion;
- an audit paired with remaining work or restart instructions;
- content following the final record link;
- a response pointing to a different record than the validated candidate;
- any handwritten or contradictory text before the generated response.

The response comparison uses normalized line endings but otherwise requires exact
content. This turns output standardization into an enforced invariant rather than
an instruction the model may omit.

## Failure feedback and loop control

Every violation has a stable issue code. A blocking reason contains:

- the issue code and short description;
- relevant expected and actual identifiers or digests;
- the minimum corrective action or command;
- the candidate record path when applicable.

It does not reproduce the contract, record, transcript, or previous model output.
Example:

```text
AHK-STOP-ROOT: expected authorized root issue-1323; candidate declared local-unit.
Correct by continuing issue-1323 or rendering a successor continuation for it.
```

The state stores both an issue signature and a correction-cycle counter. The issue
signature is derived from the authorization ID, registry revision, and sorted
violated invariant codes; it exists only to deduplicate feedback. The cycle counter
increments for every blocked stop after the model has been automatically continued,
regardless of cosmetic candidate changes, changed evidence, or a different
issue-code set. It resets only after a compliant stop or a real external user turn,
never through a model lifecycle command. An unchanged signature receives a shorter
reason referencing the same codes. After three blocked stops in one correction
cycle, the adapter activates a circuit breaker: it stops automatic continuation
with the host's supported visible policy-failure shape. This does not retract the
already displayed attempted message; it marks the conversation outcome as
enforcement failure rather than compliant completion and prevents further
automatic token spending.

`stop_hook_active` is treated as loop evidence, not as permission to bypass the
gate. A corrected second pass is validated normally. Consumer acceptance verifies
the configured host block cap exceeds the toolkit's breaker threshold; enforcement
is reported unverified when host policy can terminate the correction loop first.

## Error handling and enforcement policy

The existing blanket rule that all automatic hooks fail open is replaced with a
split policy:

- informational session-start, context-health, and authoring reminders fail open;
- lifecycle enforcement on `UserPromptSubmit` and `Stop` fails closed once the
  session is tracked;
- an untracked, tool-free informational response may fail open;
- corrupt tracked state, missing owned runtime files, validation exceptions, and
  unreadable candidate records block stopping with a runtime issue code.

Hooks remain bounded against oversized payloads, unsafe paths, symlink escapes,
control characters, and untrusted transcript locations. Hook diagnostics never
include raw transcript or customer content.

## Model evaluation and token policy

No model call is required for the enforcement path. Deterministic state, lineage,
validation, rendering, and transcript-reference checks decide whether stopping is
allowed. A successful hook emits nothing into model context.

Optional semantic evaluators are adapter extensions, not core dependencies. They
may assess whether a newly inferred root remains aligned with an initiating goal or
flag whether a response outside the deterministic affirmation grammar may be
linguistically ambiguous. The local deterministic classifier is the sole hard gate
for transition approval. An evaluator can only recommend that the model ask a
clearer question; it can never approve a transition or substitute for missing
proposal-and-affirmation evidence. Evaluators are disabled unless a consumer
explicitly configures a provider and data policy.

Evaluator routing is capability based:

- simple goal-alignment and ambiguity triage use the cheapest configured model
  that passes the repository's evaluation threshold;
- a stronger model is used only for genuinely ambiguous or high-risk semantic
  review;
- evaluator inputs contain only bounded structured scope/proposal fields and the
  minimum excerpt needed for the declared check; full transcripts, repository
  source, credentials, and unrelated user content are forbidden;
- evaluator inputs and outputs are never retained in fixtures or logs;
- cached decisions use a keyed HMAC with a random local secret rather than a plain
  digest, preventing dictionary recovery of low-entropy affirmations.

The core contains no provider or model name. Consumer configuration chooses the
available evaluator, and enforcement continues deterministically when no evaluator
is configured.

## CLI surface

The vendored runner adds lifecycle commands used by models and acceptance tests:

```text
handoff-toolkit lifecycle inspect
handoff-toolkit lifecycle register-root ...
handoff-toolkit lifecycle propose-transition ...
handoff-toolkit lifecycle request-decision ...
handoff-toolkit lifecycle adopt-v1 ...
handoff-toolkit hook --platform claude|codex --event user-prompt-submit
handoff-toolkit hook --platform claude|codex --event pre-tool-use
handoff-toolkit hook --platform claude|codex --event stop
```

Commands validate all identifiers and paths, print bounded diagnostics, and never
accept raw prompt or transcript content as arguments. Commands that mutate runtime
state use compare-and-swap against the expected state digest to prevent concurrent
or stale model actions.

## Installation and trust

The manifest distributes the schema-v2 contract, skill, templates, runtime, and
updated Claude Code and Codex hook fragments. Installation and synchronization
retain existing ownership, dry-run, stale-plan, and rollback protections.

Hard enforcement is not reported as enabled merely because JSON files exist or
commands run directly. Consumer acceptance must prove that each host trusts and
discovers the installed `Stop` hook and that a deliberate synthetic violation:

1. reaches the hook;
2. blocks the attempted stop;
3. returns the expected issue code to the same model session;
4. allows a corrected stop;
5. persists no prompt, reply, or transcript content.

If this end-to-end check is not observed, installation reports enforcement as
unverified rather than claiming the workflow is protected.

## Testing strategy

Implementation follows red-green TDD. Unit and integration coverage includes:

- schema-v2 identity, predecessor, digest, scope-retention, and transition rules;
- rejection of narrowed, invented, missing, reordered, or falsely completed roots;
- v1 validation compatibility and explicit single-record adoption;
- state-machine transitions and atomic compare-and-swap behavior;
- conservative affirmative, negative, ambiguous, qualified, and non-adjacent
  authorization responses;
- permitted decision requests and rejected continue-or-stop evasions;
- pre-tool root-registration enforcement for shell commands, file edits, unknown
  tools, and every non-allowlisted mutation-capable tool;
- exact continuation/audit response comparison;
- exact decision-request response comparison and rejection of all handwritten
  preambles;
- tracked and untracked hook runtime failures;
- host-specific blocking JSON and exit-code behavior;
- failure deduplication, correction reset, `stop_hook_active`, and circuit breaking;
- shared-registry locking, stale compare-and-swap rejection, explicit session joins,
  and concurrent work on unrelated authorization chains;
- bounded payloads, unsafe paths, symlink escapes, corrupt state, and concurrency;
- installer/sync ownership, upgrades, manifest hashes, and Windows commands;
- real Claude Code and Codex stop/correction smoke tests using the cheapest suitable
  configured models.

Committed fixtures and logs contain no prompts, replies, transcripts, secrets,
credentials, or customer data. End-to-end prompts are generated ephemerally from
synthetic scenario data, outputs are reduced to pass/fail issue-code assertions,
and temporary artifacts are removed after verification.

Before reporting completion, run every verification command documented in the
README, inspect warnings, and confirm the worktree and packaged distribution
contain only intended changes.

## Documentation changes

The contract, README, consumer integration guide, skill, templates, and host
configuration documentation will distinguish:

- semantic truth from mechanically verified provenance;
- advisory hooks from fail-closed lifecycle enforcement;
- turn `Stop` from session `SessionEnd`;
- inherited authorization from approved scope transitions;
- successful silent checks from failure-only model feedback;
- deterministic enforcement from optional model evaluation.

Repository instructions for Claude Code and Codex remain semantically identical.
Their unconditional `Automatic hooks fail open` rule is replaced explicitly with
the informational-versus-lifecycle split defined above, in both `AGENTS.md` and
`CLAUDE.md`.

Version documentation will be refreshed only from verified current sources and
will record the date of that check.

## Non-goals

Schema v2 does not:

- claim to prove arbitrary natural-language meaning or whether all real-world work
  is complete;
- call GitHub, tracker, deployment, or model APIs from the core package;
- retain prompts, replies, transcripts, credentials, or customer content;
- rewrite deprecated historical handoffs in bulk;
- allow a successful hook to add routine model context;
- use semantic model evaluation as the only authority for a blocking decision.

The design also cannot retract a noncompliant assistant message already displayed
before a host fires `Stop`. It guarantees corrective continuation and a compliant
or visibly failed terminal outcome. Preventing transient display would require a
different host capability that validates structured output before presentation.

Explicit v1 adoption remains a trust boundary: it locks the selected v1 record's
self-declared root as the first v2 authority. Adoption therefore requires a
specific user-selected path and confirmation, and the toolkit reports that the
earlier scope semantics were not mechanically proven.
