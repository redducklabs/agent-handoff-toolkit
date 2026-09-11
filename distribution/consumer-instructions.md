Read `docs/agent-handoff/contract.md` and the installed `agent-handoff` skill
before creating or changing a continuation or completion audit.

- Use a continuation only when authorized work remains.
- Create a completion audit when the highest authorized scope completes.
- Resolve every question that gates the next action before finalizing a continuation.
- The contract is the shared minimum for new records. Project overlays may add
  stricter requirements, but cannot loosen or contradict the record-type
  decision, metadata-derived canonical sections, or final-tail requirements.
- Treat every handoff record that existed before the current pinned release was
  adopted in the consumer as a deprecated historical artifact by policy.
  During installation or synchronization, do not open, read, review, validate,
  migrate, summarize, reconcile, or rewrite those records. Apply the current
  contract only to new or materially replaced records. Do not resolve questions
  from or mark individual legacy files.
- Treat verification results, exact next actions, and other canonical sections
  as generated content derived from record metadata, not handwritten prose.
- For schema v2, run `lifecycle inspect` before creating a tracked record. Use
  `lifecycle register-root`, `lifecycle resume`, and `lifecycle join` only for
  their defined lifecycle states; an existing handoff does not authorize a new
  root or immutable scope definition without an approved transition.
- Explicitly validate the direct candidate against the locked root and lineage.
  The renderer-only terminal response is the only permitted tracked final response;
  do not add a handwritten preamble or summary.
- Register a legitimate decision request only when user authority is required for
  a blocked exact action. Comply with corrective Stop feedback: an attempted
  message may already be displayed, but it is not a compliant terminal outcome.
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
- Render a continuation's required final tail with
  `python .agent-handoff-toolkit/runner.py render-tail <continuation-record>`.
- The continuation tail must state `This session is stopped because authorized
  work remains` and `What you need to do: Start a new session from the
  continuation handoff below`. Its fenced block for the new session begins with
  `Continue from handoff`, states the exact next action, and lists only essential blockers,
  decisions, and validation gates.
  Keep `next_session_prompt` within 120 words. The prompt and `exact_action`
  fields must not reproduce the handoff document; the generated absolute link
  is how the next session locates it.
- Run `install` and `sync` only from the exact toolkit release checkout. The
  vendored runner intentionally contains the record and hook runtime, not the
  installer modules needed to update itself.
- Complete the post-install acceptance checklist in
  `.agent-handoff-toolkit/consumer-integration.md` before enabling the workflow
  in a consumer.
