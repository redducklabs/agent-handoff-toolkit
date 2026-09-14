Read `docs/agent-handoff/contract.md` and the installed `agent-handoff` skill
before creating or changing a continuation or completion audit.

- Use a continuation only when authorized work remains.
- Create a completion audit when the highest authorized scope completes.
- Resolve every question that gates the next action before finalizing a continuation.
- The contract is the shared minimum for new records. Project overlays may add
  stricter requirements, but cannot loosen or contradict the record-type
  decision, the single-copy metadata rule, or final-response requirements.
- Treat every handoff record that existed before the current pinned release was
  adopted in the consumer as a deprecated historical artifact by policy.
  During installation or synchronization, do not open, read, review, validate,
  migrate, summarize, reconcile, or rewrite those records. Apply the current
  contract only to new or materially replaced records. Do not resolve questions
  from or mark individual legacy files.
- A schema-v2 record stores each fact once, in its visible metadata block. No
  section restates verification, the exact next action, the scope list, or the
  next-session prompt.
- A session runs ungated until it registers a root: shell commands, file
  edits, MCP calls, web fetches, subagents, and todo lists all run untouched.
  Register a root only when the work spans more than one session, will be
  handed off, or the user expects to resume it later; otherwise do nothing —
  no lifecycle command is required for conversation, investigation, ticket
  creation, or a single-session fix.
- `lifecycle one-off` records a deliberate decision that a session's work
  needs no handoff. It grants no authority — no authorization ID, no chain —
  and does not make the session tracked.
- The toolkit may attach an informational `AHK-DECLARE` notice on a
  session's first repository write with no registered root, and an
  `AHK-NO-HANDOFF` notice at `Stop` when the repository changed during an
  undeclared session and it ends with work uncommitted. Neither advisory
  blocks, and neither carries a permission decision: the notice is attached
  alongside the host's normal approval flow, which runs unchanged. Treat
  either as a prompt to decide, not as an error to fix.
- For schema v2, run `lifecycle inspect` before creating a tracked record. Use
  `lifecycle register-root`, `lifecycle resume`, and `lifecycle join` only for
  their defined lifecycle states; an existing handoff does not authorize a new
  root or immutable scope definition without an approved transition.
- To register the first root, attempt `lifecycle register-root` with
  `--scope-id`, `--scope-kind`, and the semantic slots as plain text in
  `--scope-title` and `--scope-outcome`. The denial encodes the definition and
  returns the complete bound command after `Command:`; run that verbatim. Do
  not hand-encode `--scope-definition-b64`. A denial names the failed check
  after `failed=`.
- Explicitly validate the direct candidate against the locked root and lineage.
  The renderer-only terminal response is the only permitted tracked final response;
  do not add a handwritten preamble or summary, and do not retype the generated
  response.
- Register a legitimate decision request only when user authority is required for
  a blocked exact action. Comply with corrective Stop feedback: an attempted
  message may already be displayed, but it is not a compliant terminal outcome.
  Blocking feedback names the failed checks after `failed=`.
- Validate new or materially replaced records with
  `python .agent-handoff-toolkit/runner.py validate <record>`.
- Render the required terminal response with
  `python .agent-handoff-toolkit/runner.py render-tail <record>`. Its output is
  the whole response; send it verbatim with nothing before or after it.
- Run `install` and `sync` only from the exact toolkit release checkout. The
  vendored runner intentionally contains the record and hook runtime, not the
  installer modules needed to update itself.
- Complete the post-install acceptance checklist in
  `.agent-handoff-toolkit/consumer-integration.md` before enabling the workflow
  in a consumer.
