import base64
import json

import pytest

from encompass_field_finder import EncompassClient, EncompassConfig, FieldDependencyFinder
from encompass_field_finder.models import ChangeSupport, MatchKind

from fake_api import FakeSession


def make_api():
    s = FakeSession()
    custom_fields = [
        {"id": "CX.TOTAL", "description": "total", "format": "DECIMAL_2", "isCalculatedField": True,
         "calculation": "[CX.OLD] + [1109]", "contractPath": "loan.customFields[(fieldName == 'CX.TOTAL')].value"},
        {"id": "CX.OLD.AUDIT", "format": "AUDIT", "audit": {"fieldId": "CX.OLD", "data": "UserId"}},
        {"id": "CX.OLD", "description": "the old field", "format": "STRING"},
        {"id": "CX.UNRELATED", "calculation": "[4000]"},
    ]
    state = {"custom_fields": custom_fields, "patched": None, "templates": None}

    def cf_get(path, params, body):
        ids = params.get("ids")
        if ids:
            wanted = set(ids.split(","))
            return 200, [c for c in state["custom_fields"] if c["id"] in wanted], {}
        return 200, state["custom_fields"], {}

    def cf_patch(path, params, body):
        assert params == {"action": "update"}
        state["patched"] = body
        return 200, body, {}

    s.route("GET", "/encompass/v3/settings/loan/customFields", cf_get)
    s.route("PATCH", "/encompass/v3/settings/loan/customFields", cf_patch)
    s.route("GET", "/encompass/v3/loanPipeline/canonicalFields",
            [{"canonicalName": "Fields.CX.OLD", "fieldId": "CX.OLD", "description": "old", "isAudited": False}])
    s.route("GET", "/encompass/v3/schemas/loan/standardFields", [])

    def search(path, params, body):
        assert params == {"key": "form_fields"}
        fid = body["query"]["bool"]["must"][0]["term"]["fieldId"]
        if fid == "CX.OLD":
            return 200, {"results": [{"fieldId": "CX.OLD", "type": "Custom", "formName": "LO Worksheet",
                                      "id": "f1", "enabled": True}]}, {}
        return 200, {"results": []}, {}

    s.route("POST", "/encwsearch/v1/search", search)

    templates = [{"id": "t1", "title": "Need flood cert", "internalDescription": "Required when [CX.OLD] = Y",
                  "createdBy": {"entityId": "admin"}}]

    def tmpl_list(path, params, body):
        assert params["view"] == "full"
        return 200, templates if params["start"] == 1 else [], {}

    s.route("GET", "/encompass/v3/settings/loan/conditions/templates", tmpl_list)
    s.route("GET", "/encompass/v3/settings/loan/conditions/templates/t1", lambda p, q, b: (200, templates[0], {}))

    def tmpl_patch(path, params, body):
        state["templates"] = (params, body)
        return 200, body, {}

    s.route("PATCH", "/encompass/v3/settings/loan/conditions/templates", tmpl_patch)

    cdo = base64.b64encode(json.dumps({"watchFields": ["CX.OLD", "4000"]}).encode()).decode()
    s.route("GET", "/encompass/v1/company/customObjects", ["plugin-config"])
    s.route("GET", "/encompass/v1/company/customObjects/plugin-config",
            {"name": "plugin-config", "dataObject": cdo})
    s.route("GET", "/webhook/v1/subscriptions",
            [{"subscriptionId": "s1", "endpoint": "https://hook", "filters": {"attributes": ["/customFields/x"]},
              "resource": "Loan", "events": ["change"]}])
    s.route("GET", "/encompass/v3/settings/templates/loanProgram/folders",
            lambda p, q, b: (200, {"contents": [{"entityType": "LoanProgram", "entityName": "Conv 30",
                                                 "entityPath": "Public:\\Conv 30"}]}, {})
            if q.get("path") == "Public:\\" else (404, None, {}))
    s.route("GET", "/encompass/v3/settings/templates/loanProgram/items",
            {"templateName": "Conv 30", "contents": {"customFields": [{"fieldName": "CX.OLD", "stringValue": "Y"}]}})
    s.route("POST", "/encompass/v1/loanPipeline", lambda p, q, b: (200, [], {"X-Total-Count": "42"}))
    return s, state


@pytest.fixture
def api():
    s, state = make_api()
    cfg = EncompassConfig(base_url="https://api.test", instance_id="BE1", username="u", password="p",
                          client_id="c", client_secret="s")
    return EncompassClient(cfg, session=s), s, state


def test_scan_finds_references_across_sources(api):
    client, session, _ = api
    finder = FieldDependencyFinder(client=client, include_loan_usage=True, workers=1)
    res = finder.scan(["CX.OLD"])
    cats = {(r.category, r.item_name) for r in res.references}
    assert ("Custom Field Calculation", "CX.TOTAL") in cats
    assert ("Custom Field (Audit)", "CX.OLD.AUDIT") in cats
    assert ("Input Form (Encompass web)", "LO Worksheet") in cats
    assert ("Enhanced Conditions", "Need flood cert") in cats
    assert ("Custom Data Objects", "plugin-config") in cats
    assert ("Reporting Database", "Fields.CX.OLD") in cats
    assert ("Field Definition", "CX.OLD") in cats
    assert ("Loan Templates", "Conv 30") in cats
    loan = [r for r in res.references if r.category == "Loan Data"][0]
    assert loan.item_name.startswith("42")
    # unrelated custom field must not show up, and the field's own id is not a "reference"
    assert not any(r.item_name == "CX.UNRELATED" for r in res.references)
    assert not any(r.item_name == "CX.OLD" and r.category == "Custom Field Calculation" for r in res.references)
    # coverage: missing endpoints reported as not available; no-API areas called out
    statuses = {(c.source, c.status) for c in res.coverage}
    assert ("rest:milestones", "not_available") in statuses
    assert any(c.status == "manual_only" and c.category == "Business Rule" for c in res.coverage)
    assert session.token_requests == 1


def test_apply_dry_run_then_real(api, tmp_path):
    client, session, state = api
    finder = FieldDependencyFinder(client=client, workers=1, include_templates=False)
    res = finder.scan(["CX.OLD"])

    dry = finder.apply(res, "replace", mapping={"CX.OLD": "CX.NEW"}, dry_run=True,
                       backup_dir=str(tmp_path / "bk"))
    assert state["patched"] is None and state["templates"] is None
    assert any(o.status == "dry_run" for o in dry)

    out = finder.apply(res, "replace", mapping={"CX.OLD": "CX.NEW"}, dry_run=False,
                       backup_dir=str(tmp_path / "bk"))
    by_status = {o.status for o in out}
    assert "applied" in by_status
    # custom field calc updated, read-only properties stripped (one PATCH per custom field)
    sent = [c for (m, p, q, b) in session.calls if m == "PATCH" and p.endswith("customFields") for c in b]
    calc = next(c for c in sent if c["id"] == "CX.TOTAL")
    assert calc["calculation"] == "[CX.NEW] + [1109]"
    assert "contractPath" not in calc and "isCalculatedField" not in calc
    audit = next(c for c in sent if c["id"] == "CX.OLD.AUDIT")
    assert audit["audit"]["fieldId"] == "CX.NEW"
    # condition template updated through PATCH ?action=Update with list body
    params, body = state["templates"]
    assert params == {"action": "Update"}
    assert body[0]["internalDescription"] == "Required when [CX.NEW] = Y"
    assert "createdBy" not in body[0]
    # web form and RDB entries are manual
    manual = [o for o in out if o.status == "manual"]
    assert any("LO Worksheet" in o.message or "Form Builder" in o.message for o in manual)
    assert list((tmp_path / "bk").iterdir())


def test_remove_is_conservative(api, tmp_path):
    client, session, state = api
    finder = FieldDependencyFinder(client=client, workers=1, include_templates=False)
    res = finder.scan(["CX.OLD"])
    out = finder.apply(res, "remove", dry_run=False, backup_dir=str(tmp_path / "bk"))
    assert state["patched"] is None            # calculations are never auto-edited on remove
    assert state["templates"] is None
    assert all(o.status in ("manual", "applied", "dry_run", "failed") for o in out)
    assert any(o.status == "manual" for o in out)


def test_change_support_flags(api):
    client, _, _ = api
    res = FieldDependencyFinder(client=client, workers=1, include_templates=False).scan(["CX.OLD"])
    calc = next(r for r in res.references if r.item_name == "CX.TOTAL")
    assert calc.change_support == ChangeSupport.REPLACE_ONLY and calc.match_kind == MatchKind.CALC_EXPRESSION
    web = next(r for r in res.references if r.category == "Input Form (Encompass web)")
    assert web.change_support == ChangeSupport.MANUAL
