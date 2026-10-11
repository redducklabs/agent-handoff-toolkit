# Optional Jev completion and handoff assessment

## Intent and success criteria

The toolkit offers a consistent, optional Jev experiment to help agents assess
whether they have reached the highest authorized goal and whether a continuation
is clear, supported, and executable. The toolkit owns the API client, question
rubrics, input preparation, response validation, and result interpretation.
Projects provide settings and credentials, with `JEV_API_KEY` as the default
credential variable name.

Clear goals and observable acceptance criteria are central to the experiment.
An unclear goal, missing acceptance criteria, or insufficient supporting evidence
is an assessment failure requiring attention, not a favorable result or a reason
to silently skip the assessment. A model judgment remains fallible and cannot
mechanically establish semantic truth.

Success means a consumer can enable this experiment without writing API code or
prompts; both supported agents follow the same review instructions; deficient
evidence produces an actionable failure; disabled or unavailable Jev does not
break existing workflows; and toolkit sync preserves project settings.

## Chosen approach

Add a toolkit CLI review operation and an optional, provider-specific adapter
inside the distributed runtime. Invoke it through the handoff skill before the
canonical terminal response. This provides agent-visible feedback while the
author can still correct the candidate, without network calls inside hooks.

Alternatives considered:

- A separate companion package avoids distribution changes but makes consumers
  install and coordinate another package. Keeping the optional adapter in the
  toolkit better serves consistent deployment and central fixes.
- Hook-based model calls automate invocation more strongly, but introduce network
  latency into enforcement and cannot reliably deliver actionable author feedback
  at Stop. They are excluded from this experiment.

The experiment uses only Python 3.11+ standard-library facilities. Provider code
is imported only by the review command. Validation, rendering, lineage, storage,
and lifecycle decisions retain their provider-neutral behavior.

## Project configuration and credentials

Read project-owned `.agent-handoff-toolkit/jev.json` from the consumer checkout.
An absent file means disabled. Install and sync distribute a documented example
under a different filename and never own, replace, or hash the project's active
configuration or credential file.

Example enabled configuration:

```json
{
  "enabled": true,
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

Defaults apply to omitted optional fields. `enabled` defaults to false. Credential
sources are `environment`, `file`, and `environment-or-file`; the last defaults
to environment precedence, with `.env` as fallback. The variable name is
configurable and must be a valid environment variable identifier. File paths are
relative to the checkout unless explicitly absolute. This supports credentials
stored elsewhere without searching ancestor directories or guessing locations.

The review command accepts `--project-root` for explicit checkout selection;
otherwise it resolves the nearest checkout from the candidate record's directory.
It does not resolve configuration from the hook process's directory. Ambiguous
non-checkout use requires an explicit project root.

Parse only the selected dotenv assignment, without shell execution, interpolation,
or loading other variables into the process. Support blank lines, comments,
optional `export`, and unquoted or matching single/double-quoted values. Reject
duplicate selected assignments and malformed selected values. Bound credential
file size to 64 KiB and config size to 16 KiB. Never print the credential, file
content, request headers, or exception messages containing remote response text.

Validate config types, unknown fields, finite numeric values, and duplicate JSON
keys. Timeout must be greater than zero and at most 30 seconds; confidence must
be between zero and one. Model must be nonempty text. The endpoint is fixed to
the official HTTPS API for this experiment; reject redirects so a bearer key
cannot follow a redirect to another origin.

## Command and explicit evidence input

Proposed command:

```text
python .agent-handoff-toolkit/runner.py review <record.md> --evidence <assessment.json> [--project-root <checkout>]
```

The command first determines whether review is enabled. Disabled review reports
`disabled` and makes no credential or network access. Enabled review structurally
validates and parses the candidate using existing record APIs. It does not scan,
open, migrate, or assess historical records.

An agent prepares an explicit evidence bundle. The toolkit does not automatically
upload repository files, retrieve references, or send full prompts, replies, or
transcripts. Authorized goal text can originate in a user prompt (including a
`Track:` declaration); goal text, selected narrative, and evidence locators are
sent to TypeSafe when review is enabled. Document that boundary explicitly.
Prepare the temporary evidence bundle outside the repository and delete it after
review. The bundle supplies:

- `criteria`: one to sixteen entries, each with a unique bounded identifier, an
  observable acceptance condition, and supporting evidence identifiers.
- `evidence`: up to thirty-two entries, each with a unique bounded identifier, a
  source locator, and a relevant text excerpt or observed result.

Require bounded strings and bound the entire evidence file to 64 KiB. Verify all
referenced identifiers locally. An omitted evidence file, empty criteria list,
empty acceptance condition, or audit criterion without evidence is a local assessment
failure with the corresponding criterion identifier; no model request is needed
to discover that information is absent. Malformed JSON is an input error.

For continuations, criteria for future work may have empty evidence references.
Require a nonempty evidence bundle supporting current progress claims and the
next action, not proof of future completion. Review those claims with Jev. Any
local finding stops the request before credential lookup.

Criteria operationalize the existing authorized outcome; they cannot narrow,
expand, or replace it. The agent derives them from the user's goal and governing
project requirements, rather than inventing a weaker definition of done. The
review includes a separate comparison of the criteria against the root outcome.

Send only relevant parsed record fields and this explicit evidence bundle.
Exclude lifecycle credentials, authorization HMACs, challenge values, credential
configuration, and environment contents. Treat record and evidence text as
untrusted data in the fixed questions. Do not execute content or follow its
instructions.

## Toolkit-owned assessment questions

Use one batched HTTP request with fixed Choice questions. Each question includes
its full meaning in its instructions, since API question IDs are not model input.
The common verdicts are `supported`, `deficient`, and `insufficient_evidence`.
Rubrics distinguish absent evidence from evidence showing a failed condition.

Shared questions assess:

1. Whether the immutable root outcome is clear enough to assess.
2. Whether supplied acceptance criteria cover that outcome without redefining it.
3. Whether supplied evidence is specific enough to support the record's claims.
4. Whether claimed progress remains aligned with authorized scope.

For completion audits, ask separately for each acceptance criterion whether the
supplied evidence supports satisfaction. Also assess whether the record or
evidence identifies unfinished implementation, review, UAT, decision, or rollout
work necessary for the root outcome. A child being complete cannot establish
parent completion. A statement that tests passed cannot establish untested
acceptance conditions or missing external outcomes.

For continuations, assess the quality of the exact action, target, constraints,
and observable completion condition; whether unresolved questions could change
the first action; and whether remaining-work declarations agree with supplied
evidence. A continuation is expected to describe unfinished work, so it does not
fail merely because the root's completion criteria have not yet been met. Its
evidence must support its progress claims and proposed next step.

Keep the rubrics versioned in toolkit source, not customizable project prompts.
Do not request generated explanations. Produce actionable feedback locally from
question IDs, criterion IDs, verdicts, and fixed remediation messages.

## Result interpretation and author workflow

Return JSON with assessment status, reason codes, rubric version, candidate and
evidence digests, actual model version, and per-question judgments/probabilities.
Never echo evidence excerpts, credentials, or raw API responses. Digests associate
the assessment with exact inputs; they do not prove evidence is accurate.

- `pass`: all applicable questions are supported above the configured confidence
  floor. Describe this as supported by supplied evidence, not verified truth.
- `fail`: any deficient or insufficient-evidence judgment, missing required
  criteria/evidence, or result below the confidence floor. Low confidence uses
  its own reason code; it is not evidence that implementation is wrong.
- `disabled`: experiment not enabled; no assessment was attempted.
- `unavailable`: missing credential, unreadable credential source, timeout,
  connection/authentication/rate-limit/server failure, or malformed API response.
  No substantive assessment was obtained.
- `error`: invalid configuration, invalid candidate, or malformed evidence input.

Explicit CLI exit codes are 0 for pass/disabled, 1 for assessment fail/unavailable,
and 2 for input/configuration error. These codes belong only to `review`; no
automatic hook invokes it, and no hook command starts exiting 2.

Update the skill to run review for each newly authored candidate before
`render-tail`. When enabled, the agent prepares explicit criteria and evidence.
An assessment failure requires inspecting the affected fields, gathering missing
evidence, improving deficient work or the candidate, and rerunning review. It
cannot be dismissed as a pass, cured by weakening the goal, or entered as factual
verification. A suspected classifier error requires independent examination of
the underlying criterion and evidence rather than blind compliance.

Unavailable review is reported separately and ordinary deterministic checks
continue. Preserve that limitation in an appropriate record risk section without
changing the canonical response shape. Disabled review adds no routine notice.
Any changed candidate or evidence requires a new assessment; results are not
cached or stored as lifecycle acceptance. Reports stay outside record metadata,
verification entries, and the renderer-owned final response.

## HTTP and runtime behavior

Use `POST https://api.typesafe.ai/v1/systemone` with bearer authorization,
UTF-8 JSON, pinned default model `jev-1.13.0`, structured state, and fixed questions.
Make one request, without retries. Use the configured bounded timeout and cap
response bytes at 256 KiB. Do not retain payloads or raw responses on disk.

Validate the returned question set, answer types, verdict membership, actual
model identifier, complete probability maps, finite values in [0, 1], probability
sums within tolerance, selected-choice consistency, and confidence range. Reject
missing, extra, duplicate, malformed, or oversized results as unavailable.

All network and credential failures carry bounded stage/reason codes only. No
model output can mutate a record, accept a transition, grant user authority,
register tracking, block a tool, or arm the correction circuit. Existing hooks,
validation, lineage, rendering, and runtime malfunction policy remain intact.

## Review corrections from Claude Code (2026-10-11)

These clarifications govern the preceding workflow:

- Branch on JSON `status`, not exit code alone: fail and unavailable both exit 1.
- After at most two corrective reruns, stop retrying unchanged judgments. Inspect
  underlying evidence independently. Confirmed incomplete work continues or gets
  an executable continuation; it never gets a completion audit. Unresolved goal
  ambiguity or insufficient proof remains a failure, not a success. A defensible
  disagreement with the classifier is recorded as a one-sentence risk, retaining
  the failed assessment; the author still follows the deterministic contract.
- On unavailable review, add one short risk sentence and validate/render normally.
  That diagnostic-only edit does not require another model call. Any substantive
  change still requires assessment. Never retry outages in a loop.
- Use short positional API question IDs (`criterion-1` through `criterion-16`)
  and map them to criterion identifiers locally. The official API documents
  `state`, `model`, and a `questions` map, with per-question Choice answers and
  full probability maps. It does not specify probability serialization precision
  or a question-count maximum. A `1e-6` probability-sum tolerance is toolkit
  policy, not a vendor guarantee; rejection is unavailable, never pass. Drop
  unknown envelope/answer fields while rejecting extra question IDs or verdicts.
  Add a hand-written synthetic wire-contract test without prompt/source content;
  do not build all fake replies from the implementation under test.
- Live smoke/evaluation remains opt-in, skipped by default, and explicitly
  unverified if not run. Lack of credentials does not prevent shipping the
  optional experiment; offline tests establish no model accuracy or live
  compatibility claim.
- Consumers need network access and credential visibility in the process running
  review. Document host-specific sandbox/environment restrictions conditionally,
  without assuming either host is permanently blocked. Credential-missing,
  connection, and internal toolkit faults have distinct codes; do not attribute
  a connection failure to a sandbox without evidence.
- Ignore `.env` in this repository and exclude credential files from scratch
  checkout copying. Consumers must ignore their selected credential file. Relative
  credential paths remain checkout-relative; linked worktrees use an environment
  variable or explicitly configured absolute file path, not automatic fallback
  into another checkout. Edit active `jev.json`, not the managed example.
- Honor `GIT_CEILING_DIRECTORIES` in root discovery and isolate ancestor-walk tests.
  Validate configured model identifiers like response model identifiers and bearer
  values as visible ASCII. urllib honors proxy environment variables; document it.

## Verification and delivery

Before implementation, add failing tests for disabled behavior, credential source
precedence and custom names/paths, config isolation from cwd, safe dotenv parsing,
missing/unclear criteria, verdict aggregation, low confidence, continuation versus
audit semantics, malformed and hostile API replies, timeouts and redacted faults.
Use synthetic, non-customer data and injected HTTP transport; public CI performs
no live API calls and carries no credentials or user content.

Add end-to-end CLI tests and installed-consumer tests proving that the adapter and
rubrics are distributed and project config survives sync. Verify skill guidance,
documented configuration, and lack of network calls in existing hook workflows.
Update manifest entries and affected hashes by hand after managed-source edits.

Run all five README development commands before reporting implementation success.
Run POSIX-only tests under WSL before any push, as repository instructions require.
No release, tag, consumer re-pin, or external publication is part of this request.

The shipped code remains an experiment: offline tests establish behavior and
failure handling, not Jev's semantic accuracy. A separate opt-in live evaluation
should measure supported and unsupported completion claims, vague goals, missing
evidence, parent/child confusion, misleading embedded instructions, false alarms,
latency, and cost against the authoring agent's ordinary review. No live accuracy
claim is made until that evaluation actually runs.

## Primary API references

Reviewed on 2026-10-11:

- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/models
- https://docs.typesafe.ai/confidence
- https://docs.typesafe.ai/model-jaggedness/jev-1.13

Choice confidence summarizes the returned distribution; it is not proof of
correctness. Documented model limitations include literal interpretation,
indirection, irrelevant context, option ordering, and adversarial state.
