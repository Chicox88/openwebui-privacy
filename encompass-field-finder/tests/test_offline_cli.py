import io
import json
import zipfile

from encompass_field_finder import FieldDependencyFinder
from encompass_field_finder.cli import main
from encompass_field_finder.models import ChangeSupport


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def build_exports(root):
    (root / "BusinessRules").mkdir()
    (root / "BusinessRules" / "FieldDataEntry.xml").write_text(
        '<?xml version="1.0"?>\n<Rules>\n  <Rule Name="Require score">\n'
        '    <Condition>[CX.OLD] &lt;&gt; ""</Condition>\n    <FieldID>4000</FieldID>\n'
        "  </Rule>\n</Rules>\n", encoding="utf-8")
    (root / "codebase").mkdir()
    (root / "codebase" / "Form1.cs").write_text(
        'var s = Loan.Fields["CX.OLD"].Value; // 4000 not a field here\n', encoding="utf-8")
    inner_docx = _zip({"word/document.xml": "<w:t>Score: [CX.OLD]</w:t>"})
    pkg = _zip({"forms/LOWorksheet.emfrm": "<Form><TextBox FieldID=\"CX.OLD\"/></Form>",
                "letters/Welcome.docx": inner_docx,
                "ddm/FieldRules.json": json.dumps({"rules": [{"name": "r1", "targetField": "CX.OLD"}]})})
    (root / "export.empkg").write_bytes(pkg)
    (root / "plugin.dll").write_bytes(b"\x00MZ\x90" + "CX.OLD".encode("utf-16-le") + b"\x00\x00junk")
    (root / "UTF16Report.rpt.xml").write_bytes("<Report><Column>Fields.CX.OLD</Column></Report>".encode("utf-16"))


def test_offline_scan_and_rewrite(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    exports.mkdir()
    build_exports(exports)
    finder = FieldDependencyFinder(export_paths=[str(exports)])
    res = finder.scan(["CX.OLD"])
    by_cat = {}
    for r in res.references:
        by_cat.setdefault(r.category, []).append(r)
    assert "Business Rule" in by_cat
    rule = by_cat["Business Rule"][0]
    assert rule.line == 4 and "Require score" in (rule.json_path or "")
    assert "Custom Input Form (desktop)" in by_cat
    assert "Dynamic Data Management" in by_cat
    assert "Letter / Email Templates" in by_cat
    assert "Report" in by_cat
    dll = [r for r in res.references if r.file_path.endswith("plugin.dll")]
    assert dll and dll[0].change_support == ChangeSupport.MANUAL
    # no API: business rules covered by export -> not listed as manual_only
    gaps = {c.category for c in res.coverage if c.status == "manual_only"}
    assert "Business Rule" not in gaps and "Dashboard" in gaps

    monkeypatch.chdir(tmp_path)
    out = finder.apply(res, "replace", mapping={"CX.OLD": "CX.NEW"}, dry_run=False, output_dir="mod")
    assert any(o.status == "applied" for o in out)
    mod = tmp_path / "mod" / "exports"
    xml = (mod / "BusinessRules" / "FieldDataEntry.xml").read_text(encoding="utf-8")
    assert "[CX.NEW]" in xml and "<FieldID>4000</FieldID>" in xml
    z = zipfile.ZipFile(mod / "export.empkg")
    assert 'FieldID="CX.NEW"' in z.read("forms/LOWorksheet.emfrm").decode()
    inner = zipfile.ZipFile(io.BytesIO(z.read("letters/Welcome.docx")))
    assert "[CX.NEW]" in inner.read("word/document.xml").decode()
    assert json.loads(z.read("ddm/FieldRules.json"))["rules"][0]["targetField"] == "CX.NEW"
    rpt = (mod / "UTF16Report.rpt.xml").read_bytes().decode("utf-16")
    assert "Fields.CX.NEW" in rpt
    # originals untouched
    assert "[CX.OLD]" in (exports / "BusinessRules" / "FieldDataEntry.xml").read_text(encoding="utf-8")


def test_cli_offline_scan_and_apply(tmp_path, monkeypatch, capsys):
    exports = tmp_path / "exports"
    exports.mkdir()
    build_exports(exports)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "fields.txt").write_text("CX.OLD  # the old one\n", encoding="utf-8")
    rc = main(["scan", "--offline", "--export", str(exports), "--fields-file", "fields.txt",
               "--format", "html", "-o", "report.html", "-q"])
    assert rc == 0
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "FieldDataEntry.xml" in html and "NOT SCANNED" in html
    data = json.loads((tmp_path / "efinder-results.json").read_text(encoding="utf-8"))
    assert data["references"]

    rc = main(["apply", "--offline", "--export", str(exports), "--replace", "CX.OLD=CX.NEW", "-q"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and not (tmp_path / "efinder-modified-exports").exists()

    rc = main(["apply", "--offline", "--export", str(exports), "--replace", "CX.OLD=CX.NEW",
               "--yes", "--no-confirm", "-q"])
    assert (tmp_path / "efinder-modified-exports").exists()

    for fmt in ("text", "csv", "md", "json"):
        assert main(["scan", "--offline", "--export", str(exports), "-f", "CX.OLD", "--format", fmt,
                     "-o", f"r.{fmt}", "-q"]) == 0
