import base64

from encompass_field_finder.matcher import FieldMatcher, normalize_contract_path
from encompass_field_finder.models import Confidence, MatchKind
from encompass_field_finder.walker import apply_edit, format_path, parse_path, walk


def kinds(matches):
    return [(m.field_id, m.kind, m.confidence) for m in matches]


def test_calc_and_accessor_syntax_are_high_confidence():
    m = FieldMatcher(["4000", "CX.LOAN.SCORE"])
    text = 'iif([4000] = "", [#CX.LOAN.SCORE], Loan.Fields["4000"]) + Fields.CX.LOAN.SCORE'
    got = kinds(m.find(text))
    assert ("4000", MatchKind.CALC_EXPRESSION, Confidence.HIGH) in got
    assert ("CX.LOAN.SCORE", MatchKind.CALC_EXPRESSION, Confidence.HIGH) in got
    assert ("4000", MatchKind.CODE_ACCESSOR, Confidence.HIGH) in got
    assert ("CX.LOAN.SCORE", MatchKind.CODE_ACCESSOR, Confidence.HIGH) in got


def test_numeric_bare_tokens_are_filtered_by_default():
    m = FieldMatcher(["4000"])
    assert m.find("the fee is 4000 dollars") == []
    assert len(m.find("the fee is 4000 dollars", include_all=True)) == 1
    assert m.find("price 4000.50") == []  # never matched, not even low


def test_ids_do_not_match_longer_ids():
    m = FieldMatcher(["CX.A", "1172"], min_confidence=Confidence.LOW)
    assert m.find("[CX.AB] [CX.A.B] [11720] CX.A2") == []
    assert len(m.find("value of CX.A. Done")) == 1


def test_json_style_and_xml_style():
    m = FieldMatcher(["CX.FOO"])
    assert kinds(m.find('{"fieldId": "CX.FOO"}'))[0][1] == MatchKind.CODE_ACCESSOR
    assert kinds(m.find("<FieldID>CX.FOO</FieldID>"))[0][1] == MatchKind.XML_TEXT
    assert kinds(m.find('<Rule FieldID="CX.FOO" />'))[0][1] == MatchKind.CODE_ACCESSOR


def test_short_numeric_quoted_is_low():
    m = FieldMatcher(["12"])
    assert m.find('x = "12"') == []
    assert m.find("[12] + 1")[0].confidence == Confidence.HIGH


def test_replace_keeps_syntax_and_ignores_plain_numbers():
    m = FieldMatcher(["4000"])
    out, n = m.replace('[4000] + Fields["4000"] + 4000', "4000", "CX.NEW")
    assert out == '[CX.NEW] + Fields["CX.NEW"] + 4000'
    assert n == 2


def test_regex_ids():
    m = FieldMatcher(["re:FL01\\d\\d"])
    assert len(m.find("[FL0102] and [FL0199]")) == 2
    assert m.exact("FL0105") == "re:FL01\\d\\d"


def test_contract_paths():
    assert normalize_contract_path("loan.applications[0].borrower.firstName") == "applications.borrower.firstname"
    assert normalize_contract_path("/applications/*/borrower/firstName") == "applications.borrower.firstname"
    m = FieldMatcher(["4000"], aliases={"4000": ["loan.applications[0].borrower.firstName"]})
    assert m.match_contract_path("$.contents.applications[1].borrower.firstName") == "4000"
    assert m.match_contract_path("$.contents.applications[1].coborrower.firstName") is None


def test_walk_finds_keys_values_and_text():
    m = FieldMatcher(["CX.FOO"])
    obj = {"rules": [{"fieldId": "CX.FOO", "expr": "[CX.FOO] > 1"}], "CX.FOO": 1}
    hits = {(h.path_str, h.kind) for h in walk(obj, m)}
    assert ("$.rules[0].fieldId", MatchKind.EXACT_VALUE) in hits
    assert ("$.rules[0].expr", MatchKind.CALC_EXPRESSION) in hits
    assert ('$["CX.FOO"]', MatchKind.KEY) in hits


def test_walk_base64():
    m = FieldMatcher(["CX.FOO"])
    payload = base64.b64encode(b'{"watch": ["CX.FOO", "4000"]}').decode()
    hits = list(walk({"dataObject": payload}, m, decode_base64=True))
    assert hits and hits[0].snippet.startswith("[base64]")
    er = apply_edit({"dataObject": payload}, "$.dataObject", hits[0].kind, m, "CX.FOO", "replace",
                    "CX.BAR", base64_value=True)
    assert base64.b64decode(er.obj["dataObject"]).decode() == '{"watch": ["CX.BAR", "4000"]}'


def test_path_round_trip():
    p = ["a", 0, "weird key", "b_c", 12]
    assert parse_path(format_path(p)) == p


def test_apply_edit_remove_semantics():
    m = FieldMatcher(["CX.FOO"])
    obj = {"list": ["CX.FOO", "X"], "entries": [{"fieldId": "CX.FOO", "v": 1}, {"fieldId": "Y"}],
           "single": {"fieldId": "CX.FOO"}, "expr": "[CX.FOO]+1"}
    r = apply_edit(obj, "$.list[0]", MatchKind.EXACT_VALUE, m, "CX.FOO", "remove")
    assert r.changed and r.obj["list"] == ["X"]
    r = apply_edit(obj, "$.entries[0].fieldId", MatchKind.EXACT_VALUE, m, "CX.FOO", "remove")
    assert r.changed and r.obj["entries"] == [{"fieldId": "Y"}]
    r = apply_edit(obj, "$.single.fieldId", MatchKind.EXACT_VALUE, m, "CX.FOO", "remove")
    assert not r.changed
    r = apply_edit(obj, "$.expr", MatchKind.CALC_EXPRESSION, m, "CX.FOO", "remove")
    assert not r.changed
    r = apply_edit(obj, "$.expr", MatchKind.CALC_EXPRESSION, m, "CX.FOO", "replace", "CX.BAR")
    assert r.changed and r.obj["expr"] == "[CX.BAR]+1"
    assert obj["expr"] == "[CX.FOO]+1"  # original untouched
