"""Walk arbitrary JSON looking for field references, and edit JSON by path."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional, Union

from .matcher import FieldMatcher, TextMatch
from .models import Confidence, MatchKind

PathPart = Union[str, int]


@dataclass
class JsonHit:
    field_id: str
    path: List[PathPart]
    kind: MatchKind
    confidence: Confidence
    snippet: str
    text_match: Optional[TextMatch] = None   # set for hits inside a larger string

    @property
    def path_str(self) -> str:
        return format_path(self.path)


def format_path(path: List[PathPart]) -> str:
    out = "$"
    for p in path:
        if isinstance(p, int):
            out += f"[{p}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p):
            out += f".{p}"
        else:
            out += "[" + json.dumps(p) + "]"
    return out


def parse_path(path: str) -> List[PathPart]:
    """Inverse of :func:`format_path`."""
    if not path.startswith("$"):
        raise ValueError(f"not a json path: {path}")
    i, out, s = 1, [], path
    while i < len(s):
        if s[i] == ".":
            m = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)", s[i:])
            if not m:
                raise ValueError(f"bad path {path!r} at {i}")
            out.append(m.group(1))
            i += m.end()
        elif s[i] == "[":
            if s[i + 1] == '"':
                dec = json.JSONDecoder()
                val, end = dec.raw_decode(s, i + 1)
                out.append(val)
                i = end + 1
            else:
                j = s.index("]", i)
                out.append(int(s[i + 1:j]))
                i = j + 1
        else:
            raise ValueError(f"bad path {path!r} at {i}")
    return out


def maybe_decode_base64(value: str) -> Optional[str]:
    """CDOs and some settings carry base64 text; decode it if it looks like readable text."""
    if len(value) < 16 or len(value) % 4 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
        return None
    try:
        raw = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return None
    for enc in ("utf-8", "utf-16"):
        try:
            txt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in txt)
        if txt and printable / len(txt) > 0.95:
            return txt
    return None


def walk(
    obj: Any,
    matcher: FieldMatcher,
    use_contract_paths: bool = False,
    decode_base64: bool = False,
    _path: Optional[List[PathPart]] = None,
) -> Iterator[JsonHit]:
    """Yield every field reference inside a JSON-like object."""
    path = _path or []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = path + [k]
            fid = matcher.exact(k) if isinstance(k, str) else None
            if fid:
                yield JsonHit(fid, kp, MatchKind.KEY, Confidence.HIGH, f"key {k!r}")
            if use_contract_paths and not isinstance(v, (dict, list)):
                cfid = matcher.match_contract_path(format_path(kp))
                if cfid:
                    yield JsonHit(
                        cfid, kp, MatchKind.CONTRACT_PATH, Confidence.HIGH,
                        f"{format_path(kp)} = {str(v)[:80]}",
                    )
            yield from walk(v, matcher, use_contract_paths, decode_base64, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, matcher, use_contract_paths, decode_base64, path + [i])
    elif isinstance(obj, str):
        fid = matcher.exact(obj)
        if fid:
            yield JsonHit(fid, path, MatchKind.EXACT_VALUE, Confidence.HIGH, f"= {obj!r}")
            return
        for tm in matcher.find(obj):
            yield JsonHit(tm.field_id, path, tm.kind, tm.confidence, tm.snippet(obj), tm)
        if use_contract_paths:
            # webhook filters / paths stored as values: "/applications/*/borrower/firstName"
            if obj.startswith(("/", "$.", "loan.")):
                cfid = matcher.match_contract_path(obj)
                if cfid:
                    yield JsonHit(cfid, path, MatchKind.CONTRACT_PATH, Confidence.HIGH, f"= {obj!r}")
        if decode_base64:
            txt = maybe_decode_base64(obj)
            if txt:
                for tm in matcher.find(txt):
                    yield JsonHit(
                        tm.field_id, path, tm.kind, tm.confidence,
                        "[base64] " + tm.snippet(txt), tm,
                    )


# ---------------------------------------------------------------------- editing
def get_at(obj: Any, path: List[PathPart]) -> Any:
    cur = obj
    for p in path:
        cur = cur[p]
    return cur


def set_at(obj: Any, path: List[PathPart], value: Any) -> None:
    if not path:
        raise ValueError("cannot replace the root")
    parent = get_at(obj, path[:-1])
    parent[path[-1]] = value


def delete_at(obj: Any, path: List[PathPart]) -> None:
    parent = get_at(obj, path[:-1])
    del parent[path[-1]]


def rename_key(obj: Any, path: List[PathPart], new_key: str) -> None:
    parent = get_at(obj, path[:-1])
    old = path[-1]
    items = list(parent.items())
    parent.clear()
    for k, v in items:
        parent[new_key if k == old else k] = v


@dataclass
class EditResult:
    obj: Any
    changed: bool
    before: str
    after: str
    message: str


def apply_edit(
    obj: Any,
    path_str: str,
    kind: MatchKind,
    matcher: FieldMatcher,
    old_id: str,
    action: str,
    new_id: Optional[str] = None,
    base64_value: bool = False,
) -> EditResult:
    """Apply a replace/remove to a deep copy of ``obj`` at ``path_str``.

    Removal rules (kept deliberately conservative):

    * value exactly equal to the field id inside a list -> the list element is removed
    * value exactly equal to the field id inside an object that is itself a list element
      (``[{"fieldId": "X", ...}]``) -> that whole entry is removed
    * object key equal to the field id -> the key is removed
    * anything inside an expression / free text -> refused (would break the expression)
    """
    data = copy.deepcopy(obj)
    path = parse_path(path_str)
    if kind == MatchKind.KEY:
        if action == "replace":
            rename_key(data, path, new_id)
            return EditResult(data, True, f"key {path[-1]!r}", f"key {new_id!r}", "renamed key")
        delete_at(data, path)
        return EditResult(data, True, f"key {path[-1]!r}", "(removed)", "removed key")

    value = get_at(data, path)
    if not isinstance(value, str):
        return EditResult(data, False, repr(value), repr(value), "target is not a string")

    if kind == MatchKind.EXACT_VALUE:
        if action == "replace":
            set_at(data, path, new_id)
            return EditResult(data, True, value, new_id, "replaced value")
        # remove
        if path and isinstance(path[-1], int):
            delete_at(data, path)
            return EditResult(data, True, value, "(list element removed)", "removed list element")
        if len(path) >= 2 and isinstance(path[-2], int):
            entry = get_at(data, path[:-1])
            delete_at(data, path[:-1])
            return EditResult(
                data, True, json.dumps(entry)[:300], "(entry removed)",
                "removed the list entry that pointed at the field",
            )
        return EditResult(
            data, False, value, value,
            "value is a single property; removing it could invalidate the object - change manually",
        )

    if kind == MatchKind.CONTRACT_PATH:
        return EditResult(data, False, value, value, "contract-path references must be changed manually")

    # text / expression hit
    text = value
    if base64_value:
        decoded = maybe_decode_base64(value)
        if decoded is None:
            return EditResult(data, False, value[:200], value[:200], "could not decode base64 value")
        text = decoded
    if action != "replace":
        return EditResult(
            data, False, text[:300], text[:300],
            "field is used inside an expression/text; removing it automatically would break the "
            "expression - edit manually or use replace",
        )
    new_text, n = matcher.replace(text, old_id, new_id)
    if n == 0:
        return EditResult(data, False, text[:300], text[:300], "reference no longer present")
    stored = base64.b64encode(new_text.encode("utf-8")).decode("ascii") if base64_value else new_text
    set_at(data, path, stored)
    return EditResult(data, True, text, new_text, f"replaced {n} occurrence(s)")
