Read `docs/agent-handoff/contract.md` and the installed `agent-handoff` skill
before creating or changing a continuation or completion audit.

- Use a continuation only when authorized work remains.
- Create a completion audit when the highest authorized scope completes.
- Resolve every question that gates the next action before finalizing a continuation.
- The contract is the shared minimum for new records; project overlays may add
  stricter requirements.
- When an overlay conflicts with record semantics or final response shape, the
  contract's continuation/completion distinction and final-tail requirements are
  canonical.
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
- Render a continuation's required final tail with
  `python .agent-handoff-toolkit/runner.py render-tail <continuation-record>`.
- Run `install` and `sync` only from the exact toolkit release checkout. The
  vendored runner intentionally contains the record and hook runtime, not the
  installer modules needed to update itself.
