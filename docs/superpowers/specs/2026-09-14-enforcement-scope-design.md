# Enforcement scope: stop gating work that needs no handoff

Status: approved design, not yet planned or implemented.
Target release: v0.4.0 (breaking change to the enforcement contract).

## Problem

The toolkit gates work it has no reason to gate. A session that has not
registered an authorization root can use exactly three tools — `Read`, `Glob`
and `Grep` on Claude Code, `view_image` on Codex. Everything else is classified
`mutation-capable` and denied with `AHK-PRE-ROOT` until the session derives a
scope title and outcome and registers a root.

Measured against a fresh session with no root:

| Action | Today |
| --- | --- |
| Answer a question, end the turn | allowed |
| `Read` / `Grep` / `Glob` | allowed |
| `git status` | denied |
| `gh issue create` | denied |
| Run the test suite | denied |
| Fix a typo | denied |
| Query an MCP server, read-only | denied |
| `WebFetch` / `WebSearch` | denied |
| Launch a subagent | denied |
| Write a todo list | denied |

Pure conversation works, because `Stop` fails open before a root exists. But an
investigation stops the moment it needs `git log`, a web search or an MCP query.
Creating a ticket is `gh`, so it is denied. A todo list, which writes nothing to
the repository, is denied.

The toolkit's goal is that work which needs a handoff produces one. The
mechanism it uses is "no mutation before a root exists". The over-reach is that
it treats "not one of three read-only names" as mutation, so the cost of the
guarantee falls on every session regardless of whether the work needs
continuity at all.

## What this changes, and what it costs

After this change the toolkit no longer guarantees that work needing a handoff
produces one. It guarantees that *declared* tracked work follows the lifecycle,
and it makes undeclared drift visible at the end of a session. That is a
deliberate trade: the guarantee stops being mechanical in exchange for removing
friction from every session that does not need it.

`mechanics.md` must state this in those terms. The repository already declines
to claim that semantic truth is mechanically verified; the same discipline
applies here, and the documentation must not imply a stronger guarantee than
the mechanism supports.

## Design

### Enforcement modes

| Mode | Meaning | Gated |
| --- | --- | --- |
| `OPEN` (new default) | No root, nothing declared | Nothing except control commands |
| `ONE_OFF` (new) | Session declared itself a one-off | Nothing except control commands |
| `TRACKED` | Root registered | Unchanged |
| `AWAITING_DECISION` | Unchanged | Unchanged |
| `COMPLETE` | Unchanged | Unchanged |

`OPEN` replaces the gating role of today's `UNTRACKED`. The `_READ_ONLY`
allowlist stops being a gate: nothing needs classifying as safe enough, because
nothing is denied.

Two consequences follow, both intended. `AHK-PRE-ROOT` ceases to exist as a work
gate, so the pre-root bootstrap deadlock repaired in v0.3.2 becomes structurally
impossible rather than merely fixed. And because no shell command is forced at
session start, a host permission classifier that refuses the long bound
`register-root` command is no longer on the critical path of every new session.

### The control plane is still bound

The one carve-out: the toolkit continues to intercept its own control commands,
matching `python <runner> lifecycle …`, and repairs them exactly as it does
today — denying once and returning the bound form with the session key,
challenge and expected revision filled in.

This is not gating work. It is the control plane, and it is the only way a
session learns its own credentials: `UserPromptSubmit` returns silently on the
normal path, so a `PreToolUse` denial is the sole carrier of the session key
today. Preserving it keeps `lifecycle inspect` working as the discovery path and
keeps `register-root` available to a session that knows up front that its work
spans sessions.

`AHK-CONTROL-BINDING` is unchanged.

### Detector 1: advisory at the first repository write

Fires once per session, in `OPEN` only, on the first call to a known
file-writing tool:

- Claude Code: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`
- Codex: `apply_patch`

Not `Bash`, not MCP tools, not unknown tools. Classifying shell commands as
read-only or not is fragile, and making `git status` a trigger would defeat the
purpose.

The hook returns `permissionDecision: "allow"` together with a `systemMessage`:
the write proceeds untouched. The message states that the session is changing
the repository with no registered root and carries both bound commands ready to
run — `lifecycle one-off` and `lifecycle register-root …`. A session flag makes
it fire once and never again.

A missed trigger costs an advisory, not a guarantee. Detector 2 is the backstop
and does not depend on tool classification at all.

### Detector 2: note at `Stop`

Fires in `OPEN` or `ONE_OFF` when the session ends with unfinished work in the
repository. `systemMessage` only; it never blocks and never errors.

The rule is exact, and both conditions must hold:

1. `git status --porcelain` is non-empty at `Stop`, and
2. its digest differs from the digest recorded at the first `UserPromptSubmit`.

Condition 1 alone would fire on work the user left uncommitted before the
session began. Condition 2 alone would fire on a session that *cleaned* the
tree by committing pre-existing changes, which is the opposite of the case
worth reporting. Together they mean: this session changed the repository and
left the change unfinished.

The signal is deliberately "unfinished at `Stop`" rather than "made changes".
Uncommitted work at the end of a session is exactly when continuity matters. A
session that committed and left the tree clean has landed its work and needs no
handoff. A session that created a ticket or ran an investigation touched
nothing and stays silent.

Per the approved backstop, a declared one-off still receives this note. The
`one-off` declaration suppresses the write-time advisory, not the
end-of-session one.

### `lifecycle one-off`

A new subcommand taking the same session binding as the existing ones, so the
control-plane repair path hands the agent the bound form. It grants no
authority. It records only that the session decided, so that the write-time
advisory is not repeated.

## Error handling

Both detectors are informational, so both fail silent. A missing `git` binary, a
directory that is not a repository, a `git` invocation that times out or returns
unparseable output, an unreadable state file, or an unexpected exception in the
advisory path all produce no message and no error.

`git status --porcelain` runs once per `Stop`, which is a subprocess on every
turn end. It needs an explicit timeout and a fail-silent path around it.

This is a real change to the repository rule that tracked lifecycle hooks fail
closed. That rule continues to apply to `TRACKED` sessions. `OPEN` and `ONE_OFF`
fail open by construction, because there is nothing to enforce. `mechanics.md`
must be amended rather than left to imply the old behaviour.

Hook output must keep exit code 0 on every advisory path: exit code 2 blocks
unconditionally, regardless of the JSON returned.

## Host output contract

The design depends on emitting a message without blocking:

- `Stop`: a top-level `systemMessage`, with no block field, does not prevent the
  stop.
- `PreToolUse`: `permissionDecision: "allow"` together with a message allows the
  call and attaches the message.

**Verified, and revised.** Against Claude Code 2.1.269 both forms surface the
message as an `informational` system event. Under the default permission mode,
however, `permissionDecision: "allow"` also satisfies the permission gate — the
write completed with no prompt and an empty `permission_denials` — while a bare
top-level `systemMessage` with no `hookSpecificOutput` left the gate intact and
the host asked for approval as usual. Attaching a message therefore does not
require a decision field, and `AHK-DECLARE` ships without one: an advisory must
not grant an approval nobody gave it.

The placement of `additionalContext` is not settled. Documentation summarised it
as a top-level field, but the toolkit's own working code nests it under
`hookSpecificOutput` for `UserPromptSubmit`. The implementation must verify
placement per event against a real host rather than trusting either source, and
the `acceptance` smoke harness — which exists to prove a host honours hook
output, and which v0.3.1 repaired — is the right instrument.

## Testing

The suite currently contains many tests asserting pre-root denial. They encode
the behaviour being removed and must be rewritten deliberately, not deleted.

New coverage:

- Every tool runs untouched in `OPEN`, covering the full table in the Problem
  section above.
- The advisory fires exactly once, on the first file-writing tool, and allows
  the call.
- `Bash` never triggers the advisory.
- The `Stop` note fires on a tree the session dirtied; stays silent on a clean
  tree, on a tree already dirty at baseline, and when the work was committed.
- Fail-silent paths: no `git` binary, not a repository, `git` times out, `git`
  returns unparseable output.
- `TRACKED` enforcement is unchanged end to end. This is the regression risk
  that matters most and deserves the heaviest coverage.
- Control-command repair still works in `OPEN`, including the plain-slot
  `register-root` form added in v0.3.2.

## Documentation and skill

`skills/agent-handoff/SKILL.md` describes a world in which the agent must
register a root before it can act. Under this design the agent chooses, so the
skill must state when work is tracked: it spans more than one session, it will
be handed off, or the user expects to resume it later. Without that change
nothing will ever be declared tracked and the design fails in practice.

`mechanics.md` needs its enforcement-boundary and fail-closed sections amended,
and a statement of the weakened guarantee. `distribution/consumer-instructions.md`
needs the same correction. `contract.md` governs what an author writes inside a
record and is unaffected.

## Migration

`EnforcementMode.UNTRACKED` persists as the string `"untracked"` in session
state. Renaming it outright would fail to load existing state, so the loader
must accept the legacy value and map it to `OPEN`.

Consumers re-pin as they did for v0.3.2 and v0.3.3: run `sync --apply` from a
release checkout, then update `INSTALL_STATE_SHA256`, the release string and the
toolkit commit in each repository's context-health test and installation
document. The three consumers are `therapy-link`, `aurolegal.ai` and
`sts-connect`.

## Out of scope

- The host permission classifier that refused the bound `register-root` command.
  This design reduces its impact by removing the forced shell command at session
  start, but does not diagnose or fix it.
- Any change to record structure, validation, rendering, or the
  continuation/completion-audit distinction.
- Any change to `register-root`, `resume`, `join` or `adopt-v1` semantics.
