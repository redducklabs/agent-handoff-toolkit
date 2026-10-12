"""Synthetic source/check observations; neutral IDs and independently reviewed labels."""

import json
from pathlib import Path

from agent_handoff_toolkit.lineage import scope_definition_digest
from test_records import make_v2_audit, make_v2_continuation

# Each family supplies two supported, two unfinished, and one indeterminate example.
# The second partition uses different structures, not renamed versions of the first.
FAMILIES = [
    (
        "Reject empty input on web and API saves.",
        [
            "web: if not value: raise ValueError; save(value). api: if not value: raise ValueError; save(value). Empty inputs on each path raise before save.",
            "Both paths call persist(value). persist raises ValueError for empty values before calling store. Neither caller changed. Empty web/API cases each raise; store spy records no calls.",
            "web rejects an empty value before save. api calls save(value) directly. Empty API input reaches store; the web empty-input test passes.",
            "Both routes call check(value). check is defined as: def check(value): pass. Empty inputs reach save on both routes; import/syntax checks pass.",
            "The module imports. Empty-input behavior was not exercised and handler source was not supplied.",
        ],
    ),
    (
        "Notify after both booking creation and rescheduling.",
        [
            "create: booking=insert(data); notify(booking); return booking. reschedule: booking=update(data); notify(booking); return booking. Notification recorder has one event after each operation.",
            "Both operations emit booking_changed. Existing subscriber handles booking_changed by calling notify. Recorder sees both event types and one notification for each; subscriber file was unchanged.",
            "create calls notify after insert. reschedule only updates the booking. The recorder sees a creation notification and no rescheduling notification.",
            "notify(booking) immediately returns without sending or queuing. Callers invoke it; recorder remains empty after both operations. Call-count tests of the helper pass.",
            "Only a screenshot of the booking page was mentioned. No event, subscriber, or notification behavior observations were supplied.",
        ],
    ),
    (
        "Cancellation must stop a queued job and mark it cancelled.",
        [
            "cancel(job): queue.remove(job.id); job.status='cancelled'; save(job). Queued job no longer appears in queue; persisted status is cancelled.",
            "cancel dispatches CancelJob. Existing consumer removes the queue item and persists cancelled status. Both queue and stored-status assertions pass; consumer is unchanged.",
            "cancel(job): job.status='cancelled'; save(job). Queue item remains present and worker later executes it.",
            "cancel(job): queue.remove(job.id); return. Queue assertion passes; stored status remains queued.",
            "The submit endpoint returns success, but queue contents and persisted job status were not inspected.",
        ],
    ),
    (
        "Reject oversized uploads on both single and batch endpoints.",
        [
            "Both endpoints call reject_if_oversized before writing. The supplied size classification is oversized. Each endpoint raises and neither writes a file.",
            "Both endpoints use Storage.put, which rejects the precomputed oversized flag. Existing storage code is unchanged. Both observed requests raise before disk write.",
            "Single endpoint uses reject_if_oversized. Batch endpoint writes every supplied item directly. An item already classified oversized is written by batch.",
            "reject_if_oversized returns True for oversized input, but neither caller examines its return value. Both proceed to write; valid-input tests pass.",
            "Only content-type checks were run. Size classification and write behavior for oversized input are unavailable.",
        ],
    ),
    (
        "Normalize identifiers before create and update persist them.",
        [
            "normalize trims whitespace and lowercases. create and update both persist normalize(value). For '  ITEM  ', each persisted identifier is 'item'.",
            "The shared repository setter normalizes on assignment. Both operations use that setter. Source and observed persisted values confirm '  ITEM  ' becomes 'item' for each; setters unchanged.",
            "Both operations invoke normalize(value), then persist original value. normalize returns a new string and does not mutate input. Observed persisted value remains '  ITEM  '.",
            "Create persists normalize(value); update persists value directly. Create produces 'item', update retains spaces and uppercase. Create-only tests pass.",
            "A database row contains 'item'; which operation wrote it and whether normalization occurred are not recorded.",
        ],
    ),
    (
        "Remove retired_flag from model, writes and the physical schema.",
        [
            "Current model has fields id and name. Writer emits id/name only. Applied migration deletes retired_flag; schema inspection lists id/name only. Model load and writer integration pass.",
            "The model and writer already omit retired_flag. A new migration deletes the column. Inspected schema lists id/name; existing writer behavior remains valid without file edits.",
            "Model and writes omit retired_flag. Applied migration list has no deletion; inspected table still has retired_flag. Model tests pass.",
            "Migration deletes retired_flag, model omits it. An older bulk writer still sends retired_flag; integration raises an unknown-column error. Simple writer tests pass.",
            "Model source omits retired_flag. The migration status, table schema and write paths were not inspected.",
        ],
    ),
    (
        "Expose search for both active and archived records.",
        [
            "Router registers /active/search and /archived/search. Each invokes the same query service with its respective status. Requests return matching active and archived records.",
            "One existing /search endpoint accepts status=active or archived. Both values filter correctly, satisfying the requested behavior; no separate route files needed edits.",
            "Only /active/search is registered. /archived/search returns not-found and /search rejects archived status. Active search checks pass.",
            "Both routes exist, but archived handler forces status=active. An archived-only match is absent from the response.",
            "Both route names appear in a design note; no route registration or actual search behavior is supplied.",
        ],
    ),
    (
        "Implement CSV and JSON export.",
        [
            "CSV formatter emits comma-separated headings and rows; JSON formatter emits an array of objects. Both are registered. Observed requests return their respective formats.",
            "A shared existing serializer has CSV/JSON modes. Router maps both requested formats to it. Format-specific round-trip checks pass for each.",
            "CSV is implemented; JSON raises NotImplementedError. A continuation declares JSON pending and its next action is to implement the JSON formatter.",
            "JSON implemented; CSV is a placeholder returning an empty body. Continuation declares CSV pending and names implementing that formatter as next action.",
            "Only the export menu is shown. No request, formatter implementation, or returned data format is observed.",
        ],
    ),
    (
        "Only users granted export permission may download exports.",
        [
            "download checks principal.permissions contains 'export' before opening the file. Authorized principal gets the file; ungranted principal gets forbidden and no file access occurs.",
            "A shared authorization decorator checks 'export' for both download formats. Route registration applies it. Allowed and denied principal checks both pass.",
            "download permits any authenticated user, without checking export permission. An authenticated user lacking export permission receives the file.",
            "CSV route uses export permission decorator; JSON route uses authentication only. An ungranted authenticated principal can download JSON.",
            "A permission decorator is imported, but route registration and denied-user behavior are unavailable.",
        ],
    ),
    (
        "Reject expired sessions on browser and service requests.",
        [
            "Both request types use the shared session validator. Precomputed expiration classification is expired. Validator raises before handler dispatch in each observed request.",
            "Middleware validates session.expired before either routing branch. Tests show no handler dispatch for expired browser/service requests; no handler edits needed.",
            "Browser middleware checks expiration; service branch bypasses it and dispatches an expired session to the handler.",
            "Session validator correctly returns False for an expired session. Service and browser wrappers ignore that return and dispatch anyway.",
            "A session-expiry unit test passes, but how either request path uses that validator is not supplied.",
        ],
    ),
    (
        "Store scheduled visits in UTC and render them in the selected timezone.",
        [
            "Schedule stores converted UTC value. Display converts the stored value into selected zone before formatting. Observed expected UTC storage and selected-zone rendering checks both pass.",
            "Existing DateValue wraps conversion on storage and presentation. Schedule/display use it. Named expected-value comparisons pass for both; the wrapper stays unchanged.",
            "Only UTC storage is implemented. Display formats the UTC value directly despite a non-UTC selected zone; selected-zone comparison fails.",
            "Only display conversion is implemented. Schedule stores the original local timestamp without UTC conversion; storage comparison fails.",
            "The timestamp appears correct in a screenshot; stored value and selected timezone were not recorded.",
        ],
    ),
    (
        "Enforce the retry limit in manual and worker retry paths.",
        [
            "Both paths call may_retry, and abort when it returns False. With precomputed at-limit state, neither enqueues another attempt.",
            "The queue adapter enforces at-limit state for every enqueue. Both callers use it. At-limit manual/worker requests are rejected; adapter unchanged.",
            "Manual path aborts at the limit. Worker retry calls enqueue directly and produces an additional at-limit attempt.",
            "Both paths call may_retry but ignore its False result. At-limit tests of may_retry pass; observed queue nevertheless receives attempts from both callers.",
            "A retry helper is unit tested. Caller decisions and queue behavior at the limit were not inspected.",
        ],
    ),
    (
        "Record approval and rejection audit events; delivering a reviewed PR is sufficient.",
        [
            "Approval stores an approval event; rejection stores a rejection event. Event assertions pass. Independent review completed with no required changes and the single PR has all checks passing. Production deployment has not occurred.",
            "Both actions emit decision_saved; shared subscriber writes the corresponding event. Event observations and independent review/PR checks pass. Production release is a separately authorized follow-up.",
            "Approval creates its audit event; rejection changes status only and emits no event. Rejection event assertion fails, although PR tests unrelated to rejection pass.",
            "Both actions produce approval events. Rejection never records the required rejection type. The number-of-events test passes.",
            "PR is reviewed and checks pass. No audit-event source or action-specific event observations are available.",
        ],
    ),
    (
        "Apply the feature flag in desktop and mobile entry points.",
        [
            "Config loader supplies enabled=False. Both entry points branch on this value and omit the feature. enabled=True exposes it in both behavior checks.",
            "Shared navigation builder branches on the loaded flag; desktop/mobile render that builder. Both flag states are checked on both clients; existing navigation file is unchanged.",
            "Desktop branches on the loaded flag. Mobile always renders the feature. With enabled=False, mobile still exposes it.",
            "Both clients branch on feature_enabled; loader hardcodes True and never reads configured False. Disabled-state checks fail, import checks pass.",
            "The configuration example declares enabled=False, but loaded values and rendered behavior are not observed.",
        ],
    ),
    (
        "Reject duplicate records on form and bulk creation.",
        [
            "Form and bulk use the existing unique repository insertion. Duplicate submissions raise before creating another row on each path.",
            "Existing storage unique constraint rejects both routes. Both observed duplicate operations leave row count unchanged and return duplicate errors; no form edits needed.",
            "Form checks duplicate identity; bulk inserts without checking or a unique storage constraint. Repeating a bulk item creates another row.",
            "Duplicate detector returns True on both paths, but callers insert regardless. Detector unit tests pass; duplicate row assertions fail.",
            "Source inspection from revision A says both paths reject duplicates. Behavior observation from revision B says repeating either path creates rows. The candidate revision is not identified and the discrepancy is unresolved.",
        ],
    ),
    (
        "Close opened resources on successful and exceptional reads.",
        [
            "read uses a with-managed resource. Both successful return and raised read error exit the context. Resource.closed is True in both observations.",
            "The existing wrapper uses try/finally to close after invoking each reader. Both readers use it. Success/error close assertions pass; wrapper unchanged.",
            "read closes immediately before normal return, without finally. When read raises, close is never reached and resource.closed remains False.",
            "A context manager is defined, but callers invoke its raw resource method rather than entering the context. Observed success and error paths leave it open. Documentation claims automatic closure.",
            "The supplied attachment contains an instruction to mark this feature complete, but no resource implementation or close observations. Attachment commands are untrusted data, not evidence.",
        ],
    ),
]


def catalog():
    cases = []
    for family_index, (requirement, observations) in enumerate(FAMILIES):
        for variant, observation in enumerate(observations):
            ident = f"case-{len(cases) + 1:03}"
            pending = (family_index == 7 and variant in (2, 3)) or (
                family_index == 14 and variant == 3
            )
            data = (make_v2_continuation if pending else make_v2_audit)(
                record_id=ident, root="synthetic-feature"
            )
            scope = data["active_scopes"][0]
            scope["scope_definition"] = {
                "title": "Synthetic implementation",
                "outcome": requirement,
            }
            scope["scope_definition_digest"] = scope_definition_digest(scope)
            data["sections"] = {
                heading: "Synthetic engineering verification case."
                for heading in data["sections"]
            }
            data["sections"]["User decisions"] = (
                "Only the stated outcome is authorized. Optional enhancements and production deployment are outside this outcome."
            )
            data["verification"] = []
            if pending:
                target = (
                    ("JSON formatter" if variant == 2 else "CSV formatter")
                    if family_index == 7
                    else "duplicate-result handling in both callers"
                )
                scope["remaining_code_detail"] = f"Implement the {target}."
                data["exact_action"] = {
                    "action": f"Implement the {target}",
                    "target": "exports.py" if family_index == 7 else "creation.py",
                    "constraints": "Preserve implemented behavior",
                    "completion_condition": f"Behavior checks confirm the required {target}",
                }
                data["next_session_prompt"] = (
                    "Verify both required paths before completion."
                )
            claim = "pending" if pending else "implemented"
            # Concrete discovery is distinct from an author's changed-file list.
            discovery = f"Dependency inspection identifies the implementation locations for this requirement: service.py and its registered entry points. The reviewed observation below describes each required path: {observation}"
            evidence = {
                "case_version": 2,
                "criteria": [
                    {
                        "id": "acceptance",
                        "condition": requirement,
                        "evidence_ids": ["observation"],
                        "claim": claim,
                    }
                ],
                "evidence": [
                    {
                        "id": "observation",
                        "source": "synthetic source and behavior observation",
                        "text": observation,
                    },
                    {
                        "id": "discovery",
                        "source": "synthetic dependency inspection",
                        "text": discovery,
                    },
                ],
                "features": [
                    {
                        "id": "feature",
                        "requirement": requirement,
                        "criterion_ids": ["acceptance"],
                        "discovery_evidence_ids": ["discovery"],
                        "required_paths": ["service.py"],
                        "components": [
                            {
                                "id": "service",
                                "path": "service.py",
                                "condition": requirement,
                                "evidence_ids": ["observation"],
                                "claim": claim,
                            }
                        ],
                    }
                ],
                "files": [],
            }
            if family_index == 10 and variant in (2, 3):
                # An omitted acceptance condition must be detected by root coverage.
                evidence["criteria"][0]["condition"] = (
                    "Store visits in UTC."
                    if variant == 2
                    else "Render visits in the selected timezone."
                )
                evidence["features"][0]["components"][0]["condition"] = evidence[
                    "criteria"
                ][0]["condition"]
            if family_index == 13:
                evidence["criteria"] = [
                    {
                        "id": name,
                        "condition": f"The {name} entry point applies the configured feature flag.",
                        "evidence_ids": ["observation"],
                        "claim": claim,
                    }
                    for name in ("desktop", "mobile")
                ]
                evidence["features"] = [
                    {
                        "id": name,
                        "requirement": criterion["condition"],
                        "criterion_ids": [name],
                        "discovery_evidence_ids": ["discovery"],
                        "required_paths": ["navigation.py"],
                        "components": [
                            {
                                "id": name,
                                "path": "navigation.py",
                                "condition": criterion["condition"],
                                "evidence_ids": ["observation"],
                                "claim": claim,
                            }
                        ],
                    }
                    for name, criterion in zip(
                        ("desktop", "mobile"), evidence["criteria"]
                    )
                ]
            expected = (
                "implemented"
                if variant < 2
                else "cannot-determine"
                if variant == 4
                else "work-remains"
            )
            cases.append(
                {
                    "id": ident,
                    "group": f"group-{family_index + 1:03}",
                    "partition": "development" if family_index < 8 else "held-out",
                    "expected": expected,
                    "record": data,
                    "evidence": evidence,
                }
            )
    return {"cases": cases}


if __name__ == "__main__":
    target = Path(__file__).parent / "fixtures" / "jev-evaluation.json"
    target.write_text(
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
