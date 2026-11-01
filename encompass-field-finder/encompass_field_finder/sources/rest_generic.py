"""Catalog-driven scanner for Encompass settings endpoints."""

from __future__ import annotations

import copy
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from ..client import ApiError
from ..models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, CoverageEntry, MatchKind, Reference,
)
from ..walker import apply_edit, parse_path, walk
from .base import (
    ApplyContext, ScanContext, Source, SourceResult, coverage_from_error, downgrade_low,
    extract_list, fill, first_key, manual_outcome, write_backup,
)

log = logging.getLogger(__name__)


class GenericSettingsSource(Source):
    """Scans one catalog entry: list (+ optional detail) endpoints, deep-searching the JSON."""

    def __init__(self, entry: Dict[str, Any]) -> None:
        self.entry = entry
        self.key = f"rest:{entry['key']}"
        self.category = entry.get("category", "Settings")
        self.description = entry.get("item_type", entry["key"])

    # ------------------------------------------------------------------ fetching
    def _list(self, ctx: ScanContext) -> List[Any]:
        spec = self.entry["list"]
        params = dict(spec.get("params") or {})
        page = spec.get("paginate")
        if not page:
            return extract_list(ctx.client.get(spec["path"], params or None), spec.get("items_key"))
        items: List[Any] = []
        start, limit = page.get("start", 0), page.get("limit", 1000)
        for _ in range(page.get("max_pages", 50)):
            params[page.get("start_param", "start")] = start
            params[page.get("limit_param", "limit")] = limit
            batch = extract_list(ctx.client.get(spec["path"], params), spec.get("items_key"))
            items.extend(batch)
            if len(batch) < limit:
                break
            start += limit
        return items

    def _item_id(self, item: Any) -> str:
        if self.entry.get("item_is_name") and isinstance(item, str):
            return item
        return first_key(item, self.entry.get("id_keys", ["id"]))

    def _listed_min(self, item: Any) -> Any:
        """The bits of a list item needed to re-fetch it later (kept in the scan result)."""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            keys = list(self.entry.get("id_keys", ["id"])) + list(self.entry.get("name_keys", []))
            return {k: item[k] for k in keys if k in item and isinstance(item[k], (str, int))}
        return None

    def _item_name(self, item: Any, default: str) -> str:
        keys = self.entry.get("name_keys", ["name", "title", "id"])
        return first_key(item, keys, default) if keys else default

    def _detail_request(self, item: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        d = self.entry.get("detail")
        if not d:
            return None
        iid = self._item_id(item)
        if not iid:
            return None
        base = item if isinstance(item, dict) else {}
        path = fill(d["path"], base, id=iid)
        params = {k: fill(str(v), base, id=iid) for k, v in (d.get("params") or {}).items()}
        return path, params

    def _fetch_detail(self, ctx: ScanContext, item: Any) -> Tuple[Any, Optional[str]]:
        req = self._detail_request(item)
        if not req:
            return item, None
        try:
            return ctx.client.get(req[0], req[1] or None), None
        except ApiError as e:
            return item, f"{req[0]}: HTTP {e.status}"

    # ------------------------------------------------------------------ scan
    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        e = self.entry
        try:
            items = self._list(ctx)
        except Exception as err:  # noqa: BLE001
            cov = coverage_from_error(self.key, self.category, err, e.get("item_type", e["key"]))
            if e.get("documented", True) is False and cov.status == "not_available":
                cov.note += " (undocumented endpoint probe)"
            res.coverage.append(cov)
            return res

        detail_errors: List[str] = []
        if e.get("detail"):
            with ThreadPoolExecutor(max_workers=max(1, ctx.workers)) as pool:
                fetched = list(pool.map(lambda it: self._fetch_detail(ctx, it), items))
        else:
            fetched = [(it, None) for it in items]

        update_ok = bool(e.get("update"))
        for idx, ((obj, derr), listed) in enumerate(zip(fetched, items)):
            if derr:
                detail_errors.append(derr)
            iid = self._item_id(listed) or self._item_id(obj) or str(idx)
            name = self._item_name(obj, "") or self._item_name(listed, iid) or iid
            api_req = self._detail_request(listed)
            api = f"GET {api_req[0]}" if api_req else f"GET {e['list']['path']}"
            for hit in walk(obj, ctx.matcher, e.get("contract_paths", False), e.get("decode_base64", False)):
                support, notes = self._support_for(hit.kind, update_ok)
                res.references.append(Reference(
                    field_id=hit.field_id,
                    category=self.category,
                    item_type=e.get("item_type", e["key"]),
                    item_name=name,
                    item_id=iid,
                    location=fill(e.get("location", ""), {}, name=name),
                    source=self.key,
                    match_kind=hit.kind,
                    confidence=hit.confidence,
                    snippet=hit.snippet,
                    api=api,
                    json_path=hit.path_str,
                    change_support=support,
                    change_notes=notes,
                    extra={"catalog_key": e["key"], "listed": self._listed_min(listed),
                           "base64": hit.snippet.startswith("[base64]")},
                ))
        note = e.get("notes", "")
        if detail_errors:
            note = (note + " " if note else "") + f"{len(detail_errors)} detail call(s) failed, e.g. {detail_errors[0]}"
        res.coverage.append(CoverageEntry(self.key, self.category, "ok", len(items), note))
        downgrade_low(res.references)
        return res

    @staticmethod
    def _support_for(kind: MatchKind, update_ok: bool) -> Tuple[ChangeSupport, str]:
        if not update_ok:
            return ChangeSupport.MANUAL, "No documented update API for this object type."
        if kind in (MatchKind.EXACT_VALUE, MatchKind.KEY):
            return ChangeSupport.AUTO, ""
        if kind == MatchKind.CONTRACT_PATH:
            return ChangeSupport.MANUAL, "Referenced by loan contract path; edit manually."
        return ChangeSupport.REPLACE_ONLY, "Used inside an expression/text: replace is automatic, removal is manual."

    # ------------------------------------------------------------------ apply
    def _refetch(self, ctx: ApplyContext, ref: Reference) -> Any:
        e = self.entry
        listed = ref.extra.get("listed") or {k: ref.item_id for k in e.get("id_keys", ["id"])[:1]}
        req = self._detail_request(listed)
        if req:
            return ctx.client.get(req[0], req[1] or None)
        for it in self._list(ScanContext([], ctx.matcher, ctx.client)):
            if self._item_id(it) == ref.item_id:
                return it
        raise LookupError(f"{ref.item_type} '{ref.item_name}' not found any more")

    def apply_batch(self, ctx: ApplyContext, changes: List[ChangeRequest]) -> List[ChangeOutcome]:
        upd = self.entry.get("update")
        if not upd:
            return [manual_outcome(c) for c in changes]
        outcomes: List[ChangeOutcome] = []
        groups: Dict[str, List[ChangeRequest]] = defaultdict(list)
        for c in changes:
            groups[c.ref.item_id or c.ref.item_name].append(c)

        for item_id, group in groups.items():
            ref0 = group[0].ref
            try:
                original = self._refetch(ctx, ref0)
            except Exception as err:  # noqa: BLE001
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "failed", str(err)) for c in group]
                continue
            current = copy.deepcopy(original)
            applied: List[Tuple[ChangeRequest, Any]] = []
            # replacements first, then removals deepest/highest-index first so paths stay valid
            replaces = [c for c in group if c.action != "remove"]
            removals = sorted((c for c in group if c.action == "remove"),
                              key=lambda c: _path_sort_key(c.ref.json_path), reverse=True)
            for c in replaces + removals:
                if c.ref.change_support in (ChangeSupport.MANUAL, ChangeSupport.NONE) or (
                    c.action == "remove" and c.ref.change_support == ChangeSupport.REPLACE_ONLY
                ):
                    outcomes.append(manual_outcome(c))
                    continue
                try:
                    er = apply_edit(current, c.ref.json_path, c.ref.match_kind, ctx.matcher,
                                    c.ref.field_id, c.action, c.new_field_id,
                                    base64_value=bool(c.ref.extra.get("base64")))
                except Exception as err:  # noqa: BLE001
                    outcomes.append(ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "failed", str(err)))
                    continue
                if not er.changed:
                    outcomes.append(manual_outcome(c, er.message))
                    continue
                current = er.obj
                applied.append((c, er))
            if not applied:
                continue
            body_item = copy.deepcopy(current)
            if isinstance(body_item, dict):
                for k in upd.get("strip", []):
                    body_item.pop(k, None)
            body = [body_item] if upd.get("body") == "list" else body_item
            path = fill(upd["path"], current if isinstance(current, dict) else {}, id=item_id)
            params = dict(upd.get("params") or {})
            if ctx.dry_run:
                for c, er in applied:
                    outcomes.append(ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "dry_run",
                                                  f"would {upd['method']} {path}: {er.message}",
                                                  er.before[:500], er.after[:500]))
                continue
            backup = write_backup(ctx.backup_dir, f"{self.entry['key']}_{item_id}", original)
            try:
                ctx.client.request(upd["method"], path, params=params or None, json_body=body)
                status, msg = "applied", f"{upd['method']} {path} OK"
            except ApiError as err:
                status, msg = "failed", str(err)
            for c, er in applied:
                outcomes.append(ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, status,
                                              f"{msg}: {er.message}", er.before[:500], er.after[:500], backup))
        return outcomes


def _path_sort_key(path: Optional[str]) -> Tuple:
    try:
        return tuple((0, p, "") if isinstance(p, int) else (1, 0, p) for p in parse_path(path or "$"))
    except ValueError:
        return ()
