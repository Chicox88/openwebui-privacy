"""High-level, reusable entry point.

>>> from encompass_field_finder import FieldDependencyFinder, EncompassConfig
>>> finder = FieldDependencyFinder(config=EncompassConfig.from_env(), export_paths=["./exports"])
>>> result = finder.scan(["CX.OLD.FIELD", "4000"])
>>> for ref in result.references: print(ref.category, ref.item_name, ref.location)
>>> outcomes = finder.apply(result, action="replace", mapping={"CX.OLD.FIELD": "CX.NEW.FIELD"},
...                         dry_run=True)
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

from .catalog import load_catalog
from .client import EncompassClient, EncompassConfig
from .matcher import FieldMatcher, iter_unique, normalize_field_id
from .models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, Confidence, CoverageEntry, Reference, ScanResult,
)
from .sources import (
    ApplyContext, CustomFieldsSource, FieldDefinitionsSource, GenericSettingsSource, LoanUsageSource,
    OfflineExportSource, ScanContext, Source, TemplatesSource, WebInputFormsSource,
)

log = logging.getLogger(__name__)

# Areas with no public REST API. If nothing scanned them (no offline export covering the
# category), the report says so explicitly instead of implying "no references".
NO_API_AREAS: Dict[str, str] = {
    "Business Rule": "Field Data Entry, Field Triggers, Persona Access to Fields/Loan Actions, Automated "
                     "Conditions, Milestone Completion, Input Form List, Print Form rules, Loan Folder "
                     "rules... Export them from Encompass Settings (or dump them with an SDK utility) and "
                     "scan the export with --export.",
    "Dynamic Data Management": "DDM Field Rules, Fee Rules, Data Tables and field scenarios. Export from "
                               "Settings > Dynamic Data Management and scan with --export.",
    "Custom Input Form (desktop)": "Desktop custom input forms and their codebase. Export from Input Form "
                                   "Builder (Encompass package) and scan with --export. Web forms ARE "
                                   "covered through the API.",
    "Report": "Report templates (pipeline / loan / financial). Export the templates and scan with --export.",
    "Dashboard": "Dashboard templates and views. Export the templates and scan with --export.",
    "Alert": "Custom alerts and alert rules. Export from Settings and scan with --export.",
    "Plugin / Codebase": "Plugins, custom form codebase assemblies and SDK apps. Scan the source code or "
                         "the DLLs with --export.",
    "Print Forms": "Field mappings inside custom print forms (Word/PDF). The API lists the forms only; "
                   "scan the form files with --export.",
}


class FieldDependencyFinder:
    """Find (and optionally change) everything in Encompass that references given field ids."""

    def __init__(
        self,
        config: Optional[EncompassConfig] = None,
        client: Optional[EncompassClient] = None,
        export_paths: Optional[Sequence[str]] = None,
        catalog_path: Optional[str] = None,
        include_undocumented: bool = False,
        include_loan_usage: bool = False,
        include_templates: bool = True,
        only_sources: Optional[Iterable[str]] = None,
        skip_sources: Optional[Iterable[str]] = None,
        extra_sources: Optional[Sequence[Source]] = None,
        min_confidence: Union[str, Confidence] = Confidence.MEDIUM,
        workers: int = 4,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        if client is None and config is not None and config.is_configured():
            client = EncompassClient(config)
        self.client = client
        self.export_paths = list(export_paths or [])
        self.min_confidence = Confidence(min_confidence)
        self.workers = workers
        self.progress = progress or (lambda m: log.info(m))

        sources: List[Source] = []
        if self.client is not None:
            sources += [FieldDefinitionsSource(), CustomFieldsSource(), WebInputFormsSource()]
            sources += [GenericSettingsSource(e) for e in load_catalog(catalog_path, include_undocumented)]
            if include_templates:
                sources.append(TemplatesSource())
            if include_loan_usage:
                sources.append(LoanUsageSource())
        if self.export_paths:
            sources.append(OfflineExportSource(self.export_paths))
        sources += list(extra_sources or [])

        only = {s.lower() for s in only_sources} if only_sources else None
        skip = {s.lower() for s in (skip_sources or [])}

        def keep(s: Source) -> bool:
            k = s.key.lower()
            short = k.split(":", 1)[-1]
            if s.key == FieldDefinitionsSource.key:      # always useful (aliases, RDB)
                return not ({k, short} & skip)
            if only is not None and not ({k, short} & only):
                return False
            return not ({k, short} & skip)

        self.sources = [s for s in sources if keep(s)]

    # ------------------------------------------------------------------ scanning
    def scan(self, field_ids: Sequence[str]) -> ScanResult:
        ids = list(iter_unique(normalize_field_id(f) for f in field_ids if normalize_field_id(f)))
        if not ids:
            raise ValueError("no field ids given")
        matcher = FieldMatcher(ids, self.min_confidence)
        result = ScanResult(field_ids=ids, started_at=_now())
        ctx = ScanContext(ids, matcher, self.client, result.field_info, self.workers, self.progress)
        for src in self.sources:
            if src.requires_client and self.client is None:
                result.coverage.append(CoverageEntry(src.key, src.category, "skipped", 0, "no API credentials"))
                continue
            self.progress(f"scanning {src.key} ...")
            try:
                sr = src.scan(ctx)
            except Exception as err:  # noqa: BLE001 - one broken source must not kill the scan
                log.exception("source %s failed", src.key)
                result.coverage.append(CoverageEntry(src.key, src.category, "error", 0, str(err)))
                continue
            result.references.extend(sr.references)
            result.coverage.extend(sr.coverage)
            if sr.references:
                self.progress(f"  {src.key}: {len(sr.references)} reference(s)")
        if self.client is None:
            result.coverage.append(CoverageEntry(
                "rest:*", "Encompass API", "skipped", 0,
                "No API credentials supplied - only offline exports were scanned."))
        self._add_gap_notes(result)
        result.references = _dedupe(result.references)
        result.references.sort(key=lambda r: (r.field_id.lower(), r.category, r.item_name.lower(),
                                              -r.confidence.rank, r.json_path or "", r.line or 0))
        result.finished_at = _now()
        return result

    def _add_gap_notes(self, result: ScanResult) -> None:
        covered = {c.category for c in result.coverage if c.source.startswith("offline") and c.status == "ok"}
        for area, how in NO_API_AREAS.items():
            if area not in covered:
                result.coverage.append(CoverageEntry("no-public-api", area, "manual_only", 0, how))

    # ------------------------------------------------------------------ changes
    def plan(
        self,
        result: Union[ScanResult, Sequence[Reference]],
        action: str,
        mapping: Optional[Dict[str, str]] = None,
        new_field_id: Optional[str] = None,
        ref_ids: Optional[Iterable[str]] = None,
        include_low: bool = False,
    ) -> List[ChangeRequest]:
        """Turn scan results into change requests (``action`` = ``replace`` | ``remove``)."""
        if action not in ("replace", "remove"):
            raise ValueError("action must be 'replace' or 'remove'")
        refs = result.references if isinstance(result, ScanResult) else list(result)
        wanted = set(ref_ids) if ref_ids else None
        lmap = {k.lower(): v for k, v in (mapping or {}).items()}
        out = []
        for r in refs:
            if wanted is not None and r.ref_id not in wanted:
                continue
            if r.change_support == ChangeSupport.NONE:
                continue
            if r.confidence == Confidence.LOW and not include_low and wanted is None:
                continue
            new = None
            if action == "replace":
                new = lmap.get(r.field_id.lower(), new_field_id)
                if not new:
                    continue
            out.append(ChangeRequest(r, action, new))
        return out

    def apply(
        self,
        result: Union[ScanResult, Sequence[Reference]],
        action: str,
        mapping: Optional[Dict[str, str]] = None,
        new_field_id: Optional[str] = None,
        ref_ids: Optional[Iterable[str]] = None,
        dry_run: bool = True,
        backup_dir: str = "efinder-backups",
        output_dir: str = "efinder-modified-exports",
        include_low: bool = False,
    ) -> List[ChangeOutcome]:
        """Replace or remove references. ``dry_run=True`` (default) changes nothing."""
        changes = self.plan(result, action, mapping, new_field_id, ref_ids, include_low)
        field_ids = sorted({c.ref.field_id for c in changes} | {c.new_field_id for c in changes if c.new_field_id})
        matcher = FieldMatcher(field_ids or ["__none__"], self.min_confidence)
        actx = ApplyContext(matcher, self.client, dry_run, backup_dir, output_dir, self.progress)
        by_source: Dict[str, List[ChangeRequest]] = defaultdict(list)
        for c in changes:
            by_source[c.ref.source].append(c)
        outcomes: List[ChangeOutcome] = []
        for key, group in by_source.items():
            src = self._source_for(key)
            if src is None:
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "skipped",
                                           f"scanner {key} is not active in this run") for c in group]
                continue
            if src.requires_client and self.client is None:
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "skipped",
                                           "API credentials required") for c in group]
                continue
            self.progress(f"{'planning' if dry_run else 'applying'} {len(group)} change(s) via {key}")
            outcomes += src.apply_batch(actx, group)
        return outcomes

    def _source_for(self, key: str) -> Optional[Source]:
        for s in self.sources:
            if s.key == key or key.startswith(s.key + ":"):
                return s
        return None


def _dedupe(refs: List[Reference]) -> List[Reference]:
    seen, out = set(), []
    for r in refs:
        if r.ref_id in seen:
            continue
        seen.add(r.ref_id)
        out.append(r)
    return out


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
