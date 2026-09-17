Agent handoff toolkit. Sessions run untracked by default and nothing is gated
until a root is registered.

- Register a root only for work that continues in another session or that the
  user will resume later. Attempt
  `python .agent-handoff-toolkit/runner.py lifecycle register-root --scope-id <id> --scope-kind <kind> --scope-title "<title>" --scope-outcome "<outcome>"`;
  the denial returns the bound command after `Command:`; run that verbatim.
- A user prompt whose first line is `Track: <goal>` registers that goal as the
  root; one whose first line is `Continue from handoff: <path>` resumes that
  record's chain. Either way the rest of the prompt is the work, and
  `AHK-TRACK-FAILED` or `AHK-RESUME-FAILED` means it did not take effect.
- A tracked session ends with a continuation or a completion audit, authored
  through the `agent-handoff` skill. The renderer is the only source of the
  final response: send the `render-tail` output as your entire final message.
  A turn that is merely unfinished may instead end on the exact
  `progress_response` from `lifecycle inspect`; nothing else record-less is
  accepted.
- `AHK-DECLARE` and `AHK-NO-HANDOFF` are notices, not errors, and each fires
  once. `lifecycle one-off` records that a session needs no handoff.
- If a hook looks broken, run
  `python .agent-handoff-toolkit/runner.py lifecycle doctor` from the
  repository root. An `AHK-HOOK-RUNTIME` notice blocks nothing in an untracked
  session.
- Handoff records that predate the installed release are historical. Do not
  read, validate or migrate them.
- Run `install` and `sync` only from the toolkit release checkout.
