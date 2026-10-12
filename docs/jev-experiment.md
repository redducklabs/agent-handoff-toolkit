# Optional Jev implementation assessment

Jev assesses whether supplied evidence supports implemented behavior, shows work
remaining, or cannot establish either. It is voluntary advice outside hooks and
lifecycle enforcement. It does not prove code correctness or authorize completion.
The same CLI and author workflow apply to Claude Code and Codex.

## Machine-local setup

Copy `.agent-handoff-toolkit/jev.example.json` to ignored
`.agent-handoff-toolkit/jev.json` and set `enabled` to true. Keep enablement,
credential location and active settings machine-local; never commit or copy them
between machines. Missing configuration disables review. Install/sync owns the
example, never active configuration or credentials. Configure each checkout
independently. Defaults:

```json
{
  "enabled": false,
  "credential": {"variable": "JEV_API_KEY", "source": "environment-or-file", "file": ".env"},
  "model": "jev-1.13.0",
  "timeout_seconds": 10,
  "minimum_confidence": 0.8
}
```

Credential sources are `environment`, `file`, or `environment-or-file`; the last
uses a nonempty environment value first. Absolute or checkout-relative credential
paths are allowed; worktrees never search another checkout for keys. Ignore the
credential file before storing a key. Dotenv reads only the selected assignment,
without execution, interpolation, environment mutation or escape expansion.
Optional export, BOM, CRLF, matching quotes and comments are supported; duplicate,
empty or malformed selected assignments are unavailable. Bearer values must be
visible ASCII. Credentials never enter evidence or reports.

Unknown fields, duplicate JSON keys, invalid UTF-8, boolean numeric values and
nonfinite numbers are errors. Config is capped at 16 KiB, credential files at
64 KiB. Timeout is greater than zero and at most 30 seconds; confidence is [0,1].
Model names match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. The process needs network and
credential visibility; urllib honors proxy environment variables. A connection
error does not establish a sandbox failure.

## Construct a verification case

Start with the immutable authorized outcome and explicit user decisions. Discover
affected files, callers, alternate paths, configuration and tests through repository
inspection, rather than treating the changed-file list as exhaustive. Account for
unchanged/shared implementations. Do not invent deployment or approval gates.

Write sanitized evidence in a temporary file outside the repository. Supply relevant
implementation excerpts and specific observed checks with their sources. Passing
tests support only exercised behavior. Author assertions are not trusted proof.
Exclude credentials, customer/provider records, tax IDs, full prompts, replies,
transcripts and raw logs. Version-2 example:

```json
{
  "case_version": 2,
  "criteria": [{"id": "validation", "condition": "Both handlers reject empty input.",
    "evidence_ids": ["inspection"], "claim": "implemented"}],
  "evidence": [
    {"id": "inspection", "source": "sanitized inspection and behavior checks",
     "text": "Both handlers call validate_input; empty-input checks pass on both paths."},
    {"id": "discovery", "source": "entry-point search",
     "text": "The web and API handlers are the two entry points; both are in handlers.py."}
  ],
  "features": [{"id": "input-validation", "requirement": "Reject empty input on both handlers.",
    "criterion_ids": ["validation"], "discovery_evidence_ids": ["discovery"],
    "required_paths": ["handlers.py"],
    "components": [{"id": "handlers", "path": "handlers.py",
      "condition": "Both handlers apply empty-input validation.",
      "evidence_ids": ["inspection"], "claim": "implemented"}]}],
  "files": []
}
```

Each condition belongs to exactly one feature. Shared paths may serve several
features, but feature/component IDs are unique. Claims are `implemented` or
`pending`. Components grant no lifecycle authority. Non-code features may have no
components, but still need acceptance conditions and discovery observations.
`required_paths` records the explicit inventory obtained from those observations.
Code checks that each inventoried file is represented by a component. Unaccounted
locations make the case inconclusive; they do not prove absent implementation.
An empty inventory is appropriate for a non-code feature. Inventory honesty and
completeness remain the author's responsibility and require independent inspection.

Optional `files` entries are `{"path":"handlers.py","sha256":"<64 lowercase hex>"}`.
These bind evidence to exact local bytes. Stale/missing/oversized files make
assessment inconclusive before credentials are read. Paths must name a component
and be checkout-relative POSIX paths without traversal; symlink escapes are
rejected. Each bound file is capped at 1 MiB. Hashing uploads neither contents nor
hashes, and does not prove supplied excerpts are truthful.

For detailed coverage, use `case_version: 3` with an additional `requirements`
list. Each unit has `id`, `source` (`root` or `feature:<id>`), integer `start` and
`end` character offsets, the exact source `quote`, and mapped `criterion_ids`.
Anchor root units to the candidate's unchanged authorized outcome and feature
units to the corresponding requirement. Every source must be accounted for by
contiguous, non-overlapping spans from zero to its full length. Split meaningful
obligations and retain their qualifiers; include enough conditions to cover each.
Up to 32 units are allowed, with root text bounded to 8000 characters. Exact
anchors prove text accounting only, never semantic completeness; independently
inspect the decomposition and its evidence. Legacy and version-2 inputs remain
readable and use their original coverage question.

Optional unit `feature_ids` explicitly selects definitions needed to interpret
references. Unrelated feature definitions are excluded. Whole-source units may
map an acceptance condition that is an exact copy of the entire source. Only
that identical whole-source correspondence is accounted for locally, without a
model equivalence question. Its criterion still receives an independent semantic
implementation assessment with every mapped source context. Fragments and
paraphrases retain semantic coverage checks. Reports identify `exact-text` versus
`model` provenance; local accounting invents no answer, probability or confidence.
Exact text never proves implementation or semantic correctness.

Each feature's full requirement is also assessed against its explicitly selected
criterion/component evidence. A narrow passing condition cannot hide a different
unfinished part of that requirement. Coverage findings concern the verification
case: `accounted` (text correspondence), `covered` (model-supported inventory),
`inadequate`, or `cannot-determine`. They do not alone prove a code
defect. `behavior_state` records behavior judgments; overall `state` remains
`cannot-determine` when coverage does not support completion, unless concrete
behavior evidence already establishes `work-remains`.

Limits: 16 criteria for legacy/version-2 inputs, 32 for version-3; 16 features,
32 components total, 32 evidence entries, 32 resolved
references per list, 64 KiB evidence file. IDs match
`[A-Za-z][A-Za-z0-9_-]{0,63}`; conditions/requirements/source are at most 1000
characters, evidence text 8000, paths 240. Unknown fields, duplicate ownership or
references, unresolved references and wrong types are errors. Missing audit proof
is inconclusive. Future pending conditions may lack proof in a continuation.
Legacy `{criteria,evidence}` remains readable without explicit claims or hierarchy.

## Run and interpret

```text
python .agent-handoff-toolkit/runner.py review handoffs/<new-record>.md --evidence <temporary-evidence.json>
```

`--project-root <checkout>` selects machine-local settings and file bindings.
Otherwise the candidate's nearest `.git` determines the checkout, respecting
`GIT_CEILING_DIRECTORIES`, independently of invoking cwd. Enabled review accepts
valid schema-v2 records only and never scans historical handoffs. Disabled review
reads no candidate, evidence or credential.

Questions select relevant conditions and resolved evidence. A semantic
coverage question assesses whether conditions cover the authorized outcome;
exact file-path accounting belongs to code, not a model judgment.
Component/condition judgments are combined in code; no holistic completion verdict
overrides individual findings. Code owns references, hashes, ownership and
aggregation. Jev cannot discover an omitted file without a supplied clue.

| Status | Exit | Interpretation |
| --- | --- | --- |
| `pass` | 0 | Current claims/coverage supported; continue deterministic checks. |
| `inconclusive` | 1 | Missing evidence or uncertainty; inspect or gather proof. |
| `fail` | 1 | Confident contrary evidence; inspect localized work or claim. |
| `disabled` | 0 | No assessment attempted. |
| `unavailable` | 1 | Service/runtime failure, no substantive assessment. |
| `error` | 2 | Correct configuration, candidate or evidence input. |

Read `implementation.state`: `implemented`, `work-remains`, or `cannot-determine`.
A confident contrary child keeps its feature unfinished; uncertainty prevents
supported completion. Negative and positive answers below the floor are uncertain.
In a continuation, explicitly pending findings appear under `progress`, not defect
`reasons`. It can pass while implementation work remains; continue implementing.
Claimed-implemented components still need proof. Legacy continuation criteria
default to pending; version-2 cases declare claims explicitly. Continuations also
check whether progress claims agree with recorded verification and whether a
decision could change the first action. These checks do not require pending
implementation to be finished.

Reports contain fixed messages, IDs/paths/evidence references, digests, validated
model/answers, implementation summaries, elapsed time and request count. No evidence
text, generated reasoning, credential settings, raw responses or transcripts.
Relevant outcome, decisions, remaining-work descriptions, action and selected
evidence leave the machine. Authorization HMACs and lineage IDs are excluded.
There are no implicit source uploads.

Keep advice outside factual verification, metadata and renderer-owned responses.
Confirmed unfinished work means continue or write an executable continuation, never
an audit. Independently inspect classifier disagreement; retain the assessment and
explain defensible disagreement as one risk sentence. After at most two substantive
corrective reruns, inspect remaining findings rather than looping. On unavailable
review add one risk sentence and validate/render normally; do not retry a diagnostic
edit. No result is cached as authority. Delete your temporary evidence; retain only
compact reports.

## Wire contract and evaluation

Requests use Choice at `https://api.typesafe.ai/v1/systemone`, one unique question
per request, no redirects/retries. Its exact selected facts enter the API's `state`;
task, policy and criteria remain in the question. No sibling or unrelated evidence
enters that request. Answers exactly match requested IDs and verdicts
before aggregation. Byte-identical questions share one judgment, with
`judgment_sources` recording reuse; an exactly matching criterion condition and
ordered evidence selection supplies its component judgment too. Different
conditions or evidence remain separate. Select enough related code and observed
inputs/results for each question to stand alone; evidence attached elsewhere is
not implicit context. This reuse never selects the more favorable answer.
Answers use verdicts
`supported`, `deficient`, `insufficient_evidence`. Probability-sum tolerance is
1e-6. Confidence follows `(3 * max_probability - 1) / 2`, allowing two-decimal
rounding of 0.0125 plus 1e-6. Conservative reduction prevents upward rounding from
crossing the floor. Response cap is 256 KiB; unknown response fields are discarded.
All requests validate and agree on actual model; a partial fault is unavailable.
Socket timeout is not a total deadline. Rubric `jev-implementation-v5` defaults to
model `jev-1.13.0`.
Response-schema failures include only a closed validation stage (envelope,
answer-shape, probabilities, or confidence), never response content. Validation
tolerances and the configured confidence floor are unchanged.

Live evaluation requires explicit opt-in:

```text
python -m agent_handoff_toolkit review-evaluate tests/fixtures/jev-evaluation.json --partition development --project-root <configured-checkout> --live
```

Freeze independent labels before model answers; keep related mutations in one
partition. Freeze the evaluator before opening `held-out` results; do not tune on
them. Measure detected/missed/unresolved defects, false defects, unsupported
completion, inconclusive coverage, unavailable calls and elapsed time. Cost is
explicitly unknown when billing usage is unavailable. Outages stop the run without
retry. Catalogs contain synthetic record/evidence objects; labels are never sent.

Initial qualification: 16 defective, 16 clean and 8 insufficient cases; detect every
planted defect, at most one false defect, support at least 15 clean cases, no
unsupported completion or unsupported defect on insufficient evidence, all eight
insufficient cases correctly inconclusive, and no unassessed cases. Report sample size and limitations;
synthetic cases do not establish representative real-world accuracy. Compare
old/new on sanitized STS engineering cases and controlled defects before updating
other consumers. Exercise both hosts and option-order/context sensitivity. Ordinary
tests and opt-in `AHK_JEV_LIVE_SMOKE=1` checks establish compatibility, not accuracy.
Never weaken outcome or labels to obtain qualification.

This candidate is not qualified for rollout. Preserved earlier held-out runs
detected 16, 16 and 15 of 16 planted gaps and supported 12, 10 and 14 of 16 clean
cases respectively. All eight insufficient cases were inconclusive in each run,
with no definite false defects. None meets the unchanged qualification bar.
Independent inspection found incomplete conditions or evidence in some packets,
alongside model uncertainty. A connection failure also stopped an earlier attempt.

The revised whole-source accounting and direct evidence approach matched all
seven independently labeled STS diagnostic cases once: supported implementation,
five controlled gaps, and missing proof. Repeated actual Claude Code and Codex
invocations confirmed installed-command compatibility but exposed model-field
uncertainty and invalid probability responses. Complementary AST and effective
runtime evidence supported the clean case; the next assessment stopped on an
invalid probability sum. Keep every uncertain and unavailable result. Fresh
qualification and reliable STS validation remain pending; do not release or
update other consumers on the strength of one successful diagnostic run.

The subsequent frozen version-4 run assessed all 40 cases: all 16 preregistered
critical gaps were localized at the unchanged floor, no false defects were
reported, and all eight insufficient cases were inconclusive. Only 13 of 16 clean
cases were supported, so it also failed qualification. Later independent diagnosis
found an overbroad input-domain assurance in one synthetic clean case; preserve
its frozen label and result rather than restricting its requirement after scoring.

A reviewed diagnostic moved unchanged selected facts into `state` and isolated
each question. Two opened clean cases became supported, two sampled gaps remained
deficient, and missing proof remained inconclusive. The case with the domain caveat
stayed uncertain. That carrier-and-isolation diagnostic is not fresh qualification.
Version 5 uses this input boundary. It increases requests and latency: each
unique question makes one sequential call; a maximum-size version-3 continuation
can require 115 calls. The configured timeout applies to socket operations, not
an overall assessment deadline. Any fault withholds the whole assessment; no
partial verdict is used.

The version-5 STS diagnostic matched all seven frozen labels without a service
fault and localized each controlled gap. Actual ephemeral Claude Code and Codex
then invoked the installed unreleased candidate: each supported the clean STS
copy, rejected the serializer gap, and kept missing proof inconclusive. These
opened cases establish compatibility and diagnostic progress, not fresh accuracy
qualification. The new independently labeled version-5 comparison selected 40
cases and stopped on case 4 when the response failed probability validation.
Three substantive assessments completed: one localized critical gap, one clean
case remained uncertain, and missing proof correctly remained inconclusive.
The fourth assessment was unavailable and 36 cases were not attempted. This is
an incomplete, unqualified run; unattempted critical seeds are not counted as
misses or detections. Preserve it without normalizing responses, relaxing the
confidence floor or retrying until passing. A provider response-contract
correction is needed before dependable qualification can proceed; the rejected
response's exact probability subcondition is unknown because raw payloads are
not retained. Release and consumer adoption remain on hold.

See TypeSafe's [citation verification](https://docs.typesafe.ai/cookbooks/citation_check),
[decomposed verification](https://docs.typesafe.ai/cookbooks/sde_cascade),
[confidence](https://docs.typesafe.ai/confidence),
[repeatability experiment](https://docs.typesafe.ai/cookbooks/consistency_choice_cookbook)
and [model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
