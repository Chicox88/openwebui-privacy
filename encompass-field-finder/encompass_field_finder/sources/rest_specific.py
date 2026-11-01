"""REST scanners that need custom logic (beyond the generic catalog)."""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from ..client import ApiError
from ..models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, Confidence, CoverageEntry, MatchKind, Reference,
)
from ..walker import apply_edit, walk
from .base import (
    ApplyContext, ScanContext, Source, SourceResult, coverage_from_error, downgrade_low,
    extract_list, manual_outcome, write_backup,
)

log = logging.getLogger(__name__)


def _is_custom(fid: str) -> bool:
    """Loan custom fields (CX.*). Legacy CUSTnnFV fields live in the standard schema."""
    return fid.upper().startswith("CX.")


# ====================================================================== field definitions / RDB
class FieldDefinitionsSource(Source):
    """Looks the fields up (standard/custom schema + pipeline canonical names).

    Runs first: it records field metadata in ``ctx.field_info`` and teaches the matcher each
    field's loan contract path, so templates and webhook filters that use contract paths are
    recognised too.
    """

    key = "rest:field_definitions"
    category = "Field Definition"

    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        plain = [f for f in ctx.field_ids if not f.lower().startswith("re:")]
        std = [f for f in plain if not _is_custom(f)]
        cus = [f for f in plain if _is_custom(f)]

        def record(fid: str, info: Dict[str, Any], origin: str) -> None:
            slot = ctx.field_info.setdefault(fid, {})
            slot.update({k: v for k, v in info.items() if v not in (None, "")})
            slot.setdefault("origins", []).append(origin)
            for k in ("contractPath", "jsonPath", "modelPath"):
                if info.get(k):
                    ctx.matcher.add_alias(fid, info[k])

        calls = [
            ("standardFields", "/encompass/v3/schemas/loan/standardFields", std, "ids"),
            ("customFields", "/encompass/v3/settings/loan/customFields", cus, "ids"),
            ("pipelineCanonicalFields", "/encompass/v3/loanPipeline/canonicalFields", plain, "fieldIds"),
        ]
        for label, path, ids, param in calls:
            if not ids:
                continue
            try:
                body = ctx.client.get(path, {param: ",".join(ids)})
            except Exception as err:  # noqa: BLE001
                res.coverage.append(coverage_from_error(self.key, self.category, err, label))
                continue
            found = 0
            for row in extract_list(body):
                if not isinstance(row, dict):
                    continue
                fid = ctx.matcher.exact(str(row.get("fieldId") or row.get("id") or ""))
                if not fid:
                    continue
                found += 1
                record(fid, row, label)
                if label == "pipelineCanonicalFields":
                    res.references.append(Reference(
                        field_id=fid, category="Reporting Database", item_type="Pipeline / RDB field",
                        item_name=row.get("canonicalName", f"Fields.{fid}"),
                        location="Encompass > Settings > Company/User Setup > Reporting Database "
                                 "(also usable in Pipeline views, reports and dashboards)",
                        source=self.key, match_kind=MatchKind.DEFINITION, confidence=Confidence.HIGH,
                        snippet=f"{row.get('canonicalName')} - {row.get('description', '')}"
                                + (" (audited)" if row.get("isAudited") else ""),
                        api=f"GET {path}?{param}={fid}",
                        change_support=ChangeSupport.MANUAL,
                        change_notes="Add the new field to / remove the old field from the Reporting "
                                     "Database in Settings; existing loans need an RDB update to backfill.",
                    ))
                elif label == "customFields":
                    res.references.append(Reference(
                        field_id=fid, category="Field Definition", item_type="Loan Custom Field",
                        item_name=fid,
                        location=f"Encompass > Settings > Loan Setup > Custom Fields > Loan Custom Fields > {fid}",
                        source=self.key, match_kind=MatchKind.DEFINITION, confidence=Confidence.HIGH,
                        snippet=f"{row.get('description', '')} [{row.get('format', '')}]"
                                + (" calculated" if row.get("isCalculatedField") else ""),
                        api=f"GET {path}?ids={fid}",
                        change_support=ChangeSupport.NONE,
                        change_notes="Custom field ids cannot be renamed; create the new field, migrate "
                                     "references with this tool, then retire the old one.",
                    ))
            res.coverage.append(CoverageEntry(self.key, self.category, "ok", found, label))
        for fid in plain:
            if fid not in ctx.field_info:
                ctx.field_info[fid] = {"warning": "field not found by schema/custom field/pipeline lookups"}
        return res


# ====================================================================== custom field calculations
_CF_READ_ONLY = ("contractPath", "jsonPath", "modelPath", "createdBy", "createdDate",
                 "modifiedBy", "modifiedDate", "isCalculatedField", "type")


class CustomFieldsSource(Source):
    """Custom field calculations, AUDIT custom fields and anything else in custom field setup."""

    key = "rest:custom_fields"
    category = "Custom Field Calculation"
    LIST_V3 = "/encompass/v3/settings/loan/customFields"
    LIST_V1 = "/encompass/v1/settings/loan/customFields"

    def _all(self, client) -> Tuple[List[Dict[str, Any]], str]:
        try:
            return extract_list(client.get(self.LIST_V3)), self.LIST_V3
        except ApiError as err:
            if err.status not in (404, 405):
                raise
        return extract_list(client.get(self.LIST_V1)), self.LIST_V1

    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        try:
            fields, path = self._all(ctx.client)
        except Exception as err:  # noqa: BLE001
            res.coverage.append(coverage_from_error(self.key, self.category, err, "custom fields"))
            return res
        calc_hidden = 0
        for cf in fields:
            if not isinstance(cf, dict):
                continue
            cid = str(cf.get("id", ""))
            if cf.get("isCalculatedField") and not cf.get("calculation"):
                calc_hidden += 1
            for hit in walk(cf, ctx.matcher):
                top = hit.path[0] if hit.path else ""
                if top in ("id", "contractPath", "jsonPath", "modelPath"):
                    continue  # the field's own identity
                if top == "calculation":
                    cat, itype = "Custom Field Calculation", "Calculated custom field"
                elif top == "audit":
                    cat, itype = "Custom Field (Audit)", "AUDIT custom field (tracks changes to this field)"
                else:
                    cat, itype = "Custom Field Setup", "Custom field property"
                if hit.kind in (MatchKind.EXACT_VALUE, MatchKind.KEY):
                    support, notes = ChangeSupport.REPLACE_ONLY, "Removing would invalidate the custom field; replace or edit manually."
                else:
                    support, notes = ChangeSupport.REPLACE_ONLY, "Used in the calculation: replace is automatic, removal is manual."
                res.references.append(Reference(
                    field_id=hit.field_id, category=cat, item_type=itype, item_name=cid, item_id=cid,
                    location=f"Encompass > Settings > Loan Setup > Custom Fields > Loan Custom Fields > {cid}",
                    source=self.key, match_kind=hit.kind, confidence=hit.confidence,
                    snippet=hit.snippet, api=f"GET {self.LIST_V3}?ids={cid}", json_path=hit.path_str,
                    change_support=support, change_notes=notes,
                ))
        note = f"via {path}"
        if calc_hidden:
            note += (f"; {calc_hidden} calculated field(s) returned no 'calculation' text - the API user "
                     "must be an (super) administrator to see calculations")
        res.coverage.append(CoverageEntry(self.key, self.category, "ok", len(fields), note))
        downgrade_low(res.references)
        return res

    def apply_batch(self, ctx: ApplyContext, changes: List[ChangeRequest]) -> List[ChangeOutcome]:
        outcomes: List[ChangeOutcome] = []
        groups: Dict[str, List[ChangeRequest]] = defaultdict(list)
        for c in changes:
            groups[c.ref.item_id].append(c)
        for cid, group in groups.items():
            try:
                cur = extract_list(ctx.client.get(self.LIST_V3, {"ids": cid}))
                original = next(x for x in cur if isinstance(x, dict) and str(x.get("id")) == cid)
            except Exception as err:  # noqa: BLE001
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "failed",
                                           f"could not re-read custom field {cid}: {err}") for c in group]
                continue
            current, applied = copy.deepcopy(original), []
            for c in group:
                if c.action == "remove":
                    outcomes.append(manual_outcome(c, c.ref.change_notes))
                    continue
                er = apply_edit(current, c.ref.json_path, c.ref.match_kind, ctx.matcher,
                                c.ref.field_id, "replace", c.new_field_id)
                if not er.changed:
                    outcomes.append(manual_outcome(c, er.message))
                    continue
                current = er.obj
                applied.append((c, er))
            if not applied:
                continue
            body = {k: v for k, v in current.items() if k not in _CF_READ_ONLY}
            if ctx.dry_run:
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "dry_run",
                                           f"would PATCH {self.LIST_V3}?action=update for {cid}: {er.message}",
                                           er.before[:500], er.after[:500]) for c, er in applied]
                continue
            backup = write_backup(ctx.backup_dir, f"customField_{cid}", original)
            try:
                ctx.client.patch(self.LIST_V3, [body], {"action": "update"})
                status, msg = "applied", f"PATCH customFields?action=update {cid} OK"
            except ApiError as err:
                status, msg = "failed", str(err)
            outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, status,
                                       f"{msg}: {er.message}", er.before[:500], er.after[:500], backup)
                         for c, er in applied]
        return outcomes


# ====================================================================== Encompass web forms
class WebInputFormsSource(Source):
    """Encompass *web* input forms (Form Builder) via the Encompass web search API."""

    key = "rest:web_input_forms"
    category = "Input Form (Encompass web)"
    PATH = "/encwsearch/v1/search"

    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        total, failures = 0, []
        for fid in ctx.field_ids:
            if fid.lower().startswith("re:"):
                continue
            body = {"from": 0, "size": 10000, "query": {"bool": {"must": [{"term": {"fieldId": fid}}]}}}
            try:
                data, _ = ctx.client.post(self.PATH, body, {"key": "form_fields"})
            except Exception as err:  # noqa: BLE001
                failures.append(err)
                continue
            for row in extract_list(data, "results"):
                if not isinstance(row, dict):
                    continue
                total += 1
                custom = str(row.get("type", "")).lower() == "custom"
                name = row.get("formName") or row.get("name") or row.get("fileName") or row.get("id")
                state = "enabled" if row.get("enabled") else "disabled"
                res.references.append(Reference(
                    field_id=fid,
                    category=self.category if custom else "Standard Form (Encompass web)",
                    item_type=f"{row.get('type', 'Form')} web input form ({state})",
                    item_name=str(name), item_id=row.get("id"),
                    location=(f"Encompass web > Settings > Custom Forms (Form Builder) > '{name}'" if custom
                              else f"Encompass web standard form '{name}'"),
                    source=self.key, match_kind=MatchKind.SEARCH_INDEX, confidence=Confidence.HIGH,
                    snippet=(f"fieldType={row.get('fieldType')} visible={row.get('fieldVisible')} "
                             f"enabled={row.get('fieldEnabled')} devices={row.get('deviceTypes')} "
                             f"modified={row.get('modifiedDate')}"),
                    api=f"POST {self.PATH}?key=form_fields (fieldId={fid})",
                    change_support=ChangeSupport.MANUAL if custom else ChangeSupport.NONE,
                    change_notes=("No public API edits web forms: open the form in Form Builder and "
                                  "rebind/remove the control." if custom else "Standard form (ICE managed)."),
                    extra={"personaAccessList": row.get("personaAccessList"), "status": row.get("status")},
                ))
        if failures and len(failures) == len([f for f in ctx.field_ids if not f.lower().startswith("re:")]):
            res.coverage.append(coverage_from_error(self.key, self.category, failures[0], "Encompass web form search"))
        else:
            note = f"{len(failures)} field search(es) failed" if failures else ""
            res.coverage.append(CoverageEntry(self.key, self.category, "ok", total, note))
        return res


# ====================================================================== templates
TEMPLATE_TYPES: Dict[str, Tuple[str, str]] = {
    "loanProgram": ("Loan Program Template", "Encompass > Settings > Loan Templates > Loan Programs"),
    "closingCost": ("Closing Cost Template", "Encompass > Settings > Loan Templates > Closing Costs"),
    "loanTemplateSet": ("Loan Template Set", "Encompass > Settings > Loan Templates > Loan Template Sets"),
    "settlementServiceProvider": ("Settlement Service Provider Template",
                                  "Encompass > Settings > Loan Templates > Settlement Service Providers"),
}


class TemplatesSource(Source):
    """Crawls template folders and deep-scans every template (data is loan-contract JSON, so
    fields are matched by contract path as well as by id)."""

    key = "rest:templates"
    category = "Loan Templates"

    def __init__(self, types: Optional[List[str]] = None, roots: Optional[List[str]] = None,
                 max_templates: int = 5000) -> None:
        self.types = types or list(TEMPLATE_TYPES)
        self.roots = roots or ["Public:\\", "Personal:\\"]
        self.max_templates = max_templates

    def _crawl(self, client, ttype: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        seen = set()
        stack = list(self.roots)
        while stack and len(found) < self.max_templates:
            path = stack.pop()
            if path in seen:
                continue
            seen.add(path)
            try:
                body = client.get(f"/encompass/v3/settings/templates/{ttype}/folders", {"path": path})
            except ApiError as err:
                if err.status in (400, 404) and path in self.roots:
                    continue
                raise
            for ent in extract_list(body, "contents"):
                if not isinstance(ent, dict):
                    continue
                etype = str(ent.get("entityType", "")).lower()
                epath = ent.get("entityPath") or ent.get("path")
                if not epath:
                    continue
                if "folder" in etype or ent.get("hasSubFolders"):
                    stack.append(epath)
                else:
                    found.append(ent)
        return found

    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        for ttype in self.types:
            label, loc = TEMPLATE_TYPES.get(ttype, (ttype, f"Encompass > Settings > Templates > {ttype}"))
            try:
                ents = self._crawl(ctx.client, ttype)
            except Exception as err:  # noqa: BLE001
                res.coverage.append(coverage_from_error(f"{self.key}:{ttype}", self.category, err, label))
                continue

            def fetch(ent: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
                try:
                    return ent, ctx.client.get(f"/encompass/v3/settings/templates/{ttype}/items",
                                               {"path": ent.get("entityPath")})
                except ApiError:
                    return ent, None

            with ThreadPoolExecutor(max_workers=max(1, ctx.workers)) as pool:
                rows = list(pool.map(fetch, ents))
            failed = 0
            for ent, body in rows:
                if body is None:
                    failed += 1
                    continue
                name = ent.get("entityName") or ent.get("entityPath")
                for hit in walk(body, ctx.matcher, use_contract_paths=True):
                    res.references.append(Reference(
                        field_id=hit.field_id, category=self.category, item_type=label,
                        item_name=str(name), item_id=ent.get("entityPath"),
                        location=f"{loc} > {ent.get('entityPath')}", source=f"{self.key}:{ttype}",
                        match_kind=hit.kind, confidence=hit.confidence, snippet=hit.snippet,
                        api=f"GET /encompass/v3/settings/templates/{ttype}/items?path={ent.get('entityPath')}",
                        json_path=hit.path_str, change_support=ChangeSupport.MANUAL,
                        change_notes="No documented template update API; edit the template in Settings.",
                    ))
            note = f"{failed} template(s) could not be read" if failed else ""
            res.coverage.append(CoverageEntry(f"{self.key}:{ttype}", self.category, "ok", len(ents), note))
        downgrade_low(res.references)
        return res


# ====================================================================== loan data usage
class LoanUsageSource(Source):
    """Counts loans that hold a value in the field (Pipeline / RDB). Opt-in: it is a data
    question rather than a configuration dependency, but tells you the blast radius."""

    key = "rest:loan_usage"
    category = "Loan Data"

    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        n = 0
        for fid in ctx.field_ids:
            if fid.lower().startswith("re:"):
                continue
            body = {"filter": {"canonicalName": f"Fields.{fid}", "matchType": "isNotEmpty"},
                    "fields": ["Loan.GUID"]}
            try:
                _, hdrs = ctx.client.post("/encompass/v1/loanPipeline", body,
                                          {"cursorType": "randomAccess", "limit": 1})
            except Exception as err:  # noqa: BLE001
                res.coverage.append(coverage_from_error(self.key, self.category, err, f"loan usage {fid}"))
                continue
            count = hdrs.get("x-total-count")
            n += 1
            res.references.append(Reference(
                field_id=fid, category=self.category, item_type="Loans with a value",
                item_name=f"{count if count is not None else '?'} loan(s)",
                location="Encompass > Pipeline (filter on the field) - requires the field in the RDB",
                source=self.key, match_kind=MatchKind.LOAN_DATA, confidence=Confidence.HIGH,
                snippet=f"Fields.{fid} isNotEmpty -> x-total-count={count}",
                api="POST /encompass/v1/loanPipeline?cursorType=randomAccess&limit=1",
                change_support=ChangeSupport.NONE,
                change_notes="Loan data is not changed by this tool. Migrate values with a batch "
                             "update / field writer job if you retire the field.",
            ))
        if n:
            res.coverage.append(CoverageEntry(self.key, self.category, "ok", n))
        return res
