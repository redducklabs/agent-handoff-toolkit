"""Public API for agent handoff records."""

__version__ = "1.0.0"

from .records import (
    ValidationIssue,
    parse_markdown,
    render_progress_response,
    render_record,
    render_successor,
    render_tail,
    render_terminal_response,
    validate_successor,
    validate_markdown,
)
from .lineage import record_digest, scope_definition_digest

__all__ = [
    "ValidationIssue",
    "__version__",
    "parse_markdown",
    "record_digest",
    "render_progress_response",
    "render_record",
    "render_successor",
    "render_tail",
    "render_terminal_response",
    "scope_definition_digest",
    "validate_successor",
    "validate_markdown",
]
