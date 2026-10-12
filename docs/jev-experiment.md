# Optional Jev assessment experiment

Jev provides voluntary advice about newly authored audits and continuations,
outside hooks and lifecycle enforcement. Pass means supported by supplied
evidence, **not verified** semantic truth. Unclear goals, deficient work,
insufficient evidence and low confidence fail assessment. Unavailable never means
pass. Existing structural validation and canonical terminal responses still apply.

## Setup

Copy the installed `.agent-handoff-toolkit/jev.example.json` to project-owned
`.agent-handoff-toolkit/jev.json`, then set `enabled` to `true` in the active
file. Edit active configuration, not the managed example: editing the example
causes sync conflicts. Install/sync never owns active configuration or credentials.
Provide active configuration in each worktree: copy it there or commit the nonsecret
settings if project policy permits. An ignored active file does not automatically
appear in linked worktrees; its absence deliberately disables review.
Missing configuration means disabled; malformed configuration is an error even
when disabled. Omitted fields take these defaults:

```json
{
  "enabled": false,
  "credential": {
    "variable": "JEV_API_KEY",
    "source": "environment-or-file",
    "file": ".env"
  },
  "model": "jev-1.13.0",
  "timeout_seconds": 10,
  "minimum_confidence": 0.8
}
```

Credential sources are `environment`, `file`, and `environment-or-file`.
The last gives a nonempty environment value precedence, falling back only when
absent/empty. Customize `variable` with an `[A-Za-z_][A-Za-z0-9_]*` name and
`file` with an absolute or checkout-relative path. Linked worktrees should use
environment credentials or an explicitly configured absolute file; the toolkit
never searches another worktree for keys. Ignore your selected credential file
in Git before storing a key and exclude it from scratch copies. This repository
ignores `.env` and excludes it and active `jev.json` from scratch checkout copies.
No credential value appears here; configure keys through your secret manager or
process environment. Never put secrets or customer data into assessment evidence.

Only the selected dotenv assignment is parsed, without shell execution,
interpolation, escape expansion or environment mutation. Blank lines, comments,
optional `export`, CRLF, and matching single/double quotes are supported.
An optional UTF-8 BOM in credential files is accepted.
Unquoted inline comments start after whitespace; after a closing quote allow only
whitespace or a comment. Duplicate, empty and malformed selected assignments are
unavailable. Unrelated malformed lines are not evaluated. Bearer characters must
be visible ASCII (0x21–0x7E).

Unknown configuration fields, nested duplicate JSON keys, invalid UTF-8,
nonfinite numbers and boolean numeric values are rejected. Config is capped at
16 KiB, credential files at 64 KiB. Timeout must be greater than zero and at most
30 seconds; confidence is in [0,1]. Model IDs match
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}`.

The process running review needs network access and selected credential visibility.
Depending on local Codex sandbox or Claude Code permission settings, configure
those capabilities for that process. A connection error alone does not establish
a sandbox failure. urllib honors proxy environment variables, including `HTTPS_PROXY`.

## Prepare evidence and review

Derive observable acceptance criteria from the immutable authorized outcome and
governing project requirements. Never weaken/redefine the goal to obtain a pass.
Prepare temporary UTF-8 evidence outside the repository; delete it after review.
This synthetic example shows the exact schema:

```json
{
  "criteria": [{
    "id": "acceptance-1",
    "condition": "The installed review command runs.",
    "evidence_ids": ["observation-1"]
  }],
  "evidence": [{
    "id": "observation-1",
    "source": "local verification of synthetic installation",
    "text": "Installed command returned disabled with exit zero."
  }]
}
```

Only the shown fields are allowed. IDs match `[A-Za-z][A-Za-z0-9_-]{0,63}` and
are unique within each list. Maximums: 16 criteria, 32 evidence entries, 32 unique
resolved references per criterion, 1000 characters per condition/source, 8000 per
text, and 64 KiB for the entire evidence file. Wrong types, unknown fields,
duplicate IDs/references, unresolved references and excessive limits are errors.
Missing evidence, empty lists, blank conditions/source/text, and audit criteria
without references fail locally before credentials are read. Future continuation
criteria may lack references; overall evidence must support current claims and
the next action, not future completion.

```text
python .agent-handoff-toolkit/runner.py review handoffs/<new-record>.md --evidence <temporary-evidence.json>
```

`--project-root <checkout>` explicitly selects settings. Otherwise the nearest
`.git` file/directory above the candidate determines the checkout, respecting
`GIT_CEILING_DIRECTORIES`, independently of the invoking cwd. Non-checkout
candidates need an explicit root. Enabled review accepts valid schema-v2 records
only and never scans historical handoffs. Disabled returns zero without checking
candidate or evidence paths.

When enabled with locally sufficient evidence, selected root goals, scope
definitions/progress, narrative, verification, next action/prompt, and supplied
evidence text/locators **leave the machine** for TypeSafe. Goal wording may
originate in user prompts, including `Track:` declarations. Full prompts, replies
and transcripts are not collected. Locators do not trigger file uploads.
Authorization HMACs, lineage identifiers/digests, credential configuration and
environment contents are excluded. Payloads/raw responses are never stored;
reports expose only fixed reasons, digests, actual model and validated answers.

## Interpret JSON status

| Status | Exit | Author action |
| --- | --- | --- |
| `pass` | 0 | All judgments supported at the floor; continue deterministic checks. |
| `disabled` | 0 | No assessment attempted; add no routine notice. |
| `fail` | 1 | Inspect each finding, improve work or gather proof, reassess. |
| `unavailable` | 1 | No substantive judgment; add one risk sentence and validate/render normally. |
| `error` | 2 | Correct configuration, candidate or evidence input. |

Branch on JSON status, never exit alone. `credential-missing` identifies absent
selected credentials; `credential-read`/`credential-format` identify source
problems; `connection`/`timeout` identify transport faults; `internal` identifies
a toolkit defect, not a presumed outage. Other fixed codes distinguish redirect,
authentication, rate-limit, server/HTTP and invalid response size/JSON/schema.
Underlying exception or remote response text never appears in feedback.

Fixed shared questions assess goal clarity, criteria coverage, evidence
specificity and scope alignment. Audits assess each criterion using positional
IDs `criterion-1` through `criterion-16`, mapped locally to criterion IDs, plus
root completion. Completed children never establish parent completion.
Continuations assess next action, hidden gates and remaining-work consistency;
unfinished authorized work is expected. Fixed instructions treat state as data
and ignore embedded instructions; this does not guarantee adversarial resistance.

Verdicts are `supported`, `deficient`, `insufficient_evidence`. Non-supported
judgments stay failed even with low confidence; supported judgments below the
floor fail as `low-confidence`. Static remediation replaces generated explanations.
After at most two corrective reruns, independently examine remaining findings.
Confirmed unfinished work continues or receives an executable continuation,
never an audit. Unresolved ambiguity/proof remains failed. Defensible classifier
disagreement gets one risk sentence while retaining the failed assessment.
Advice belongs outside factual verification, metadata and the final response.
On unavailable review, add one risk sentence; do not rerun for that diagnostic-only
edit or loop on outages. Substantive candidate/evidence changes need reassessment.
Candidate digests use toolkit record normalization; evidence digests cover exact
bytes. Neither proves truth, and acceptance is never cached in lifecycle state.

## API contract and evaluation

One POST to `https://api.typesafe.ai/v1/systemone` sends model, structured state
and Choice questions with `instructions` and an option-to-description `criteria`
map. No retries or redirects. Responses require a valid actual model, exactly
the requested question IDs, Choice type, allowed verdict, full probability map
and confidence. Numbers are finite in [0,1], with maximum-probability selected
choice (ties allowed). Confidence must agree with the documented three-option
Choice formula `(3 * maximum_probability - 1) / 2` within a rounding bound of
`0.0125` plus numeric tolerance `1e-6`. Two-decimal probability rounding can
contribute `1.5 * 0.005`, and confidence rounding another `0.005`; discrepancies
outside that bound remain unavailable. The reported assessment confidence is
the lower of the returned confidence and the formula applied to the returned
probabilities (clamped at zero) when they differ beyond numeric tolerance, so
upward rounding cannot raise it above the configured floor. Numerically coherent
confidence is retained to avoid floating-point threshold errors. Probability
sums retain tolerance `1e-6`.
Unknown envelope/answer fields are discarded. The 256 KiB
response cap also applies to injected transports. Socket-operation timeout does
not guarantee a strict total elapsed deadline; no background request continues.

Rubric: `jev-assessment-v1`; default pinned model: `jev-1.13.0`. Probability-sum
tolerance `1e-6` and at most 21 questions are toolkit policies, not vendor guarantees.
See [API](https://docs.typesafe.ai/api), [models](https://docs.typesafe.ai/models),
[confidence](https://docs.typesafe.ai/confidence) and
[model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

Offline synthetic tests establish neither semantic accuracy nor live compatibility.
Explicitly opt in to a synthetic-only compatibility smoke check by setting
`AHK_JEV_LIVE_SMOKE=1` and `JEV_API_KEY` in the process environment, then running
`python -m unittest discover -s tests -p test_jev_live.py -v`. No live request runs
without opt-in. This tests response compatibility, not accuracy.

For separate evaluation, label synthetic completion/handoff cases before running
the pinned model/rubric. Include vague goals, missing proof, failed conditions,
unfinished review/UAT/rollout, child/parent confusion and misleading embedded
instructions. Compare with author self-review; measure false alarms, misses,
latency and cost. Keep labels independent of answers and report sample size and
limitations. Do not claim semantic accuracy until evaluation actually runs.
