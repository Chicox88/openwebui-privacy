"""Field-id aware text matching and rewriting.

Encompass field ids come in many shapes: ``4000``, ``CX.LOAN.SCORE``, ``FR0104``, ``NEWHUD.X1``,
``DISCLOSEDGFE.Snapshot.X1`` ... and they appear in many syntaxes:

* custom field calculations / business rule advanced code: ``[4000]``, ``[#4000]``
* codebase / plugin / form code: ``Fields["4000"]``, ``Loan.Fields("4000")``, ``GetField("4000")``
* pipeline / reporting canonical names: ``Fields.4000``
* settings JSON / XML: ``"fieldId": "4000"``, ``<FieldID>4000</FieldID>``, ``FieldID="4000"``

Matching numeric ids as bare words would flag every ``4000`` in every document, so every hit
gets a confidence. By default only ``medium`` and ``high`` hits are reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .models import Confidence, MatchKind

# Characters that may legally appear inside a field id.
_ID_CHAR = r"[A-Za-z0-9_]"
# A field id is "not continued" if it is not followed by an id char or by ".<idchar>".
_NOT_CONTINUED = r"(?![A-Za-z0-9_]|\.[A-Za-z0-9_])"
_NOT_PRECEDED = r"(?<![A-Za-z0-9_.])"

_ACCESSOR_NAMES = (
    r"Fields|GetField|SetField|GetSimpleField|SetSimpleField|GetFieldValue|SetFieldValue|"
    r"GetCurrentField|SetCurrentField|FieldID|FieldId|fieldId|FieldValue|GetFieldEx|IsFieldLocked"
)


@dataclass(frozen=True)
class TextMatch:
    field_id: str
    start: int           # start of the field id itself inside the text
    end: int
    kind: MatchKind
    confidence: Confidence

    def snippet(self, text: str, width: int = 60) -> str:
        a = max(0, self.start - width)
        b = min(len(text), self.end + width)
        s = text[a:b].replace("\r", " ").replace("\n", " ").replace("\t", " ")
        return ("..." if a > 0 else "") + s + ("..." if b < len(text) else "")


def normalize_field_id(raw: str) -> str:
    return raw.strip().strip("[]").strip()


def is_numeric_id(field_id: str) -> bool:
    return field_id.isdigit()


class FieldMatcher:
    """Finds references to a set of field ids in strings, and rewrites them.

    ``aliases`` maps field id -> list of loan-contract paths (e.g. ``loan.baseLoanAmount``)
    used to recognise structured loan JSON (templates, webhook filters) that refer to a
    field by its contract path instead of its id.
    """

    def __init__(
        self,
        field_ids: Sequence[str],
        min_confidence: Confidence = Confidence.MEDIUM,
        aliases: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.field_ids: List[str] = []
        self._regex_ids: Dict[str, str] = {}
        for raw in field_ids:
            fid = normalize_field_id(raw)
            if not fid:
                continue
            if fid.lower().startswith("re:"):
                self._regex_ids[fid] = fid[3:]
            self.field_ids.append(fid)
        self.min_confidence = min_confidence
        self.aliases: Dict[str, List[str]] = {}
        for fid, paths in (aliases or {}).items():
            for p in paths:
                self.add_alias(fid, p)
        self._compiled: Dict[str, List[Tuple[re.Pattern, MatchKind, Confidence]]] = {
            fid: self._compile(fid) for fid in self.field_ids
        }
        self._lower = {fid.lower(): fid for fid in self.field_ids if fid not in self._regex_ids}

    # ------------------------------------------------------------------ patterns
    def _id_pattern(self, fid: str) -> str:
        if fid in self._regex_ids:
            return f"(?:{self._regex_ids[fid]})"
        return re.escape(fid)

    def _compile(self, fid: str) -> List[Tuple[re.Pattern, MatchKind, Confidence]]:
        idp = self._id_pattern(fid)
        short_numeric = is_numeric_id(fid) and len(fid) <= 3
        quoted_conf = Confidence.LOW if short_numeric else Confidence.MEDIUM
        bare_conf = Confidence.LOW
        flags = re.IGNORECASE
        pats = [
            # [4000]  [#4000]  [ 4000 ]  -- calc / business-rule expression syntax
            (rf"\[\s*#?\s*(?P<id>{idp})\s*\]", MatchKind.CALC_EXPRESSION, Confidence.HIGH),
            # Fields["4000"], Fields("4000"), GetField("4000"), FieldID="4000", fieldId: "4000"
            (
                rf"(?<![A-Za-z0-9_])(?:{_ACCESSOR_NAMES})[\"']?\s*(?:\.|\(|\[|=|:)\s*@?[\"']?"
                rf"(?P<id>{idp}){_NOT_CONTINUED}",
                MatchKind.CODE_ACCESSOR,
                Confidence.HIGH,
            ),
            # <FieldID>4000</FieldID> / >4000<
            (rf">\s*(?P<id>{idp})\s*<", MatchKind.XML_TEXT, quoted_conf),
            # "4000" / '4000'
            (rf"(?P<q>[\"'])(?P<id>{idp})(?P=q)", MatchKind.QUOTED, quoted_conf),
            # bare token
            (rf"{_NOT_PRECEDED}(?P<id>{idp}){_NOT_CONTINUED}", MatchKind.BARE_TOKEN, bare_conf),
        ]
        return [(re.compile(p, flags), k, c) for p, k, c in pats]

    def add_alias(self, field_id: str, contract_path: str) -> None:
        norm = normalize_contract_path(contract_path)
        if norm and norm not in self.aliases.setdefault(field_id, []):
            self.aliases[field_id].append(norm)

    # ------------------------------------------------------------------ queries
    def exact(self, value: str) -> Optional[str]:
        """Return the field id if ``value`` *is* one of the field ids (case-insensitive)."""
        if not isinstance(value, str):
            return None
        v = normalize_field_id(value)
        hit = self._lower.get(v.lower())
        if hit:
            return hit
        for fid, rx in self._regex_ids.items():
            if re.fullmatch(rx, v, re.IGNORECASE):
                return fid
        return None

    def find(self, text: str, include_all: bool = False) -> List[TextMatch]:
        """Find all field-id hits in ``text``. Overlapping hits keep the strongest kind."""
        if not text or not isinstance(text, str):
            return []
        best: Dict[Tuple[str, int], TextMatch] = {}
        lowered = text.lower()
        for fid, pats in self._compiled.items():
            # fast reject
            if fid not in self._regex_ids and fid.lower() not in lowered:
                continue
            for rx, kind, conf in pats:
                for m in rx.finditer(text):
                    s, e = m.start("id"), m.end("id")
                    key = (fid, s)
                    cur = best.get(key)
                    if cur is None or conf.rank > cur.confidence.rank:
                        best[key] = TextMatch(fid, s, e, kind, conf)
        out = sorted(best.values(), key=lambda t: (t.start, t.field_id))
        if include_all:
            return out
        return [t for t in out if t.confidence.rank >= self.min_confidence.rank]

    def match_contract_path(self, json_path: str) -> Optional[str]:
        """Return a field id whose contract path matches a JSON path from a loan-shaped object."""
        if not self.aliases:
            return None
        norm = normalize_contract_path(json_path)
        if not norm:
            return None
        for fid, paths in self.aliases.items():
            for p in paths:
                if norm == p:
                    return fid
                if p.count(".") >= 1 and norm.endswith("." + p):
                    return fid
        return None

    # ------------------------------------------------------------------ rewriting
    def replace(
        self, text: str, old_id: str, new_id: str, min_confidence: Optional[Confidence] = None
    ) -> Tuple[str, int]:
        """Replace references to ``old_id`` with ``new_id`` keeping the surrounding syntax.

        Only hits at or above ``min_confidence`` (default: the matcher's threshold) are touched,
        so a numeric id that also appears as a plain number is left alone.
        """
        threshold = (min_confidence or self.min_confidence).rank
        hits = [
            t for t in self.find(text, include_all=True)
            if t.field_id.lower() == normalize_field_id(old_id).lower()
            and t.confidence.rank >= threshold
        ]
        if not hits:
            return text, 0
        out, last = [], 0
        for t in hits:
            out.append(text[last:t.start])
            out.append(new_id)
            last = t.end
        out.append(text[last:])
        return "".join(out), len(hits)


def normalize_contract_path(path: str) -> str:
    """Normalise ``loan.applications[0].borrower.firstName``, ``$.a[?(@.x)].b``,
    ``/applications/*/borrower/firstName`` and ``contents.applications[2].borrower.firstName``
    to a comparable dotted lower-case form without indexes."""
    if not path:
        return ""
    p = path.strip()
    p = re.sub(r"\[[^\]]*\]", "", p)          # drop [0], [*], [?(...)], [(fieldName == ..)]
    p = p.replace("/", ".").replace("*", "")
    p = re.sub(r"^\$\.?", "", p)
    parts = [x for x in p.split(".") if x]
    if parts and parts[0].lower() == "loan":
        parts = parts[1:]
    return ".".join(parts).lower()


def iter_unique(items: Iterable[str]) -> Iterator[str]:
    seen = set()
    for i in items:
        k = i.lower()
        if k not in seen:
            seen.add(k)
            yield i
