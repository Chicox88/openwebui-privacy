import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from encompass_field_finder.app import App, SettingsStore, create_server


@pytest.fixture
def running(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))
    store.settings.output_root = str(tmp_path / "out")
    app = App(store)
    httpd = create_server(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield app, base, tmp_path
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, body=None, token=None, host=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-App-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def wait(base, token, job_id):
    for _ in range(100):
        code, j = call(base, "GET", f"/api/jobs/{job_id}", token=token)
        assert code == 200
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_ui_served_with_token(running):
    app, base, _ = running
    code, html = call(base, "GET", "/")
    assert code == 200 and b"Encompass Field Finder" in html
    assert app.token.encode() in html and b"__APP_TOKEN__" not in html


def test_security_checks(running):
    app, base, _ = running
    assert call(base, "GET", "/api/state")[0] == 403                    # no token
    assert call(base, "POST", "/api/scan", {"fields": ["X"]})[0] == 403  # no token
    assert call(base, "GET", "/", host="evil.example:80")[0] == 403      # DNS rebinding
    assert call(base, "GET", "/api/state", token=app.token)[0] == 200


def test_settings_secrets_not_exposed(running, tmp_path):
    app, base, _ = running
    code, r = call(base, "POST", "/api/settings", {"instance_id": "BE1", "username": "u", "client_id": "c",
                                                   "password": "Pa55-ZQX9", "client_secret": "S3cr3t-KJH7"}, app.token)
    assert code == 200
    s = r["settings"]
    assert s["api_configured"] and s["has_password"] and s["has_client_secret"]
    assert "Pa55-ZQX9" not in json.dumps(r) and "S3cr3t-KJH7" not in json.dumps(r)
    saved = (tmp_path / "settings.json").read_text()
    assert "Pa55-ZQX9" not in saved and "S3cr3t-KJH7" not in saved and "BE1" in saved
    call(base, "POST", "/api/settings/clear-secrets", {}, app.token)
    assert not call(base, "GET", "/api/state", token=app.token)[1]["settings"]["api_configured"]


def test_scan_preview_apply_download(running):
    app, base, tmp = running
    ex = tmp / "exports"
    ex.mkdir()
    (ex / "FieldTriggers.xml").write_text('<Rules><Rule Name="R1"><Expr>[CX.OLD] &gt; 0</Expr></Rule></Rules>')
    code, r = call(base, "POST", "/api/scan", {"fields": ["CX.OLD"], "export_paths": [str(ex)],
                                               "use_api": False}, app.token)
    assert code == 200, r
    j = wait(base, app.token, r["job_id"])
    assert j["status"] == "done", j
    refs = j["result"]["references"]
    assert refs and refs[0]["category"] == "Business Rule"
    assert j["params"]["saved_to"].endswith(".json")

    code, html = call(base, "GET", f"/api/download?job={r['job_id']}&fmt=csv")
    assert code == 200 and b"FieldTriggers.xml" in html

    body = {"scan_job": r["job_id"], "action": "replace", "mapping": {"CX.OLD": "CX.NEW"},
            "ref_ids": [refs[0]["ref_id"]], "dry_run": True}
    code, a = call(base, "POST", "/api/apply", body, app.token)
    pj = wait(base, app.token, a["job_id"])
    assert [o["status"] for o in pj["outcomes"]] == ["dry_run"]
    assert not (tmp / "out" / "modified-exports").exists()

    body["dry_run"] = False
    code, a = call(base, "POST", "/api/apply", body, app.token)
    aj = wait(base, app.token, a["job_id"])
    assert [o["status"] for o in aj["outcomes"]] == ["applied"]
    written = list((tmp / "out" / "modified-exports").rglob("FieldTriggers.xml"))
    assert written and "[CX.NEW]" in written[0].read_text()


def test_validation_errors(running):
    app, base, _ = running
    assert call(base, "POST", "/api/scan", {"fields": []}, app.token)[0] == 400
    code, r = call(base, "POST", "/api/scan", {"fields": ["X"], "export_paths": ["/nope/missing"]}, app.token)
    assert code == 400 and "do not exist" in r["error"]
    code, r = call(base, "POST", "/api/scan", {"fields": ["X"], "use_api": True}, app.token)
    assert code == 400 and "Settings" in r["error"]
    assert call(base, "POST", "/api/apply", {"scan_job": "nope", "action": "replace"}, app.token)[0] == 400


def test_load_saved_results(running):
    app, base, _ = running
    saved = {"field_ids": ["CX.A"], "references": [], "coverage": [], "field_info": {}}
    code, r = call(base, "POST", "/api/load-results", {"result": saved}, app.token)
    assert code == 200
    j = wait(base, app.token, r["job_id"])
    assert j["result"]["field_ids"] == ["CX.A"]
