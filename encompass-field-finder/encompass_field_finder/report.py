"""Render scan results / change outcomes as text, CSV, Markdown, JSON or HTML."""

from __future__ import annotations

import csv
import html
import io
from collections import Counter
from typing import Iterable, List, Optional

from .models import ChangeOutcome, Reference, ScanResult

CSV_COLUMNS = [
    "ref_id", "field_id", "category", "item_type", "item_name", "location", "match_kind", "confidence",
    "change_support", "snippet", "json_path", "file_path", "line", "api", "source", "change_notes",
]

_STATUS_LABEL = {
    "ok": "scanned", "not_available": "not available", "forbidden": "no permission",
    "error": "error", "skipped": "skipped", "manual_only": "NOT SCANNED (no public API)",
}


def to_csv(result: ScanResult) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in result.references:
        w.writerow(r.to_dict())
    return buf.getvalue()


def to_text(result: ScanResult, show_coverage: bool = True) -> str:
    lines: List[str] = []
    for fid, refs in result.by_field().items():
        info = result.field_info.get(fid, {})
        desc = info.get("description") or info.get("warning") or ""
        lines.append(f"\n=== {fid}  {('- ' + desc) if desc else ''}")
        if not refs:
            lines.append("  (no references found in the scanned sources)")
            continue
        counts = Counter(r.category for r in refs)
        lines.append("  " + ", ".join(f"{c}: {n}" for c, n in counts.most_common()))
        for r in refs:
            lines.append(f"  [{r.ref_id}] {r.category} | {r.item_type} | {r.item_name}")
            lines.append(f"      where : {r.location}")
            where = r.json_path or (f"line {r.line}" if r.line else "")
            if where:
                lines.append(f"      at    : {where}")
            lines.append(f"      match : {r.match_kind.value} ({r.confidence.value}) -> {r.snippet[:160]}")
            lines.append(f"      change: {r.change_support.value}{(' - ' + r.change_notes) if r.change_notes else ''}")
    if show_coverage:
        lines.append("\n=== Coverage")
        for c in result.coverage:
            lines.append(f"  {_STATUS_LABEL.get(c.status, c.status):28} {c.category:32} {c.source:45} "
                         f"{c.items_scanned:>6}  {c.note}")
    return "\n".join(lines) + "\n"


def to_markdown(result: ScanResult) -> str:
    out = ["# Encompass field dependency report", "",
           f"Fields: {', '.join(result.field_ids)}  ", f"Scanned: {result.started_at} - {result.finished_at}", ""]
    for fid, refs in result.by_field().items():
        out += [f"## {fid}", ""]
        if not refs:
            out += ["_No references found in the scanned sources._", ""]
            continue
        out += ["| Category | Item | Where to find it | Match | Change |", "|---|---|---|---|---|"]
        for r in refs:
            out.append("| {} | {} | {} | {} | {} |".format(
                _md(r.category), _md(f"{r.item_type}: {r.item_name}"),
                _md(r.location + (f" ({r.json_path})" if r.json_path else "")),
                _md(f"{r.match_kind.value}/{r.confidence.value}: {r.snippet[:100]}"),
                _md(r.change_support.value)))
        out.append("")
    out += ["## Coverage", "", "| Status | Area | Source | Items | Note |", "|---|---|---|---|---|"]
    for c in result.coverage:
        out.append(f"| {_STATUS_LABEL.get(c.status, c.status)} | {_md(c.category)} | {_md(c.source)} | "
                   f"{c.items_scanned} | {_md(c.note)} |")
    return "\n".join(out) + "\n"


def _md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def outcomes_to_text(outcomes: Iterable[ChangeOutcome]) -> str:
    lines = []
    for o in outcomes:
        lines.append(f"[{o.ref_id}] {o.status.upper():8} {o.action} {o.field_id}: {o.message}")
        if o.before is not None and o.after is not None and o.before != o.after:
            lines.append(f"      before: {o.before[:200]}")
            lines.append(f"      after : {o.after[:200]}")
        if o.backup_path:
            lines.append(f"      backup: {o.backup_path}")
    counts = Counter(o.status for o in outcomes)
    lines.append("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- HTML
_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:22px;margin:0 0 4px} h2{font-size:18px;margin:28px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
.meta{color:#59636e;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:12px}
th,td{border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f6f8fa;position:sticky;top:0}
tr:nth-child(even) td{background:#fbfcfd}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid #d0d7de;white-space:nowrap}
.high{background:#dafbe1}.medium{background:#fff8c5}.low{background:#ffebe9}
.auto{background:#dafbe1}.replace_only{background:#ddf4ff}.manual{background:#fff8c5}.none{background:#f6f8fa}
.manual_only{background:#ffebe9;font-weight:600}.ok{background:#dafbe1}.not_available,.skipped{background:#f6f8fa}
.forbidden,.error{background:#ffebe9}
.summary{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px}
.summary span{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:2px 8px;font-size:12px}
input.filter{padding:6px 10px;width:340px;border:1px solid #d0d7de;border-radius:6px;margin-bottom:8px}
@media (prefers-color-scheme: dark){body{background:#0d1117;color:#e6edf3}th{background:#161b22}
tr:nth-child(even) td{background:#0f141a}th,td,.pill,.summary span,input.filter{border-color:#30363d}
.summary span,.none,.not_available,.skipped{background:#161b22}.high,.auto,.ok{background:#12361f}
.medium,.manual{background:#3a2f0b}.low,.manual_only,.forbidden,.error{background:#3d1214}.replace_only{background:#0c2d4a}
input.filter{background:#0d1117;color:#e6edf3}.meta{color:#9198a1}}
"""

_JS = """
function f(inp){const q=inp.value.toLowerCase();document.querySelectorAll('tr.ref').forEach(tr=>{
tr.style.display=tr.innerText.toLowerCase().includes(q)?'':'none'})}
"""


def _e(s: Optional[object]) -> str:
    return html.escape("" if s is None else str(s))


def references_table_html(refs: List[Reference], selectable: bool = False) -> str:
    head = ("<tr>" + ("<th></th>" if selectable else "") +
            "<th>Category</th><th>Item</th><th>Where to find it</th><th>Match</th><th>Change</th></tr>")
    rows = []
    for r in refs:
        where = _e(r.location)
        if r.json_path:
            where += f"<br><code>{_e(r.json_path)}</code>"
        if r.api:
            where += f"<br><code>{_e(r.api)}</code>"
        chk = ""
        if selectable:
            dis = "" if r.change_support.value in ("auto", "replace_only") else " disabled"
            chk = f"<td><input type='checkbox' name='ref' value='{_e(r.ref_id)}'{dis}></td>"
        rows.append(
            f"<tr class='ref'>{chk}<td>{_e(r.category)}</td>"
            f"<td><b>{_e(r.item_name)}</b><br><small>{_e(r.item_type)}</small><br><small><code>{_e(r.ref_id)}</code></small></td>"
            f"<td>{where}</td>"
            f"<td><span class='pill {r.confidence.value}'>{_e(r.confidence.value)}</span> "
            f"<small>{_e(r.match_kind.value)}</small><br><code>{_e(r.snippet[:240])}</code></td>"
            f"<td><span class='pill {r.change_support.value}'>{_e(r.change_support.value)}</span>"
            f"<br><small>{_e(r.change_notes)}</small></td></tr>")
    return f"<table>{head}{''.join(rows)}</table>"


def coverage_table_html(result: ScanResult) -> str:
    rows = "".join(
        f"<tr><td><span class='pill {c.status}'>{_e(_STATUS_LABEL.get(c.status, c.status))}</span></td>"
        f"<td>{_e(c.category)}</td><td><code>{_e(c.source)}</code></td><td>{c.items_scanned}</td>"
        f"<td>{_e(c.note)}</td></tr>"
        for c in sorted(result.coverage, key=lambda c: (c.status != "manual_only", c.category)))
    return ("<table><tr><th>Status</th><th>Area</th><th>Source</th><th>Items</th><th>Note</th></tr>"
            f"{rows}</table>")


def body_html(result: ScanResult, selectable: bool = False) -> str:
    parts = [f"<div class='meta'>Fields: <b>{_e(', '.join(result.field_ids))}</b> &middot; "
             f"{len(result.references)} reference(s) &middot; {_e(result.started_at)} &rarr; {_e(result.finished_at)}</div>",
             "<input class='filter' placeholder='Filter references...' oninput='f(this)'>"]
    for fid, refs in result.by_field().items():
        info = result.field_info.get(fid, {})
        desc = info.get("description") or info.get("warning") or ""
        parts.append(f"<h2>{_e(fid)} <small style='font-weight:normal'>{_e(desc)}</small></h2>")
        if not refs:
            parts.append("<p>No references found in the scanned sources (see coverage below).</p>")
            continue
        counts = Counter(r.category for r in refs)
        parts.append("<div class='summary'>" + "".join(
            f"<span>{_e(c)}: <b>{n}</b></span>" for c, n in counts.most_common()) + "</div>")
        parts.append(references_table_html(refs, selectable))
    parts.append("<h2>Coverage &mdash; what was and was not scanned</h2>")
    parts.append(coverage_table_html(result))
    return "\n".join(parts)


def to_html(result: ScanResult, title: str = "Encompass field dependency report") -> str:
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
            f"<style>{_CSS}</style><script>{_JS}</script></head><body><h1>{_e(title)}</h1>"
            f"{body_html(result)}</body></html>")


def render(result: ScanResult, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "json":
        return result.to_json()
    if fmt == "csv":
        return to_csv(result)
    if fmt in ("md", "markdown"):
        return to_markdown(result)
    if fmt == "html":
        return to_html(result)
    return to_text(result)
