> Audit record — not a handoff. Do not use this file to start or continue a session.

```json agent-handoff-metadata
{
  "active_scopes": [
    {
      "highest_authorized": true,
      "parent_scope_id": null,
      "remaining_code": false,
      "remaining_code_detail": "<one sentence stating why no code remains; at most 240 characters>",
      "remaining_work": false,
      "scope_definition": {
        "outcome": "Complete the authorized root outcome.",
        "title": "Authorized root scope"
      },
      "scope_definition_digest": "e357f77f7628a29ab0aa330f77283171baa314a09891675adf3858e54ff975e9",
      "scope_id": "root-scope",
      "scope_kind": "standalone",
      "status": "complete"
    }
  ],
  "authorization_basis": "<how this completed outcome was authorized>",
  "authorization_evidence": {
    "evidence_hmac": "1111111111111111111111111111111111111111111111111111111111111111",
    "kind": "initial-user-turn",
    "proposal_turn_ref": null,
    "user_turn_ref": "user-turn-template-001"
  },
  "authorization_id": "authorization-template-001",
  "authorized_root_scope_id": "root-scope",
  "completed_scope_id": "root-scope",
  "predecessor": null,
  "record_id": "record-audit-template-001",
  "record_type": "completion-audit",
  "schema_version": 2,
  "timestamp": "<replace with ISO-8601 timestamp including timezone>",
  "transition": null,
  "verification": [
    {
      "check": "<command or check; at most 120 characters, and at most 8 entries in all>",
      "reason": "<why this check has not run; at most 160 characters>",
      "result": "not-run"
    }
  ]
}
```

# Completion audit

## Completed objective

<State the completed highest authorized outcome. At most 200 words.>

## Authoritative references

<List repository instructions, governing designs, plans, ADRs, issues, and rollout references. At most 200 words.>

## User decisions

<Record settled decisions that defined completion. At most 200 words.>

## Final repository state

<Record the final branch, HEAD, index, untracked files, remote, tracker, and rollout state. At most 200 words.>

## Completed work

<Describe the delivered work and explicitly closed scopes. At most 200 words.>

## Known risks or separately tracked follow-ups

<List residual risks or independently authorized work; state none when there are none. At most 200 words.>

## External effects

<Record remote, tracker, deployment, or other external mutations; state none when there were none. At most 200 words.>
