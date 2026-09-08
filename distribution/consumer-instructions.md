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
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
- Render a continuation's required final tail with
  `python .agent-handoff-toolkit/runner.py render-tail <continuation-record>`.
- The continuation tail must begin with `Continue from handoff`, state the exact
  next action, and list only essential blockers, decisions, and validation gates.
  Keep `next_session_prompt` within 120 words. The prompt and `exact_action`
  fields must not reproduce the handoff document; the generated absolute link
  is how the next session locates it.
- Run `install` and `sync` only from the exact toolkit release checkout. The
  vendored runner intentionally contains the record and hook runtime, not the
  installer modules needed to update itself.
- Complete the post-install acceptance checklist in
  `.agent-handoff-toolkit/consumer-integration.md` before enabling the workflow
  in a consumer.
