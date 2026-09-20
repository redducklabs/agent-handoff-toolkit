# Never wedge a session: the prompt path decides nothing and tolerates everything

Status: revised after four adversarial review rounds; awaiting approval.
Target release: v1.1.0 (behavior change; the documented fail-closed guarantee
narrows).

## Problem

A session in a consumer repository had every prompt rejected with:

```text
AHK-STOP-STALE: Reload lifecycle inspect and rebuild against current revisions.
```

The session could not be talked to at all. The cause is not a malfunction and
not a transient race.

Lifecycle state resolves to `<git-common-dir>/agent-handoff-toolkit/lifecycle`
(`lifecycle_storage.py:618`), so every worktree of a clone shares one registry.
When two sessions join one authorization chain and either publishes a record,
that chain's `targeted_revision` advances (`lifecycle.py:1512`,
`lifecycle.py:1589`) while `_allow_stop` updates only the publishing session
(`lifecycle.py:1206-1221`). Every peer bound to the chain is now one revision
behind.

Every fresh external user prompt for a lagging session is then rejected. The
prompt path calls `observe_user_turn` (`hook_adapters.py:968`), which raises
`StaleLifecycleState("session requires chain reconciliation")` when the
session's `chain_revision` differs from the chain's
(`lifecycle_operations.py:319-323`). The outer handler converts that to a
`BLOCK` (`hook_adapters.py:1317-1318`) and the host erases the user's message.
Non-external events and repeated turn references return earlier without a
decision (`hook_adapters.py:922-923`, `:940-941`), so the wedge is scoped to
real prompts — which is to say, to everything the user can do.

The condition is retry-persistent and unrecoverable through any lifecycle
command:

- The block carries no mutation, so the reconciliation that `_publish_decision`
  performs for `Stop` (`hook_adapters.py:1431`) never runs
  (`hook_adapters.py:1422-1451`).
- No turn starts, so no `Stop` ever runs to perform it either.
- The corrective action the message prints is itself unavailable. `inspect`
  runs `consume_control` first (`cli.py:292-294`), whose mutation preserves the
  stale `chain_revision` (`lifecycle_operations.py:300-303`) and is therefore
  rejected by `_apply` (`lifecycle_storage.py:972-979`), exiting
  `AHK-STATE-STALE` (`cli.py:383-385`).

### The same wedge has more than one cause

Chain-revision lag is one. A peer that publishes a *completion audit* leaves
the others `TRACKED` on a `complete` chain — which persisted state permits
(`lifecycle_storage.py:239-255`) — and every mutation they attempt is then
rejected by the lease check (`lifecycle_storage.py:981-984`). `AWAITING_DECISION`
(`lifecycle.py:41`) is chain-bound too and suffers both. A held registry lock
is a third cause, with no rejection at all: both implementations wait forever
(`lifecycle_storage.py:491-500`, `:504`) and the hook simply never returns.

Enumerating these is how the previous drafts of this design failed review three
times: each round found another state the enumeration had missed. The state
space is the problem, not this quarter's members of it.

### Why this is a new category

Two sessions on one chain is a supported configuration — `join` exists for it —
so the first publication by either one must not wedge the others.

This is the fourth instance of one pattern, but it differs in kind from the
three fixed in v0.5.0. Those were malfunctions rendered as policy decisions.
Here the state conditions are real and recoverable, and it is the *response*
that is unrecoverable: blocking `UserPromptSubmit` destroys the only channel
through which recovery could be requested.

## What this changes, and what it costs

`mechanics.md:159` states that tracked lifecycle hooks "fail closed on
`UserPromptSubmit` and `Stop`", treating the two events as equivalent. They are
not:

| Event | What a block stops | Who can act on the feedback |
| --- | --- | --- |
| `PreToolUse` | one tool call | the agent, still running |
| `Stop` | one turn ending | the agent, still running |
| `UserPromptSubmit` | the human's message, erased | no one; no turn starts |

Failing closed at `UserPromptSubmit` prevents nothing. No agent worktree write
and no completion occurs before the turn begins — the toolkit's own mutations
on that event are bookkeeping, not work. The only thing a block there prevents
is the user speaking.

The enforcement cost of removing it is zero: every `LifecycleDecision` that
`evaluate_user_prompt` returns is `ALLOW` (`lifecycle.py:945-946`, `:954-967`,
`:984-986`). The prompt path contains no policy-block path to lose.

## The invariant

> A hook may emit a decision only where the party it blocks is still able to
> act on the feedback.
>
> `UserPromptSubmit` is therefore never a decision point, for any cause, and
> no failure of its bookkeeping may prevent the user's message from being
> delivered.
>
> The guarantee is closed under its own failures: the machinery that reports a
> fault may not itself be able to cause one.

That last clause is not decoration. Three of the four rendering sites exist
because something above them already failed, and the outermost one invoked
`cli_main()` outside its own `try` (`distribution/runner.py:64`), so a fault
in the renderer escaped the only handler positioned to catch it.

A guarantee about emitted output is still not a guarantee about liveness. Both
lock implementations wait without a deadline — Windows polls `LK_NBLCK` in an
unbounded loop (`lifecycle_storage.py:491-500`), POSIX uses blocking `flock`
(`:477`, `:504`) — and that lock is taken during fallback root resolution
(`:639-650`), storage construction (`:678-682`, through `_locked()` at
`:698-705`), reads (`:739-744`) and CAS (`:841-859`). A held lock therefore
hangs a prompt rather than blocking it. Bounding that is deliberately not part
of this design; see Out of scope.

### Which events fail closed, at which boundary

| Boundary | `UserPromptSubmit` | `PreToolUse` | `Stop` |
| --- | --- | --- | --- |
| A. Tracked mode successfully read | advisory | deny | block |
| B. State unreadable after the adapter loaded | advisory | notice, no decision | notice, no decision |
| C. Package or adapter import corruption | advisory (**changed**) | deny | block |

Boundary B is the existing rule, unchanged (`hook_adapters.py:1320-1327`).
Boundary C keeps failing closed for work, because nothing there can read what
the session declared; only its prompt behavior changes.

## The mechanism: a best-effort prompt path

The prompt path becomes a sequence of independent best-effort steps rather than
a transaction that must be correct for every state.

Each step is attempted in order. If a step raises — for any reason: stale
state, a rejected lease, an unreadable registry, a bug — the failure is
recorded as a notice naming the step, and the sequence continues with the next
step. The hook returns context and, where the user should know, a system
message. It never returns a decision, and a failed step does not prevent the
steps that do not depend on it from being attempted.

The steps are the ones that exist today, in their existing order, with
reconciliation added at the front:

1. **Reconcile** the session against the live chain (below).
2. **Clear** a satisfied correction challenge.
3. **Record** the worktree baseline.
4. **Reenter** from a completed chain.
5. **Observe** the external user turn.
6. **Clear** the challenge the observation consumed.
7. **Answer** a pending decision.
8. **Enroll**, for `Track:` and `Continue from handoff:`.

Step 5 is not only a turn reference. `observe_user_turn` also classifies a
transition approval, mints its authorization evidence and can install a
successor chain (`lifecycle_operations.py:658`), so losing it loses this
turn's authorization-bearing bookkeeping, not merely a marker. Step 6 records
the answer to a question the session asked (`hook_adapters.py:1188`). Both
carry real costs when skipped, named below.

This is what makes the invariant robust rather than merely asserted. A state
nobody enumerated — a mode added next year, a lease shape not considered here —
degrades to "that step did not happen, the turn proceeds, the notice says so."
It cannot degrade to silence.

**Independence is bounded by causality.** Step 6 is *not* independent of step
5, and must not be made so. `register_root` binds the new authorization to
whatever external turn reference is already stored
(`lifecycle_operations.py:326`, `:415`), so enrolling after a failed
observation would bind the root to the *previous* turn rather than the
`Track:` turn that asked for it — authorization evidence naming the wrong
turn, which is worse than not enrolling. Enrollment therefore runs only when
the current turn was observed. When it does not run, and the prompt was an
explicit `Track:` or `Continue from handoff:`, the notice carries the same
"this session is untracked. Tell the user." wording the existing enrollment
failures use, so an unmet declaration is loud rather than silent.

The two steps that write independently — the worktree baseline and
reconciliation — are genuinely independent and are wrapped individually.
Notice construction itself is guarded: a failure while naming a failed step
falls back to a fixed bounded token rather than ending the sequence.

### Reconciliation is now an optimization

Because a failed step is survivable, reconciliation no longer has to be
complete or atomic. It attempts, in one mutation:

- for any chain-bound mode whose `chain_revision` lags an `active` chain,
  a refresh to the live revision, preserving the mode — including
  `AWAITING_DECISION` and its pending decision;
- for any chain-bound mode attached to a `complete` chain, release of the
  lease by setting the live revision and `mode=COMPLETE`, preserving the
  previous external turn reference so that step 4's reentry predicate can see
  a different one (`lifecycle_storage.py:879-896`).

It emits `AHK-CHAIN-ADVANCED` or `AHK-CHAIN-COMPLETED` on success, and a notice
naming the step on failure. It runs first because the other mutations preserve
the stale value and would be rejected by `_apply` (`lifecycle_storage.py:972-979`)
— not because `_apply` rejects everything from a lagging session; it does not,
which is exactly why a mutation carrying the live value is accepted.

A direct `TRACKED`-to-`OPEN` mutation stays impossible
(`lifecycle_storage.py:897-898`), so release and reentry remain two mutations
in two steps. They are no longer required to be atomic with each other: if
release succeeds and reentry fails, the session is `COMPLETE` on a complete
chain, which is valid persisted state (`:239-255`), and the next turn reenters.

### A failed correction reset costs one visible turn, not a session

If step 2 or step 5 fails, the correction circuit may stay armed. That does not
wedge anything: at three cycles the `Stop` gate returns `POLICY_FAILURE`
(`lifecycle.py:1049-1061`), rendered as `{"continue": false, "stopReason": …}`,
which ends the turn visibly with a stated reason rather than looping. The
user's next prompt — always deliverable under this invariant — retries the
reset. This is one of the places where best-effort has a user-visible cost; the
others are listed below.

## Enforcement: one chokepoint, plus one deliberate duplicate

Four independent implementations of the block shape exist today. Three are
inside the package and are unified into one rendering module. The fourth cannot
be: `distribution/runner.py` runs precisely when no package module can be
imported (`distribution/runner.py:10-43`, invoked at `:54-60`).

| Site | Today | After |
| --- | --- | --- |
| `hook_adapters.py:341-343` (`POLICY_FAILURE`), `:353` (`BLOCK`) | renders both shapes inline | delegates to the module; both downgraded on `UserPromptSubmit` |
| `hooks.py:319-329` adapter-load corruption | hand-rolled block | calls the module, importable when `hook_adapters` is not |
| `cli.py:429-439` last-resort dispatch | hand-rolled block | calls the module |
| `distribution/runner.py:10-43` bootstrap | block for `user-prompt-submit` and `stop` | **deliberate duplicate**: advisory for the prompt; `Stop` block and `PreToolUse` deny unchanged |

The module is dependency-light — importing nothing from `hook_adapters`,
`lifecycle`, `lifecycle_operations` or `lifecycle_storage`, which reduces its
failure surface without eliminating it. It takes a frozen input value of its
own (the event, decision kind, ordered issue codes with corrective actions and
bounded evidence, detail codes, repeated-issue flag) converted at the boundary
from `LifecycleDecision`, because those types live in a module it must not
import. It returns `str`, matching `HookExecution.stdout` and the existing
`sys.stdout.write` plumbing (`hooks.py:50-54`, `cli.py:414`). The exact field
types and canonical JSON bytes belong to the implementation plan, not here.

The same module holds the strict parser for the unbound control commands, so
`_pre_tool` and the outer boundary share one matcher rather than duplicating
it. It accepts an exact `doctor` invocation and its supported arguments only.

### The held-lock wedge is real, and is not fixed here

Two defects keep the documented recovery command unusable while the registry
lock is held:

- Recognition is too late inside `_pre_tool`: storage is constructed and the
  snapshot loaded before dispatch (`hook_adapters.py:1292-1299`), so the hook
  hangs before the `doctor` exemption is reached.
- `doctor` itself would then hang: `_doctor_report` constructs
  `LocalLifecycleStorage` (`cli.py:217-220`), whose construction acquires the
  registry lock (`lifecycle_storage.py:678-682` via `_locked()` at `:698-705`)
  before reaching the non-blocking `probe_lock` it was designed to use
  (`cli.py:226`, `lifecycle_storage.py:507-540`).

Fixing them means bounding lock acquisition, and a deadline that merely gets
passed around is not a design: an expiry detected after `os.replace` but
before the call returns leaves the caller reporting failure for a mutation
that was durably published (`lifecycle_storage.py:990`). Doing it correctly
requires non-blocking polling on both platforms against one absolute deadline,
an explicit "outcome unknown" result, and a bounded reload before any later
mutation — a change to locking semantics that v0.5.0 deliberately settled the
other way, and that deserves its own review rather than a ride along with
this one. It is out of scope here, with the residual stated below.

## `Stop` is unchanged, and lineage is unaffected

A chain that advances mid-turn still blocks the stop, and `_publish_decision`
still refreshes the revision as failure bookkeeping (`hook_adapters.py:1431`).

`session.chain_revision` is not the evidence that a candidate was built from the
current predecessor: `_candidate` stamps its expected revision from the
`Stop`-time snapshot (`hook_adapters.py:551-565`). The authoritative check is
predecessor identity — record ID, basename and digest against the chain's
current record (`lifecycle.py:1392-1407`) — followed by authorization and scope
lineage (`:1408-1465`). Both are untouched, so a successor drafted against a
superseded predecessor is still rejected after its session reconciles.

## `inspect` under lag and under completion

`consume_control` (`lifecycle_operations.py:300-303`) carries the live
`chain_revision`, and additionally sets `mode=COMPLETE` when the chain is
complete, because `_apply` requires both (`lifecycle_storage.py:972-984`).

Succeeding is not sufficient: `inspect()` emits `progress_response(chain)` for
every attached chain regardless of status (`lifecycle_operations.py:372`), and
that renderer always reports work in progress (`lifecycle.py:1184`). For a
completed chain it must report completion instead, or the command that exists
to tell a session where it stands would tell it something false.

## Output shape

A downgraded `UserPromptSubmit` emits one JSON object using both channels, each
diagnostic on exactly one of them:

- `hookSpecificOutput.additionalContext` carries agent-addressed text — issue
  codes, corrective actions, `failed=` details, and the step names of any
  best-effort failures — in the order produced, deduplicated within the
  channel. Existing prompt context already goes here
  (`hook_adapters.py:1148-1161`).
- `systemMessage` carries one user-addressed sentence naming what failed and
  that the turn was allowed. Runtime advisories already go here (`:388-403`).

A failure that prevents enrollment carries the same "this session is untracked.
Tell the user." wording the existing enrollment failures use
(`hook_adapters.py:1006-1008`, `:1024-1029`). That fail-open-with-notice
behavior is the codebase's existing deliberate choice, not something this
invariant introduces, so no durable quarantine state is added. What must not
happen is a failure leaving the session untracked silently.

## What best-effort costs

Named plainly, because these are real:

- An unrecorded turn reference leaves the derived control capability bound to
  an older turn, which remains self-consistent
  (`lifecycle_storage.py:763-793`). A session whose reference was *never*
  recorded has its control commands denied at `PreToolUse`
  (`hook_adapters.py:770-775`) until a prompt records one; the agent stays
  alive and the next prompt retries.
- **A missed first baseline can be permanent, not one turn.** The baseline is
  captured only while `worktree_baseline is None`
  (`hook_adapters.py:924-939`), so if the first attempt fails and the next
  prompt succeeds, the changes made in between become the baseline and the
  `Stop` advisory that compares against it (`:1370`) may never fire for them.
  This is the sharpest cost of the approach and the one most worth watching.
- **One visibly failed turn per prompt on which the reset fails**, not one in
  total. Once the correction count reaches three, every `Stop` takes the
  circuit path (`hook_adapters.py:1308-1310`, `lifecycle.py:1049-1061`) until
  a prompt resets it; a reset that fails on every prompt fails the turn every
  time.
- Release succeeding while reentry fails leaves the session `COMPLETE` for
  that turn, which suppresses three things: the `OPEN` first-write advisory,
  enrollment for a `Track:` or resume prompt arriving on it, and - because
  `Stop` allows a `COMPLETE` session unconditionally and `AHK-NO-HANDOFF`
  runs only for `OPEN` and `ONE_OFF` - the terminal unfinished-work
  advisory. Work started on that turn can therefore end without the report
  the mechanics otherwise guarantee. The turn says the session is untracked
  when a declaration was made on it, and the next turn reenters and restores
  all three; the window is one turn and it does not repeat.
- **A lost observation can lose a user's approval, not just a marker.**
  `observe_user_turn` is where a transition approval is classified and its
  evidence minted (`lifecycle_operations.py:658`), so a failure there can drop
  an approval the user actually gave. Waiting a turn does not recover it: the
  proposal has to be reissued, because approval is adjacent to the proposal
  that earned it. A lost decision answer is milder - the session asks again
  rather than proceeding as though answered.

Each is a degraded turn or a degraded advisory. None removes the user's
ability to reach the session, which is the property being bought.

## Tests

Written RED first, per the repository rule.

1. **Step-failure sweep.** For each step, force it to raise and assert: the
   prompt is delivered, no `decision` key is present, exit is 0, and the notice
   names the failing step. For the independent steps, assert the later steps
   still ran; for enrollment, assert the opposite — that a failed observation
   suppresses it and the notice says the session is untracked. This is the test
   that encodes the invariant, and it covers states the design did not
   enumerate.
2. **Peer lag**, active chain: the prompt carries `AHK-CHAIN-ADVANCED` and the
   session is reconciled — for `TRACKED` and for `AWAITING_DECISION`, asserting
   the pending decision survives.
3. **Peer completion**: release to `COMPLETE`, reentry on the same turn, and
   the release-succeeds-reentry-fails case leaving valid persisted state that
   the next turn resolves.
4. **The lineage regression.** A drafts S0 from P0; B publishes P1; A
   reconciles on a prompt; A's `Stop` with S0 is rejected with
   `AHK-STOP-PREDECESSOR` and P1 is unchanged. This detects the failure mode
   this change most plausibly introduces.
5. **Boundary matrix**: readable tracked fault, unreadable state, and import
   corruption, at all three events, asserting the boundary table.
6. **Import-failure recovery**, as processes: with the package unimportable
   the bootstrap runner delivers the prompt with the failed stage named; with
   only `hook_adapters` unimportable, `hooks.py` does the same; and with the
   package importable but its renderer raising, the runner's own outer
   boundary still delivers the prompt and exits 0.
8. **`inspect`** under lag and under completion, asserting the completed chain
   does not report work in progress.
9. **Enforcement is not weakened**: `Stop` still blocks a mid-turn chain
   advance, an unregistered root, and a mismatched predecessor digest; an armed
   circuit still terminates with `POLICY_FAILURE`.
10. **Chokepoint guard, source level**: the top-level block shape appears in
    the shared helper and in `distribution/runner.py`, nowhere else. Tests 5
    and 6 exercise the boundaries as processes, because a grep cannot.

## Documentation

- `mechanics.md:159` gains the boundary table, the best-effort prompt-path
  rule, and why the event differs in kind. `contract.md` is untouched.
- `CLAUDE.md` and `AGENTS.md` carry the same rule and stay semantically
  identical.
- `AHK-CHAIN-ADVANCED` and `AHK-CHAIN-COMPLETED` are prompt context notices,
  not decision issues, so they do not join `_ACTIONS`; that table carries
  corrective actions for codes a decision can name.
- `AHK-STOP-STALE`'s corrective action is reworded; it prescribes a command
  that only works after the `consume_control` change.
- The held-lock residual is stated where a consumer will look for it: the
  recovery section of `docs/consumer-integration.md`.

## Rollout

Hook command strings do not change, so managed-fragment digests are
unaffected; `distribution/runner.py` and the package content do change, so
consumers receive the fix only by re-pinning. Per the 2026-09-16
hook-reliability handoff that is separate work in three consumer repositories,
to be confirmed rather than assumed.

## Out of scope

- **Chain-sharing redesign.** Leases or per-session chain views would remove
  the lag; tolerating it is enough.
- **Making `doctor` runnable under partial module corruption.**
  `_lifecycle_main` imports `lifecycle_operations`, `lifecycle_storage`,
  `lineage` and `records` before dispatching (`cli.py:245-268`), so corruption
  of any of them defeats the diagnosis this design permits. Its own spec,
  tracked against issue #22's third point. **Residual: under import corruption
  the user can always reach the session and the agent can report what failed,
  but may be unable to act or to produce a full diagnosis.**
- **Bounding lock acquisition, and with it `doctor` under a held lock.** This
  needs non-blocking polling on both platforms against one absolute deadline,
  an explicit "outcome unknown" CAS result and a bounded reload before later
  mutations, as argued above. Its own spec. **Residual: a registry lock held
  by a live process still hangs a prompt rather than delivering it. The lock
  is released when its holder exits, and every hold is a short read or CAS, so
  this is a liveness risk under pathological contention rather than the
  by-construction wedge this design removes — but it is a way the toolkit can
  still stop a session, and it is not fixed here.**
- **Exact renderer field types and canonical JSON bytes.**
  Implementation-plan detail, deliberately not fixed here.

## Risks

- **Best-effort hides failures that used to be loud.** A step that fails every
  turn produces a notice rather than a stoppage, and the session keeps working
  with degraded bookkeeping. Test 1 asserts the notice; nothing asserts anyone
  reads it.
- **Codex is unverified.** Per the 2026-09-16 handoff, the Codex adapter's
  advisories have never been exercised against a real host.
- **The acceptance harness does not complete a tracked `Stop` end to end**, so
  the matrix test is evidence about rendered output, not host behavior.
- **The bootstrap duplicate can drift.** Test 7 pins its shape, not its text.

## Review

Adversarially reviewed with Codex CLI 0.154.0, read-only sandbox, four rounds,
against commit `705c8c7`.

Round 1 rejected the first draft: 20 findings, four design-changing, seven
factual corrections including an inverted rationale for the `_apply` ordering
requirement. One finding was rejected on the evidence — a claimed new
enrollment hole is existing deliberate behavior at `hook_adapters.py:1006-1008`
and `:1024-1029` — and round 2 confirmed that rejection.

Round 2 rejected the revision: six blockers, two newly discovered — a fourth
block renderer at `distribution/runner.py`, and the recovery command wedging
behind the lock it diagnoses.

Round 4 raised three blockers against the restructure. Two are fixed above:
enrollment is now causally dependent on observation, because `register_root`
binds authorization to the stored turn reference and enrolling after a failed
observation would name the wrong turn; and the bootstrap runner gained an
outer boundary around `cli_main()`, without which a fault in the renderer
escaped the only handler positioned to catch it. The third, bounded lock
acquisition, is deferred to its own spec with the residual stated under Out of
scope. Round 4 also corrected the cost section, which understated a missed
first baseline and the per-prompt cost of a failing correction reset.

Round 3 closed one blocker and rejected the rest, finding that the enumerated
state machine omitted `AWAITING_DECISION`, that its three-mutation sequence
contradicted current behavior, and that `doctor` remained unrunnable behind a
held lock. Rather than enumerate further, the design was restructured: the
prompt path is now best-effort by construction, which makes completeness of the
enumeration an optimization rather than a correctness requirement. The specific
defects round 3 found are fixed above; the exactness it asked for in renderer
types, JSON bytes and deadline budget is deferred to the implementation plan by
choice.
