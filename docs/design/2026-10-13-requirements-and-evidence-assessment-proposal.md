# Proposal: requirements and evidence assessment for agent-managed work

- Date: 2026-10-13
- Status: design proposal; implementation home undecided; Jev remains on hold
- Intended audience: maintainers of an agent manager, the agent-handoff toolkit,
  and other systems coordinating agent work

## Purpose and approval boundary

Make it possible to assess whether authorized work is adequately specified,
being implemented, supported as implemented, or needs to continue. Establish an
explicit work inventory before implementation and compare accomplishment claims
with independent observations at several stages.

This document is stored in the agent-handoff toolkit for durable reference. Its
storage location does not select the toolkit as the implementation home. The
maintainer is considering an agent manager project, where requirements,
cross-session observations, and work coordination may fit better.

The maintainer authorized writing and committing this proposal. That does not
authorize implementation, provider assessments, restoring Jev, consumer updates,
releases, tracker changes, or changes to lifecycle enforcement. Jev adoption
remains on hold. Subsequent work requires an explicit decision about the hosting
project and the particular implementation or experiment being authorized.

## Reconciled repository baseline

Research was reconciled on 2026-10-13 against published toolkit v1.4.0 at
`ec22c82322b3b0576b88653232a033acfc29c7ba`. Remote main and the peeled release
tag matched that commit. The primary checkout remained on the older
`codex/jev-experiment` branch at
`3ca48400311b26b5ae717a14436a32754a78435c`; its withdrawn Jev instructions
were not treated as current authority.

Version 1.4.0 removed the provider runtime, review CLI, configuration example,
active assessment guide, and authoring gate. The experiment findings remain in
[Jev on hold](../experiments/jev-on-hold.md). No historical implementation
approval authorizes restarting that experiment.

The current [contract](../agent-handoff/contract.md) and
[mechanics](../agent-handoff/mechanics.md) distinguish continuations from
completion audits, lock scope definitions, classify verification as pass, fail,
or not-run, and enforce structural consistency rather than semantic truth.
They allow exactly seven narrative sections, at most 200 words per section,
1,200 words per record, and eight verification entries. Adding a requirements
section or new metadata is a contract change, not part of this proposal.

## Research foundation

Requirements-driven development is useful here as a discipline of explicit
obligations, acceptance criteria, and traceability. Spec-driven development can
extend those obligations into designs and tasks. Neither a generated task list
nor a completed task checklist establishes that the authorized outcome exists.

[GitHub Spec Kit](https://github.com/github/spec-kit/blob/main/spec-driven.md)
separates specification, planning, and task generation. Its
[specification template](https://github.com/github/spec-kit/blob/main/templates/spec-template.md)
uses requirement IDs, acceptance scenarios, edge cases, and clarification
markers. [Kiro's feature specification workflow](https://kiro.dev/docs/specs/feature-specs/)
uses EARS-style conditions and expected behavior. These are useful authoring
patterns, not completion-verification guarantees.

NASA's [requirements definition guidance](https://www.nasa.gov/reference/4-2-technical-requirements-definition/)
calls for stakeholder validation, verifiability, and an agreed baseline. Its
[requirements management guidance](https://www.nasa.gov/reference/6-2-requirements-management/)
emphasizes source provenance, bidirectional traceability, and approved changes.
The proposed design borrows those principles proportionately, without requiring
NASA's organizational process.

### TypeSafe's agent-trace evaluator

The inspected WorkflowEvals revision was
`0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb`.

Its [outcome questions](https://github.com/typesafe-ai/WorkflowEvals/blob/0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb/evals/agent_trace_observability/questions.py)
distinguish fulfillment, confirmed escalation, unaddressed work, and factual
claim support. An assistant's assertion of success is insufficient. However,
honestly explaining unfinished work can satisfy the unaddressed-work question
without fulfilling the request.

Its [evidence selection](https://github.com/typesafe-ai/WorkflowEvals/blob/0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb/evals/agent_trace_observability/state.py)
separates outcomes from user reactions and limits safety assessment to context
preceding the action. Outcome state includes instructions, conversation, tool
arguments/results, errors, and the final message; results are truncated after
1,600 characters. Its pipeline does not meet this proposal's selective,
sanitized English-only input boundary.

Its [gates](https://github.com/typesafe-ai/WorkflowEvals/blob/0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb/evals/agent_trace_observability/gates.py)
record unaddressed work without using it as a decision gate and combine
fulfillment with policy-required escalation. Those operational-triage semantics
must not substitute for fulfillment of an authorized development scope. Its
[evaluation adapter](https://github.com/typesafe-ai/WorkflowEvals/blob/0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb/evals/agent_trace_observability/eval_adapter.py)
measures primary-action agreement; the
[reference protocol](https://github.com/typesafe-ai/WorkflowEvals/blob/0ac3b8ad845429f0d8e064ecfb2430a47c5a25cb/README.md)
uses model agreement, not independently established semantic truth.

Adopt its separation of questions and evidence roles. Do not adopt its
automatic-close policy, whole conversation inputs, scoring thresholds, or
escalation-as-success semantics.

### Evidence for and against Jev feasibility

| Evidence | Observed result | Limit on inference |
| --- | --- | --- |
| [Vals prose claim study](https://www.vals.ai/blogs/independent-evaluation-of-jev) | 390/400 correct; held-out routing exceeded a 1% error budget at 1.6% error and 95% coverage. | Narrow prose entailment results do not establish repository completion accuracy. |
| [LLM-AggreFact comparison](https://github.com/adorosario/jev-rag-claim-verification) | 495 claims; Jev balanced accuracy 73.3%, false verification 23.2%. | Development sample; confidence/cascade thresholds selected in-sample; see [limitations](https://github.com/adorosario/jev-rag-claim-verification/blob/main/medium/article-v3.md). |
| [Linhao Wang's coding-answer evaluation](https://github.com/goya4140/jev-reward-model-evaluation) | RM-Bench Code 67.64% structured pairwise and 75.93% pointwise. | Different answer-judging protocols; aggregates available, raw per-example outputs omitted; see [methodology](https://github.com/goya4140/jev-reward-model-evaluation/blob/main/docs/METHODOLOGY.md). |
| Preserved toolkit experiment | Revised 40-case run detected 16/16 gaps, supported 13/16 clean cases, and kept 8/8 missing-proof cases inconclusive. Corrected six-case diagnostic detected 1/2 gaps and supported 0/2 clean cases. | Failed the qualification bar. Mixed evidence does not isolate code as the cause or establish English-only performance. |

[TypeSafe's model disclosure](https://docs.typesafe.ai/models) identifies English
as the primary training language and recommends pinning model versions when
thresholds are tuned. Its [jaggedness documentation](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
warns about literal interpretation, numeric precision, indirection, distracting
context, adversarial content, and option ordering. English-only assessment is
therefore a fresh candidate to qualify, not a remedy already demonstrated.

### Research matrix

| Use case | Supported basis | Unproven assumption | Proposed treatment |
| --- | --- | --- | --- |
| Feature coverage | Compare requirements with separately established authorized obligations. | Generated requirements exhaust user intent. | Preserve source mappings; separate ambiguities and suggestions from obligations. |
| Readiness | Check observable criteria, domains, and evidence plans. | Clear English guarantees feasible implementation. | Require technical/human review where feasibility needs inspection. |
| Progress | Compare bounded claims with current observations. | Plans, file changes, or repeated summaries establish behavior. | Distinguish reported progress from observed and accepted behavior. |
| Accomplishment support | Prose claim-to-observation assessment is plausible. | English alone prevents false assurance. | Require independently sourced observations and an insufficient-evidence result. |
| Completion | Assessment can identify unsupported claims and open obligations. | Model support establishes truth or permission to stop. | Retain existing authority, human acceptance, and lifecycle rules. |

## Approaches and recommendation

| Approach | Advantages | Costs and risks |
| --- | --- | --- |
| External approved requirements baseline, referenced by work records | Stable IDs and revisions; compact handoffs; portable across repositories and managers. | Requires baseline availability, reconciliation, and provenance handling. |
| Requirements embedded in handoff sections or metadata | Self-contained record. | Schema/contract change, duplicated inventory, budget pressure, and drift between records. |
| Selected conversation decisions as the primary assessment input | Captures decisions that never reached a spec. | Omission, privacy, ambiguity, and an unstable completion denominator. |

Recommend the external baseline. Selected conversation decisions can supplement
it. They should not become its primary source of truth. Preserve existing
requirements artifacts where practical rather than requiring a particular
framework or replacing a project's established specification format.

## Portable architecture and possible implementation homes

Separate four responsibilities:

1. **Requirements ownership:** approved feature statements, requirement
   baselines, decision provenance, revision history, and acceptance authority.
2. **Observation collection:** project/host adapters collect bounded facts,
   candidate identity, environment, coverage limits, and local evidence locators.
3. **Assessment:** deterministic preflight plus an optional semantic adapter
   evaluates English packets and emits per-obligation or per-claim findings.
4. **Coordination:** a manager or human uses findings to choose continued work,
   evidence gathering, reconciliation, or acceptance review within existing
   authorization. Assessment itself performs no lifecycle transition.

An agent manager is a plausible owner of the baseline, observation store, and
coordination views because these span agents, repositories, and sessions. The
toolkit could remain a consumer of compact references and relevant progress.
Alternatively, a standalone assessment library could serve both projects. No
placement is selected here; choose after inspecting the manager's current
interfaces, storage, and authorization model.

Use a neutral assessment packet independent of Claude Code, Codex, GitHub,
tracker APIs, and Jev. Provider integrations and host collectors sit outside
the toolkit's deterministic core. A useful requirements/evidence workflow must
remain usable with human review when no semantic provider is available.

## Requirements provenance and change rules

The authoritative feature statement is approved by the responsible human and
constrained by the locked scope outcome and settled decisions. Requirements
clarify it; they cannot silently weaken it. Checking coverage against only the
generated requirement list would conceal omissions in that list.

Each requirement has a stable ID, an English obligation, source references,
acceptance criteria, applicable domain, exclusions, and a verification method.
Design choices and implementation tasks are separate artifacts. Derived
requirements and suggested concerns are proposals until explicitly approved.

An approved baseline is an immutable revision with a content digest and an
approval reference. A digest establishes byte identity, not human approval or
semantic adequacy. Preserve prior revisions and record each approved change's
reason, authority, affected requirements, and evidence needing revalidation.
Never reuse an ID for a different obligation.

Track planned, in-progress, reported implemented, evidenced, accepted, and
deferred states separately from the toolkit's scope-status vocabulary. These
are proposed manager/assessment states, not new schema-v2 values. Deferral is
not fulfillment. A changed authorized root still needs the existing approved
transition or new authorization; baseline approval cannot bypass that process.

Maintain mappings in both directions: source obligation to requirement,
requirement to acceptance criterion and observation, and claim to supporting
or contradicting observations. Count coverage locally against the approved
source-obligation inventory, keeping unresolved mappings visible. Inventory
approval does not guarantee discovery of all unstated user intent.

## Four stages of assessment

| Stage | Question | Permitted evidence | Useful finding |
| --- | --- | --- | --- |
| Feature coverage | Does every explicit authorized obligation have adequate requirements, and do requirements introduce unapproved obligations? | Approved feature statement, decisions, baseline. | Covered, partial, omitted, ambiguous, or proposed addition. |
| Readiness | Are criteria observable, clear enough to act on, and paired with suitable verification? | Requirements, domains, acceptance examples, evidence plan. | Ready within the stated domain, or a named ambiguity/evidence-plan gap. |
| Progress | Do reported states agree with observations, and what remains? | Current baseline, claims, current observations, unknowns. | Supported progress, contradiction, missing proof, or known unfinished obligation. |
| Accomplishment support | Does independent evidence support each claimed accomplishment and required acceptance condition? | Final claims, applicable observations, unresolved obligations, acceptance decisions. | Evidence support for specific claims; remaining work or evidence needs. |

Assess individual obligations or claims with relevant context. Deterministic
preflight checks IDs, baseline/candidate identity, completeness of required
fields, provenance availability, and evidence freshness. Deterministic code
also owns dates, arithmetic, counts, and aggregation.

For factual claims use supported-by-supplied-evidence, contradicted, or
insufficient-evidence labels. Keep unavailable/not-assessed distinct for
runtime faults. Low confidence becomes insufficient evidence under frozen
rules; invalid responses remain unavailable. A missing observation is not a
product defect. A passing test receipt supports only its mapped acceptance
domain, not every requirement.

Contradictions recommend reconciliation. Missing or stale proof recommends
evidence gathering. Known unfinished obligations recommend continued work.
Universal evidence support recommends acceptance review, not automatic
completion. Code may be finished while review, UAT, rollout, or decision work
remains. Neither honest disclosure nor escalation fulfills those obligations.

## Independent observations and English-only boundary

Packets visibly distinguish author claims, direct reviewer observations,
machine receipts, and unknowns. Independence concerns origin and method: a
second agent restating the author's report is not an independent observation.
A reviewer records what was actually inspected, against which candidate, and
with what limits. Machine results are independent execution facts, but an
agent-authored test can still be incomplete or incorrect.

Keep underlying code and raw evidence local to authorized inspectors. Produce
English machine receipts through fixed extraction templates that preserve
result, candidate identity, environment, requirement mapping, and limitations.
Avoid generative summarization for exact facts. A local provenance record binds
the receipt to its underlying artifact or invocation; a human can inspect it
without sending that artifact to Jev. A handwritten report that tests passed
remains a claim unless its receipt is independently available.

Provider-visible content is plain English. IDs and sanitized file paths may
serve as references. No code snippets, diffs, ASTs, serialized program
structures, raw logs, or code-reading tasks enter the packet. A transport
envelope can carry IDs and English fields without embedding program structures.
Paths are references, not invitations for provider access.

Never transmit credentials, customer/provider data, tax-ID values, full prompts,
replies, or transcripts. Select only task-relevant conversation decisions,
sanitize them, preserve their approval/source provenance locally, and declare
omissions. If redaction removes necessary meaning, report insufficient evidence.
Treat all claims and observations as data rather than instructions to the judge.

### Hypothetical packet

> Requirement R-07 requires failed operations to preserve the existing record.
>
> Claim C-12 says this behavior is implemented.
>
> Observation O-31 records that an isolated acceptance check exercised a failed
> operation against candidate A and found the record unchanged.
>
> Limitation: concurrent operations were not tested.
>
> Question: Does O-31 support C-12 within the stated acceptance domain?

If R-07 includes concurrent operations, this packet is insufficient for the full
requirement even if it supports the narrower observed behavior. Evidence
selection must not silently narrow the obligation to match the available test.

## Toolkit and host integration boundary

If later adopted, place baseline references in Authoritative references,
settled decisions in User decisions, and compact progress deltas in Completed
work or Incomplete work and risks. Detailed mappings and findings belong in an
external assessment artifact. Do not repeat structured verification or scope
facts in narrative sections, add an eighth section, or exceed record budgets.
Initially, external references remain prose references under the current
contract; mechanically binding them would require a separate design change.

Claude Code and Codex collectors should produce equivalent neutral packets.
Test actual discovery and delivery separately on each host; command compatibility
does not establish interactive discovery. Released documentation still records
Codex advisory discovery as unverified; this proposal does not assert a new
host observation.

No provider call belongs in a hook or authorization path. Preserve fail-closed
explicit policy rejection at PreToolUse and Stop, advisory automatic runtime
faults, fail-closed explicit CLI validation, and UserPromptSubmit as a
non-decision event. Semantic findings never approve transitions, clear gates,
arm the correction circuit, or generate lifecycle authority. Assessment advice
does not alter the continuation/completion-audit distinction.

## Qualification plan for later approval

Qualification is staged and requires separate authorization to execute. Results
qualify only the particular model, protocol, extraction process, and stage
tested. Old mixed-evidence runs and opened diagnostic cases earn no new credit.

1. **Prepare the protocol without provider calls.** Define schemas and English
   templates, local provenance, labels, selection/privacy rules, model version,
   confidence policy, aggregation, deadlines, and bounded retry behavior. Review
   sample packets with humans before implementing a candidate.
2. **Develop on a separate set.** Use development cases to choose thresholds
   and wording. Freeze all settings before held-out labels or outcomes are
   opened. A model alias or protocol revision invalidates the qualified version.
3. **Create independent labels.** Separate corpus authors, candidate implementers,
   and blind adjudicators. Label both the underlying case outcome and what the
   supplied packet establishes. Missing evidence remains insufficient even if
   a case author knows the underlying implementation works. Human disagreement
   is adjudicated or retained as ambiguity, not replaced by model consensus.
4. **Run a sealed feasibility screen per stage.** Propose 40 fresh cases for
   each of four stages: 16 critical seeded problems, 16 legitimate clean cases,
   and eight missing-proof cases. Require all 16 critical problems detected,
   at least 15/16 clean cases recognized, all eight missing-proof cases
   insufficient, zero false support on non-supportable cases, at most one false
   problem finding among clean cases, and no unassessed cases. Findings must
   identify the affected obligation; unrelated criticism earns no detection
   credit. These are proposed fixed gates, not measured outcomes.
5. **Exercise important boundaries.** Include omissions, invented accomplishments,
   contradictory receipts, narrowed scope, stale candidates/baselines, honest
   partial work, legitimate completion, acceptance still pending, irrelevant
   context, approval impersonation, misleading tests, and self-justification.
   Split by feature family to avoid near-duplicate leakage. Evaluate extraction
   fidelity, sanitization, omission disclosure, option-order/paraphrase stability,
   and both-host equivalence separately.
6. **Measure usefulness and reliability.** Compare with deterministic traceability
   plus human review. Report false support, missed problems, useful coverage,
   abstention, reviewer effort, latency, and cost per stage. Assess malformed
   answers, timeouts, and service faults separately. Preserve every attempt and
   any fixed retry outcomes; do not rerun selectively to replace failures or
   treat unavailable as pass. Retain reduced metrics and hashes, not sensitive
   payloads or raw transcripts.
7. **Expand before adoption.** Small synthetic screens establish feasibility,
   not a deployment error rate. A proposed 1% false-support target would need
   roughly 300 independent representative unsupported cases with zero false
   supports to put the one-sided 95% binomial upper bound below 1%. That bound
   assumes representative independent sampling and does not resolve correlated
   cases, distribution shift, or production prevalence. Measure abstention and
   false problem findings as well.
8. **Consider a limited advisory pilot only after fresh approval.** Choose the
   host project, privacy/retention rules, cost budget, operator review, and exit
   criteria. A failed or revised candidate needs a fresh sealed qualification
   set. Any code-versus-prose diagnostic requires separate approval and cannot
   introduce code into the English-only candidate.

No proposed numerical gate above authorizes automatic completion or substitutes
for project acceptance. An evaluator that abstains on everything is safe from
some errors but fails the usefulness gates. If Jev fails, retain the requirements
and observations workflow with human review rather than weakening the gates.

## Decisions needed before further work

- Choose the implementation home after reviewing the manager project: manager
  subsystem, standalone component, or toolkit extension.
- Approve the baseline/change rules, packet boundary, assessment labels, and
  evidence-independence criteria in the hosting project's written specification.
- Authorize any implementation separately, including its tests and host adapters.
- Authorize any provider qualification separately, with frozen gates, independent
  labels, privacy rules, and a budget. Consumer adoption is a later decision.

Until those decisions are made, this remains a portable proposal. The toolkit's
released behavior and Jev hold remain unchanged.
