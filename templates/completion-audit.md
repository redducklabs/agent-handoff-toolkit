> Audit record — not a handoff. Do not use this file to start or continue a session.

<!-- agent-handoff-metadata
{
  "schema_version": 1,
  "record_type": "completion-audit",
  "timestamp": "<replace with ISO-8601 timestamp including timezone>",
  "active_scopes": [
    {
      "scope_id": "<completed-highest-authorized-scope-id>",
      "scope_kind": "standalone",
      "parent_scope_id": null,
      "highest_authorized": true,
      "remaining_work": false,
      "remaining_code": false,
      "remaining_code_detail": "<explain why no code remains in this scope>",
      "status": "complete"
    }
  ],
  "verification": [
    {
      "check": "<command or check>",
      "result": "not-run",
      "reason": "<why this check has not run>"
    }
  ],
  "completed_scope_id": "<completed-highest-authorized-scope-id>",
  "authorization_basis": "<how this completed outcome was authorized>"
}
-->

# Completion audit

## Completed objective

<State the completed highest authorized outcome.>

## Authoritative references

<List repository instructions, governing designs, plans, ADRs, issues, and rollout references.>

## User decisions

<Record settled decisions that defined completion.>

## Final repository state

<Record the final branch, HEAD, index, untracked files, remote, tracker, and rollout state.>

## Completed work

<Describe the delivered work and explicitly closed scopes.>

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

## Known risks or separately tracked follow-ups

<List residual risks or independently authorized work; state none when there are none.>

## External effects

<Record remote, tracker, deployment, or other external mutations; state none when there were none.>
