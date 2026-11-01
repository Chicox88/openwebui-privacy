"""Scanner plug-in interface.

To add a new place to look, subclass :class:`Source`, implement :meth:`scan`, and (optionally)
:meth:`apply_batch` to support replace/remove. Register it with
``FieldDependencyFinder(extra_sources=[MySource()])``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..client import ApiError, EncompassClient
from ..matcher import FieldMatcher
from ..models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, Confidence, CoverageEntry, Reference,
)

log = logging.getLogger(__name__)


@dataclass
class ScanContext:
    field_ids: List[str]
    matcher: FieldMatcher
    client: Optional[EncompassClient] = None
    field_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    workers: int = 4
    progress: Callable[[str], None] = lambda msg: None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ApplyContext:
    matcher: FieldMatcher
    client: Optional[EncompassClient] = None
    dry_run: bool = True
    backup_dir: str = "efinder-backups"
    output_dir: str = "efinder-modified-exports"
    progress: Callable[[str], None] = lambda msg: None


@dataclass
class SourceResult:
    references: List[Reference] = field(default_factory=list)
    coverage: List[CoverageEntry] = field(default_factory=list)


class Source:
    key: str = "base"
    category: str = "Unknown"
    description: str = ""
    requires_client: bool = True

    def scan(self, ctx: ScanContext) -> SourceResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def apply_batch(self, ctx: ApplyContext, changes: List[ChangeRequest]) -> List[ChangeOutcome]:
        """Default: nothing can be changed automatically - return manual instructions."""
        return [manual_outcome(c) for c in changes]


# ---------------------------------------------------------------------- helpers
def manual_outcome(c: ChangeRequest, why: str = "") -> ChangeOutcome:
    what = (f"replace {c.ref.field_id} with {c.new_field_id}" if c.action == "replace"
            else f"remove {c.ref.field_id}")
    msg = why or c.ref.change_notes or "not changeable through the API"
    return ChangeOutcome(
        c.ref.ref_id, c.ref.field_id, c.action, "manual",
        f"{msg}. To do by hand: open {c.ref.location} and {what}.",
    )


def coverage_from_error(source: str, category: str, err: Exception, what: str) -> CoverageEntry:
    if isinstance(err, ApiError):
        if err.status == 404:
            return CoverageEntry(source, category, "not_available",
                                 note=f"{what}: endpoint not available on this instance/API version (404)")
        if err.status in (401, 403):
            return CoverageEntry(source, category, "forbidden",
                                 note=f"{what}: the API user lacks persona rights (HTTP {err.status})")
        return CoverageEntry(source, category, "error", note=f"{what}: HTTP {err.status} {err.body[:200]}")
    return CoverageEntry(source, category, "error", note=f"{what}: {err}")


def extract_list(body: Any, items_key: Optional[str] = None) -> List[Any]:
    """Find the list of items in an API response of unknown shape."""
    if body is None:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if items_key and isinstance(body.get(items_key), list):
            return body[items_key]
        for k in ("items", "contents", "results", "data", "value", "records", "templates"):
            if isinstance(body.get(k), list):
                return body[k]
        return [body]
    return [body]


def first_key(obj: Any, keys: Iterable[str], default: str = "") -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return default
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            if isinstance(v, dict):
                v = v.get("entityName") or v.get("name") or v.get("entityId") or json.dumps(v)
            return str(v)
    return default


def fill(template: str, item: Any, **extra: str) -> str:
    """Fill ``{key}`` placeholders from an item dict (and ``extra``)."""
    values = dict(item) if isinstance(item, dict) else {}
    values.update(extra)

    def rep(m: "re.Match[str]") -> str:
        v = values.get(m.group(1), "")
        return "" if v is None else str(v)

    return re.sub(r"\{([A-Za-z0-9_]+)\}", rep, template)


def write_backup(backup_dir: str, name: str, payload: Any) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]
    path = os.path.join(backup_dir, f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


def downgrade_low(refs: List[Reference]) -> List[Reference]:
    for r in refs:
        if r.confidence == Confidence.LOW and r.change_support != ChangeSupport.NONE:
            r.change_support = ChangeSupport.MANUAL
            r.change_notes = (r.change_notes + " " if r.change_notes else "") + \
                "Low-confidence hit - verify before changing."
    return refs
