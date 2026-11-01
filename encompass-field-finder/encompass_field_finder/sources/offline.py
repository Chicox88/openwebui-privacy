"""Offline scanner for exported Encompass artifacts.

ICE does not publish REST APIs for several desktop configuration areas (business rules,
DDM, desktop custom input forms, reports, dashboards, alerts, custom print form field
mappings, codebase / plugins). Those can be exported from Encompass (Input Form Builder,
settings export / packager tools, report & dashboard template export, SDK dump utilities,
SQL exports for on-prem databases ...) and scanned here.

Supported inputs: folders, any text file (XML, JSON, CS, VB, SQL, CSV, HTML, RTF, ...),
zip-based containers (.zip, .empkg, .docx, .xlsx, .pptx, .nupkg, .jar - nested to any depth)
and binaries (.dll, .exe, .pdf, .doc, .xls ...) via string extraction.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from ..models import (
    ChangeOutcome, ChangeRequest, ChangeSupport, Confidence, CoverageEntry, MatchKind, Reference,
)
from ..walker import walk
from .base import ApplyContext, ScanContext, Source, SourceResult, downgrade_low, manual_outcome

ZIP_EXT = {".zip", ".empkg", ".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".pptx", ".nupkg", ".jar", ".emzip"}
BINARY_EXT = {".dll", ".exe", ".pdf", ".doc", ".xls", ".ppt", ".rpt", ".bin", ".dat", ".mdb", ".accdb"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".ttf", ".otf", ".woff", ".woff2",
            ".mp3", ".mp4", ".avi", ".wav"}
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_DEPTH = 6

# (regex on lower-cased path, category, item type, UI location hint)
CATEGORY_RULES: List[Tuple[str, str, str, str]] = [
    (r"ddm|dynamicdata|datatable|feerule|fee_rule|fieldscenario",
     "Dynamic Data Management", "DDM rule / data table", "Encompass > Settings > Dynamic Data Management"),
    (r"fielddataentry|field_data_entry|fieldrule|field_rule",
     "Business Rule", "Field Data Entry rule", "Encompass > Settings > Business Rules > Field Data Entry"),
    (r"fieldtrigger|field_trigger|trigger",
     "Business Rule", "Field Trigger rule", "Encompass > Settings > Business Rules > Field Triggers"),
    (r"personaaccesstofield|fieldaccess|field_access",
     "Business Rule", "Persona Access to Fields", "Encompass > Settings > Business Rules > Persona Access to Fields"),
    (r"loanaction|loan_action",
     "Business Rule", "Persona Access to Loan Actions / Loan Action Completion",
     "Encompass > Settings > Business Rules"),
    (r"automatedcondition|automated_condition|conditionrule",
     "Business Rule", "Automated Conditions rule", "Encompass > Settings > Business Rules > Automated Conditions"),
    (r"milestonerule|milestonecompletion|milestone_rule",
     "Business Rule", "Milestone Completion rule", "Encompass > Settings > Business Rules > Milestone Completion"),
    (r"inputformlist|loanform|formlist",
     "Business Rule", "Input Form List rule", "Encompass > Settings > Business Rules > Input Form List"),
    (r"printform(selection|rule)|printselection",
     "Business Rule", "Print Form rule", "Encompass > Settings > Business Rules > Print Forms"),
    (r"alert",
     "Alert", "Custom alert / alert rule", "Encompass > Settings > Business Rules / Custom Alerts"),
    (r"businessrule|business_rule|bpm|rule",
     "Business Rule", "Business rule", "Encompass > Settings > Business Rules"),
    (r"dashboard",
     "Dashboard", "Dashboard template / view", "Encompass > Dashboard > (template) / Settings > Dashboard Templates"),
    (r"report|\.rpt$|rdbfield|reportingdatabase",
     "Report", "Report template", "Encompass > Reports (Pipeline / Loan / Financial report templates)"),
    (r"pipelineview|pipeline_view",
     "Pipeline Views", "Pipeline view", "Encompass > Pipeline > Views"),
    (r"\.emfrm$|inputform|input_form|customform|custom_form|\.emform",
     "Custom Input Form (desktop)", "Custom input form", "Encompass > Settings > Forms > Custom Input Forms (Input Form Builder)"),
    (r"customprint|printform|\.pdf$",
     "Print Forms", "Custom print form", "Encompass > Settings > Forms > Custom Print Forms"),
    (r"\.docx?$|\.rtf$|letter|mailmerge",
     "Letter / Email Templates", "Custom letter / document template", "Encompass > Settings > Forms / Docs Setup"),
    (r"efolder|document|stacking",
     "eFolder Documents", "eFolder setup", "Encompass > Settings > eFolder Setup"),
    (r"customfield|custom_field|fielddef",
     "Custom Field Calculation", "Custom field setup", "Encompass > Settings > Loan Setup > Custom Fields"),
    (r"template|\.emtpl",
     "Loan Templates", "Template", "Encompass > Settings > Loan Templates"),
    (r"\.dll$|\.exe$|\.cs$|\.vb$|plugin|codebase",
     "Plugin / Codebase", "Plugin / form codebase", "Encompass > Settings > Company/User Setup > Plugins / Input Form Builder codebase"),
    (r"\.sql$|\.csv$",
     "Database / Data Export", "Exported data", "Exported database rows"),
]


@dataclass
class _File:
    display: str        # "exports/rules.empkg!/rules/field_rules.xml"
    chain: List[str]    # ["exports/rules.empkg", "rules/field_rules.xml"]
    data: bytes


def categorize(path: str) -> Tuple[str, str, str]:
    lp = path.lower().replace("\\", "/")
    for rx, cat, itype, loc in CATEGORY_RULES:
        if re.search(rx, lp):
            return cat, itype, loc
    return "Exported Setting", "Exported file", "Exported Encompass artifact"


def decode_text(data: bytes) -> Tuple[Optional[str], str]:
    """Return (text, encoding) or (None, '') for binary data."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    sample = data[:4096]
    if sample and sample.count(b"\x00") > len(sample) // 4:
        # probably UTF-16 without BOM (or binary)
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                txt = data.decode(enc)
            except UnicodeDecodeError:
                continue
            if sum(c.isprintable() or c in "\r\n\t" for c in txt[:2000]) > 0.9 * min(len(txt), 2000):
                return txt, enc
        return None, ""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    ctrl = sum(1 for b in sample if b < 9 or 13 < b < 32)
    if sample and ctrl / len(sample) > 0.05:
        return None, ""
    return data.decode("cp1252", "replace"), "cp1252"


def extract_strings(data: bytes, min_len: int = 3) -> str:
    """ASCII + UTF-16LE printable strings (like `strings -el`), one per line."""
    out = [m.group().decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data):
        out.append(m.group().decode("utf-16-le"))
    return "\n".join(out)


_TAG = re.compile(r"<(/?)([A-Za-z_][\w:.-]*)([^<>]*?)(/?)>")
_NAME_ATTR = re.compile(r"\b(?:Name|name|Title|title|RuleName|ID|Id|id|FormName)\s*=\s*\"([^\"]{1,80})\"")


def xml_path_at(text: str, offset: int) -> str:
    """Best-effort element path (with identifying attributes) enclosing ``offset``."""
    stack: List[str] = []
    for m in _TAG.finditer(text, 0, offset):
        closing, name, attrs, selfclose = m.groups()
        if name.startswith(("?", "!")):
            continue
        if closing:
            while stack and stack[-1].split("[", 1)[0] != name:
                stack.pop()
            if stack:
                stack.pop()
        elif not selfclose:
            na = _NAME_ATTR.search(attrs or "")
            stack.append(f"{name}[@{na.group(0).split('=')[0].strip()}='{na.group(1)}']" if na else name)
    # if the hit is inside the attributes of an unclosed tag, include it
    lt, gt = text.rfind("<", 0, offset), text.rfind(">", 0, offset)
    if lt > gt:
        m = re.match(r"<([A-Za-z_][\w:.-]*)", text[lt:])
        if m:
            na = _NAME_ATTR.search(text[lt:text.find(">", lt) if text.find(">", lt) > 0 else len(text)])
            stack.append(f"{m.group(1)}[@{na.group(0).split('=')[0].strip()}='{na.group(1)}']" if na else m.group(1))
    return "/" + "/".join(stack[-8:]) if stack else ""


class OfflineExportSource(Source):
    key = "offline:exports"
    category = "Offline Export"
    requires_client = False

    def __init__(self, paths: List[str]) -> None:
        self.paths = paths

    # ------------------------------------------------------------------ reading
    def _iter(self) -> Iterator[_File]:
        for root in self.paths:
            if os.path.isdir(root):
                for dirpath, _dirs, files in os.walk(root):
                    for fn in sorted(files):
                        full = os.path.join(dirpath, fn)
                        yield from self._iter_file(full)
            elif os.path.isfile(root):
                yield from self._iter_file(root)

    def _iter_file(self, full: str) -> Iterator[_File]:
        ext = os.path.splitext(full)[1].lower()
        if ext in SKIP_EXT or os.path.getsize(full) > MAX_FILE_BYTES:
            return
        with open(full, "rb") as fh:
            data = fh.read()
        yield from self._expand(_File(full, [full], data), 0)

    def _expand(self, f: _File, depth: int) -> Iterator[_File]:
        ext = os.path.splitext(f.chain[-1])[1].lower()
        if depth < MAX_DEPTH and (ext in ZIP_EXT or f.data[:4] == b"PK\x03\x04"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(f.data))
            except zipfile.BadZipFile:
                yield f
                return
            with zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_FILE_BYTES:
                        continue
                    if os.path.splitext(info.filename)[1].lower() in SKIP_EXT:
                        continue
                    member = _File(f"{f.display}!/{info.filename}", f.chain + [info.filename], zf.read(info))
                    yield from self._expand(member, depth + 1)
            return
        yield f

    # ------------------------------------------------------------------ scanning
    def scan(self, ctx: ScanContext) -> SourceResult:
        res = SourceResult()
        counts: Dict[str, int] = defaultdict(int)
        n = 0
        for f in self._iter():
            n += 1
            ctx.progress(f"offline: {f.display}")
            cat, itype, loc = categorize(f.display)
            counts[cat] += 1
            ext = os.path.splitext(f.chain[-1])[1].lower()
            text, enc = (None, "") if ext in BINARY_EXT else decode_text(f.data)
            if text is None:
                strings = extract_strings(f.data)
                binary_note = ("Binary file (compiled code / PDF / legacy Office): change in the source "
                               "project or designer and re-import.")
                for s in set(strings.splitlines()):
                    fid = ctx.matcher.exact(s)
                    if fid:  # a string literal that *is* the field id (e.g. Fields["CX.X"] compiled)
                        res.references.append(self._ref(f, cat, itype, loc, fid, MatchKind.EXACT_VALUE,
                                                        Confidence.MEDIUM, f"string literal {s!r}", None,
                                                        None, ChangeSupport.MANUAL, binary_note))
                for tm in ctx.matcher.find(strings):
                    res.references.append(self._ref(f, cat, itype, loc, tm.field_id, tm.kind, tm.confidence,
                                                    tm.snippet(strings, 50), None, None,
                                                    ChangeSupport.MANUAL,
                                                    "Binary file (compiled code / PDF / legacy Office): "
                                                    "change in the source project or designer and re-import."))
                continue
            if ext == ".json" or (text.lstrip()[:1] in "{[" and ext not in (".cs", ".vb", ".js")):
                try:
                    obj = json.loads(text)
                except ValueError:
                    obj = None
                if obj is not None:
                    for hit in walk(obj, ctx.matcher, decode_base64=True):
                        res.references.append(self._ref(
                            f, cat, itype, loc, hit.field_id, hit.kind, hit.confidence, hit.snippet,
                            None, hit.path_str,
                            ChangeSupport.MANUAL if hit.snippet.startswith("[base64]") else ChangeSupport.REPLACE_ONLY,
                            "A modified copy is written to the output folder; re-import it into Encompass.",
                            enc))
                    continue
            line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
            is_xml = text.lstrip().startswith("<")
            for tm in ctx.matcher.find(text):
                line = _line_of(line_starts, tm.start)
                xp = xml_path_at(text, tm.start) if is_xml else None
                res.references.append(self._ref(
                    f, cat, itype, loc, tm.field_id, tm.kind, tm.confidence, tm.snippet(text), line, xp,
                    ChangeSupport.REPLACE_ONLY,
                    "A modified copy is written to the output folder; re-import it into Encompass. "
                    "Removal must be done in the designer/settings screen.", enc))
        for cat, c in sorted(counts.items()):
            res.coverage.append(CoverageEntry(self.key, cat, "ok", c, "offline export files"))
        if n == 0:
            res.coverage.append(CoverageEntry(self.key, self.category, "skipped", 0,
                                              f"no readable files under {self.paths}"))
        downgrade_low(res.references)
        return res

    def _ref(self, f: _File, cat: str, itype: str, loc: str, fid: str, kind: MatchKind,
             conf: Confidence, snippet: str, line: Optional[int], json_path: Optional[str],
             support: ChangeSupport, notes: str, encoding: str = "") -> Reference:
        name = os.path.basename(f.chain[-1])
        return Reference(
            field_id=fid, category=cat, item_type=itype, item_name=name, item_id=f.display,
            location=f"{loc} (exported file: {f.display}{':' + str(line) if line else ''})",
            source=self.key, match_kind=kind, confidence=conf, snippet=snippet,
            json_path=json_path, file_path=f.display, line=line, change_support=support,
            change_notes=notes, extra={"chain": f.chain, "encoding": encoding},
        )

    # ------------------------------------------------------------------ rewriting
    def apply_batch(self, ctx: ApplyContext, changes: List[ChangeRequest]) -> List[ChangeOutcome]:
        outcomes: List[ChangeOutcome] = []
        by_top: Dict[str, List[ChangeRequest]] = defaultdict(list)
        for c in changes:
            if c.action != "replace" or c.ref.change_support in (ChangeSupport.MANUAL, ChangeSupport.NONE):
                outcomes.append(manual_outcome(c))
                continue
            by_top[c.ref.extra["chain"][0]].append(c)
        for top, group in by_top.items():
            if not os.path.isfile(top):
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "failed",
                                           f"source file {top} not found") for c in group]
                continue
            with open(top, "rb") as fh:
                data = fh.read()
            # member chain (without top) -> [(old, new)]
            edits: Dict[Tuple[str, ...], List[ChangeRequest]] = defaultdict(list)
            for c in group:
                edits[tuple(c.ref.extra["chain"][1:])].append(c)
            try:
                new_data, results = self._rewrite(data, (), edits, ctx)
            except Exception as err:  # noqa: BLE001
                outcomes += [ChangeOutcome(c.ref.ref_id, c.ref.field_id, c.action, "failed", str(err)) for c in group]
                continue
            out_path = os.path.join(ctx.output_dir, _relative_out(top))
            for c, (ok, msg) in results.items():
                status = ("dry_run" if ctx.dry_run else "applied") if ok else "failed"
                outcomes.append(ChangeOutcome(
                    c.ref.ref_id, c.ref.field_id, c.action, status,
                    (f"would write {out_path}: " if ctx.dry_run else f"wrote {out_path}: ") + msg
                    if ok else msg))
            if not ctx.dry_run and any(ok for ok, _ in results.values()):
                os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                with open(out_path, "wb") as fh:
                    fh.write(new_data)
        return outcomes

    def _rewrite(self, data: bytes, prefix: Tuple[str, ...],
                 edits: Dict[Tuple[str, ...], List[ChangeRequest]], ctx: ApplyContext):
        results: Dict[ChangeRequest, Tuple[bool, str]] = {}
        here = [c for key, cs in edits.items() if key == prefix for c in cs]
        deeper = {k: v for k, v in edits.items() if len(k) > len(prefix) and k[:len(prefix)] == prefix}
        if here:
            text, enc = decode_text(data)
            if text is None:
                for c in here:
                    results[c] = (False, "file is binary; cannot rewrite")
            else:
                done_pairs = set()
                for c in here:
                    pair = (c.ref.field_id.lower(), (c.new_field_id or "").lower())
                    if pair in done_pairs:
                        results[c] = (True, "replaced together with another hit in the same file")
                        continue
                    text, n = ctx.matcher.replace(text, c.ref.field_id, c.new_field_id)
                    done_pairs.add(pair)
                    results[c] = (n > 0, f"replaced {n} occurrence(s) of {c.ref.field_id}"
                                  if n else "reference no longer present")
                data = _encode(text, enc)
        if deeper:
            src = zipfile.ZipFile(io.BytesIO(data))
            buf = io.BytesIO()
            with src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
                for info in src.infolist():
                    member = src.read(info)
                    key = prefix + (info.filename,)
                    sub = {k: v for k, v in deeper.items() if k[:len(key)] == key}
                    if sub:
                        member, r = self._rewrite(member, key, sub, ctx)
                        results.update(r)
                    dst.writestr(info, member)
            data = buf.getvalue()
        return data, results


def _encode(text: str, enc: str) -> bytes:
    if enc == "utf-8-sig":
        return b"\xef\xbb\xbf" + text.encode("utf-8")
    return text.encode(enc or "utf-8")


def _line_of(starts: List[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _relative_out(path: str) -> str:
    p = os.path.abspath(path)
    cwd = os.path.abspath(os.getcwd())
    if p.startswith(cwd + os.sep):
        return os.path.relpath(p, cwd)
    _drive, rest = os.path.splitdrive(p)
    return rest.lstrip("\\/")
