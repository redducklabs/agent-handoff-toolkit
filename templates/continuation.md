<!-- agent-handoff-metadata
{
  "schema_version": 1,
  "record_type": "continuation",
  "timestamp": "<replace with ISO-8601 timestamp including timezone>",
  "active_scopes": [
    {
      "scope_id": "<highest-authorized-scope-id>",
      "scope_kind": "standalone",
      "parent_scope_id": null,
      "highest_authorized": true,
      "remaining_work": true,
      "remaining_code": true,
      "remaining_code_detail": "<explain why code does or does not remain>",
      "status": "in-progress"
    }
  ],
  "next_session_gates": [],
  "verification": [
    {
      "check": "<command or check>",
      "result": "not-run",
      "reason": "<why this check has not run>"
    }
  ],
  "exact_action": {
    "action": "<immediately executable action>",
    "target": "<specific target>",
    "constraints": "<constraints that govern the action>",
    "completion_condition": "<observable condition that completes the action>"
  },
  "next_session_prompt": "<self-contained prompt that includes preflight reconciliation and the exact action>"
}
-->

# Session continuation

## Objective

<State the highest authorized outcome and the unfinished result.>

## Authoritative references

<List repository instructions, governing designs, plans, ADRs, issues, and rollout references.>

## User decisions

<Record settled decisions that constrain the next session.>

## Repository state

<Record a timestamped snapshot of branch, HEAD, index, untracked files, remote, tracker, and rollout state.>

## Completed work

<Describe completed work precisely enough that it will not be repeated.>

## Verification evidence

```json
[
  {
    "check": "<command or check>",
    "reason": "<why this check has not run>",
    "result": "not-run"
  }
]
```

## Incomplete work and risks

<List concrete remaining work, blockers, conflicts, and known risks.>

## Exact next action

```json
{
  "action": "<immediately executable action>",
  "completion_condition": "<observable condition that completes the action>",
  "constraints": "<constraints that govern the action>",
  "target": "<specific target>"
}
```

## External effects

<Record remote, tracker, deployment, or other external mutations; state none when there were none.>

## Remaining code by active scope

```json
[
  {
    "highest_authorized": true,
    "parent_scope_id": null,
    "remaining_code": true,
    "remaining_code_detail": "<explain why code does or does not remain>",
    "remaining_work": true,
    "scope_id": "<highest-authorized-scope-id>",
    "scope_kind": "standalone",
    "status": "in-progress"
  }
]
```

## Next-session prompt

```text
<self-contained prompt that includes preflight reconciliation and the exact action>
```
