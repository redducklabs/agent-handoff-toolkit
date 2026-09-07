Read `docs/agent-handoff/contract.md` and the installed `agent-handoff` skill
before creating or changing a continuation or completion audit.

- Use a continuation only when authorized work remains.
- Create a completion audit when the highest authorized scope completes.
- Resolve every question that gates the next action before finalizing a continuation.
- The contract is the shared minimum for new records. Project overlays may add
  stricter requirements, but cannot loosen or contradict the record-type
  decision, metadata-derived canonical sections, or final-tail requirements.
- Treat every pre-toolkit handoff as a deprecated historical artifact. During
  installation or synchronization, do not open, read, review, validate, migrate,
  summarize, or reconcile those records. Apply the current contract only to new
  or materially replaced records.
- Treat verification results, exact next actions, and other canonical sections
  as generated content derived from record metadata, not handwritten prose.
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
- Render a continuation's required final tail with
  `python .agent-handoff-toolkit/runner.py render-tail <continuation-record>`.
- Run `install` and `sync` only from the exact toolkit release checkout. The
  vendored runner intentionally contains the record and hook runtime, not the
  installer modules needed to update itself.
- Complete the post-install acceptance checklist in
  `docs/consumer-integration.md` before enabling the workflow in a consumer.
