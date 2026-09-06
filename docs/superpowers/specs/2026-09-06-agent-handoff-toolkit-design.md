# Agent Handoff Toolkit Design

## Goal

Create one public, versioned source for session continuations, completion audits, context-health reminders, and advisory authoring hooks that works with Claude Code and Codex.

## Architecture

The core is a Python 3.11+ standard-library package. It parses a small JSON metadata block embedded in Markdown, validates the metadata and exact section set, renders deterministic records from JSON, renders the required final response tail, and evaluates context milestones. Host adapters normalize Claude Code and Codex hook payloads into the core; they do not contain policy.

The source distribution includes one canonical skill plus platform hook fragments. Consumer installation and sync are a separate increment because safe merging requires provenance, dry-run output, and local-modification detection.

## Data model

There are two record types: `continuation` and `completion-audit`. Both include schema version, timestamp, active scopes, verification entries, and narrative sections. Continuations add empty next-session gates, exact-action fields, and a self-contained prompt. Audits add authorization basis and exclude all continuation fields.

## Failure behavior

Explicit validation and rendering fail closed with actionable diagnostics. Automatic hooks catch malformed input, missing files, and unexpected errors and exit successfully without output. This preserves the host session while keeping deliberate checks strict.

## Security and privacy

The toolkit makes no network calls and stores no prompts, replies, transcripts, credentials, or customer data. Context state uses a hash of the host session identifier and atomic writes in an untracked state directory. Hook payload parsing is bounded, and patch parsing reads file directives rather than arbitrary command content.

## Non-goals

V1 does not mutate consumer repositories, call GitHub or tracker APIs, score semantic quality with an LLM, rewrite historical handoffs, or promise exact automatic context percentages on hosts that do not expose supported telemetry.
