"""Encompass Field Dependency Finder.

Find everything in an Encompass instance that references one or more field ids (custom field
calculations, web input forms, conditions, eFolder setup, templates, CDOs, webhooks, RDB,
plus offline exports of business rules, DDM, desktop forms, reports, dashboards, codebase ...),
show where to find each reference, and replace or remove the reference where possible.
"""

from .client import ApiError, EncompassClient, EncompassConfig
from .finder import FieldDependencyFinder
from .matcher import FieldMatcher
from .models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, Confidence, CoverageEntry, MatchKind, Reference,
    ScanResult,
)

__version__ = "0.1.0"


def find_references(field_ids, config=None, export_paths=None, **kwargs) -> ScanResult:
    """One-call convenience wrapper around :class:`FieldDependencyFinder`."""
    if isinstance(field_ids, str):
        field_ids = [f for f in field_ids.replace(";", ",").split(",")]
    finder = FieldDependencyFinder(config=config, export_paths=export_paths, **kwargs)
    return finder.scan(field_ids)


__all__ = [
    "ApiError", "EncompassClient", "EncompassConfig", "FieldDependencyFinder", "FieldMatcher",
    "ChangeOutcome", "ChangeRequest", "ChangeSupport", "Confidence", "CoverageEntry", "MatchKind",
    "Reference", "ScanResult", "find_references", "__version__",
]
