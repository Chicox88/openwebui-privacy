"""Core data structures shared by every scanner, the CLI, the web UI and the reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Confidence(str, Enum):
    """How sure we are that a hit is a real reference to the field."""

    HIGH = "high"      # exact value/key, [ID] calc syntax, Fields.ID accessor, search index hit
    MEDIUM = "medium"  # quoted token / XML text node equal to the ID
    LOW = "low"        # bare token inside free text (noisy for numeric IDs)

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


class MatchKind(str, Enum):
    EXACT_VALUE = "exact_value"        # a JSON/XML value is exactly the field id
    KEY = "key"                        # a JSON object key is exactly the field id
    CALC_EXPRESSION = "calc_expression"  # [ID] / [#ID] in a calculation or rule expression
    CODE_ACCESSOR = "code_accessor"    # Fields["ID"], Fields.ID, GetField("ID") ...
    QUOTED = "quoted"                  # "ID" or 'ID' inside a larger text
    XML_TEXT = "xml_text"              # <tag>ID</tag>
    BARE_TOKEN = "bare_token"          # ID surrounded by non-identifier characters
    CONTRACT_PATH = "contract_path"    # loan JSON contract path that maps to the field id
    SEARCH_INDEX = "search_index"      # returned by an Encompass search API for the field id
    DEFINITION = "definition"          # the field's own definition (informational)
    LOAN_DATA = "loan_data"            # loans hold a value for the field (informational)


class ChangeSupport(str, Enum):
    AUTO = "auto"                  # replace and remove can be applied through the API / file rewrite
    REPLACE_ONLY = "replace_only"  # replace can be applied; removal needs a human decision
    MANUAL = "manual"              # must be changed by hand in Encompass (instructions included)
    NONE = "none"                  # informational, nothing to change


@dataclass
class Reference:
    """One place in Encompass that references (or depends on) a field id."""

    field_id: str
    category: str              # e.g. "Custom Field Calculation", "Business Rule", "Input Form (web)"
    item_type: str             # finer-grained type, e.g. "Enhanced Condition Template"
    item_name: str             # human name of the object that holds the reference
    location: str              # where to find it in the Encompass UI
    source: str                # key of the scanner that produced it
    match_kind: MatchKind
    confidence: Confidence
    snippet: str = ""          # text around the hit
    item_id: Optional[str] = None
    api: Optional[str] = None  # "GET /encompass/v3/..." to fetch the object
    json_path: Optional[str] = None  # path inside the JSON object (REST) or element path (XML)
    file_path: Optional[str] = None  # offline export file (archive members use "a.zip!/b.xml")
    line: Optional[int] = None
    change_support: ChangeSupport = ChangeSupport.MANUAL
    change_notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    ref_id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.match_kind, str):
            self.match_kind = MatchKind(self.match_kind)
        if isinstance(self.confidence, str):
            self.confidence = Confidence(self.confidence)
        if isinstance(self.change_support, str):
            self.change_support = ChangeSupport(self.change_support)
        if not self.ref_id:
            key = "|".join(
                str(x)
                for x in (
                    self.field_id, self.source, self.item_id or self.item_name,
                    self.json_path, self.file_path, self.line, self.match_kind.value, self.snippet,
                )
            )
            self.ref_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["match_kind"] = self.match_kind.value
        d["confidence"] = self.confidence.value
        d["change_support"] = self.change_support.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Reference":
        return cls(**d)


@dataclass
class CoverageEntry:
    """What a scanner was able (or not able) to look at."""

    source: str
    category: str
    status: str            # ok | not_available | forbidden | error | skipped | manual_only
    items_scanned: int = 0
    note: str = ""


@dataclass
class ScanResult:
    field_ids: List[str]
    references: List[Reference] = field(default_factory=list)
    coverage: List[CoverageEntry] = field(default_factory=list)
    field_info: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field_ids": self.field_ids,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "field_info": self.field_info,
            "coverage": [asdict(c) for c in self.coverage],
            "references": [r.to_dict() for r in self.references],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScanResult":
        return cls(
            field_ids=list(d.get("field_ids", [])),
            references=[Reference.from_dict(r) for r in d.get("references", [])],
            coverage=[CoverageEntry(**c) for c in d.get("coverage", [])],
            field_info=dict(d.get("field_info", {})),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
        )

    @classmethod
    def load(cls, path: str) -> "ScanResult":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def by_field(self) -> Dict[str, List[Reference]]:
        out: Dict[str, List[Reference]] = {f: [] for f in self.field_ids}
        for r in self.references:
            out.setdefault(r.field_id, []).append(r)
        return out


@dataclass(eq=False)  # hashed by identity
class ChangeRequest:
    """A requested modification for one reference."""

    ref: Reference
    action: str                 # "replace" | "remove"
    new_field_id: Optional[str] = None


@dataclass
class ChangeOutcome:
    ref_id: str
    field_id: str
    action: str
    status: str                 # applied | dry_run | manual | skipped | failed
    message: str = ""
    before: Optional[str] = None
    after: Optional[str] = None
    backup_path: Optional[str] = None
