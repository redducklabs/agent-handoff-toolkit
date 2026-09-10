"""Public API for agent handoff records."""

__version__ = "0.2.8"

from .records import (
    ValidationIssue,
    parse_markdown,
    render_record,
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
    "render_record",
    "render_tail",
    "render_terminal_response",
    "scope_definition_digest",
    "validate_successor",
    "validate_markdown",
]
