"""Public API for agent handoff records."""

__version__ = "0.2.6"

from .records import (
    ValidationIssue,
    parse_markdown,
    render_record,
    render_tail,
    validate_markdown,
)

__all__ = [
    "ValidationIssue",
    "__version__",
    "parse_markdown",
    "render_record",
    "render_tail",
    "validate_markdown",
]
