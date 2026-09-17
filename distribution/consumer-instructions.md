Agent handoff toolkit. Sessions run untracked by default and nothing is gated
until a root is registered.

- Register a root only for work that will continue in another session or that
  the user will resume later. Attempt
  `python .agent-handoff-toolkit/runner.py lifecycle register-root --scope-id <id> --scope-kind <kind> --scope-title "<title>" --scope-outcome "<outcome>"`;
  the denial returns the bound command after `Command:`; run that verbatim.
- A tracked session ends only with a continuation or a completion audit. Use
  the `agent-handoff` skill to author it. The renderer is the only source of
  the final response: send the `render-tail` output as your entire final
  message, with nothing before or after it.
- A user prompt whose first line is `Track: <goal>` registers that goal as the
  tracked root. A prompt whose first line is `Continue from handoff: <path>`
  resumes that record's tracked chain. Either way the rest of the prompt is the
  work, and `AHK-RESUME-FAILED` or `AHK-TRACK-FAILED` means it did not take
  effect; tell the user.
- `AHK-DECLARE` and `AHK-NO-HANDOFF` are notices, not errors, and each fires
  once. `lifecycle one-off` records that a session needs no handoff.
- If a hook looks broken, run
  `python .agent-handoff-toolkit/runner.py lifecycle doctor` from the
  repository root. An `AHK-HOOK-RUNTIME` notice blocks nothing in an untracked
  session.
- Handoff records that predate the installed release are historical. Do not
  read, validate or migrate them.
- Run `install` and `sync` only from the toolkit release checkout.
