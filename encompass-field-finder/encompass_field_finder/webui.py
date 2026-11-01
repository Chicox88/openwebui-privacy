"""Small local web UI (standard library only): enter field ids, scan, review, select and apply.

Binds to 127.0.0.1 by default. Every POST must carry a per-process random token to stop other
web pages in your browser from submitting forms to it.
"""

from __future__ import annotations

import html
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .client import EncompassConfig
from .models import ScanResult
from .report import _CSS, _JS, body_html, outcomes_to_text, render

_STATE: Dict[str, Any] = {"result": None, "args": None, "cfg": None, "exports": [], "lock": threading.Lock()}
_TOKEN = secrets.token_urlsafe(24)


def _page(title: str, body: str) -> bytes:
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<style>{_CSS} textarea,input[type=text]{{width:100%;max-width:640px;font-family:ui-monospace,monospace;"
            f"padding:6px;border:1px solid #d0d7de;border-radius:6px}} button{{padding:6px 14px;border-radius:6px;"
            f"border:1px solid #1f883d;background:#1f883d;color:#fff;cursor:pointer}} label{{display:block;margin:8px 0 2px}}"
            f"fieldset{{border:1px solid #d0d7de;border-radius:8px;margin:12px 0;padding:10px 14px}}</style>"
            f"<script>{_JS}</script></head><body><h1>{html.escape(title)}</h1>"
            f"<p><a href='/'>New scan</a></p>{body}</body></html>").encode("utf-8")


def _form() -> str:
    cfg: Optional[EncompassConfig] = _STATE["cfg"]
    api = (f"Connected to <b>{html.escape(cfg.base_url)}</b> as <b>{html.escape(cfg.username or 'token')}</b>"
           if cfg else "<b>No API credentials</b> - only offline exports will be scanned "
                       "(set ENCOMPASS_* in .env and restart to enable the API).")
    exports = "\n".join(_STATE["exports"])
    return f"""
<p>{api}</p>
<form method='post' action='/scan'>
<input type='hidden' name='token' value='{_TOKEN}'>
<label>Field ids (comma or newline separated; prefix <code>re:</code> for a regex)</label>
<textarea name='fields' rows='4' placeholder='CX.MY.FIELD, 4000, 1172'></textarea>
<label>Exported files / folders to scan (one local path per line, optional)</label>
<textarea name='exports' rows='3'>{html.escape(exports)}</textarea>
<label><input type='checkbox' name='templates' checked> Crawl loan templates (slower)</label>
<label><input type='checkbox' name='loan_usage'> Count loans holding a value (pipeline)</label>
<label>Minimum confidence <select name='min_conf'><option>medium</option><option>high</option><option>low</option></select></label>
<p><button type='submit'>Scan</button></p></form>"""


def _apply_form(result: ScanResult) -> str:
    return f"""
<fieldset><legend>Change selected references</legend>
<p>Tick references in the tables (only <i>auto</i> / <i>replace_only</i> can be ticked), or leave all
unticked to target every changeable reference.</p>
<label><input type='radio' name='action' value='replace' checked> Replace with new field id(s)</label>
<textarea name='mapping' rows='2' placeholder='{html.escape(result.field_ids[0])}=CX.NEW.FIELD'></textarea>
<label><input type='radio' name='action' value='remove'> Remove the reference (only where it is safe)</label>
<label><input type='checkbox' name='dry_run' checked> Dry run (preview only)</label>
<input type='hidden' name='token' value='{_TOKEN}'>
<p><button type='submit'>Preview / apply</button></p></fieldset>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "efinder"

    def log_message(self, fmt: str, *a: Any) -> None:  # quieter
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", status: int = 200,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _form_data(self) -> Dict[str, List[str]]:
        n = int(self.headers.get("Content-Length") or 0)
        return urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self._send(_page("Encompass Field Dependency Finder", _form()))
        elif url.path == "/download" and _STATE["result"] is not None:
            fmt = urllib.parse.parse_qs(url.query).get("fmt", ["json"])[0]
            if fmt not in ("json", "csv", "html", "md"):
                fmt = "json"
            ctype = {"json": "application/json", "csv": "text/csv", "html": "text/html", "md": "text/markdown"}[fmt]
            self._send(render(_STATE["result"], fmt).encode("utf-8"), f"{ctype}; charset=utf-8",
                       extra={"Content-Disposition": f"attachment; filename=efinder-report.{fmt}"})
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:  # noqa: N802
        data = self._form_data()
        if data.get("token", [""])[0] != _TOKEN:
            self._send(b"invalid token", "text/plain", 403)
            return
        if self.path == "/scan":
            self._scan(data)
        elif self.path == "/apply":
            self._apply(data)
        else:
            self._send(b"not found", "text/plain", 404)

    def _finder(self, exports: List[str], templates: bool = True, loan_usage: bool = False,
                min_conf: str = "medium"):
        from .finder import FieldDependencyFinder

        args = _STATE["args"]
        return FieldDependencyFinder(
            config=_STATE["cfg"], export_paths=exports, catalog_path=getattr(args, "catalog", None),
            include_undocumented=getattr(args, "include_undocumented", False),
            include_loan_usage=loan_usage, include_templates=templates, min_confidence=min_conf,
            workers=getattr(args, "workers", 4))

    def _scan(self, data: Dict[str, List[str]]) -> None:
        fields = [f.strip() for f in data.get("fields", [""])[0].replace(",", "\n").splitlines() if f.strip()]
        exports = [p.strip() for p in data.get("exports", [""])[0].splitlines() if p.strip()]
        if not fields:
            self._send(_page("Error", "<p>Enter at least one field id.</p>"), status=400)
            return
        with _STATE["lock"]:
            _STATE["exports"] = exports
            finder = self._finder(exports, "templates" in data, "loan_usage" in data,
                                  data.get("min_conf", ["medium"])[0])
            if finder.client is None and not exports:
                self._send(_page("Error", "<p>No API credentials and no export paths - nothing to scan.</p>"), status=400)
                return
            result = finder.scan(fields)
            _STATE["result"] = result
        dl = " ".join(f"<a href='/download?fmt={f}'>{f.upper()}</a>" for f in ("html", "csv", "json", "md"))
        body = (f"<p>Download: {dl}</p><form method='post' action='/apply'>"
                f"{_apply_form(result)}{body_html(result, selectable=True)}</form>")
        self._send(_page("Scan results", body))

    def _apply(self, data: Dict[str, List[str]]) -> None:
        result: Optional[ScanResult] = _STATE["result"]
        if result is None:
            self._send(_page("Error", "<p>Run a scan first.</p>"), status=400)
            return
        action = data.get("action", ["replace"])[0]
        mapping: Dict[str, str] = {}
        for line in data.get("mapping", [""])[0].replace(",", "\n").splitlines():
            if "=" in line:
                old, new = line.split("=", 1)
                mapping[old.strip()] = new.strip()
        if action == "replace" and not mapping:
            self._send(_page("Error", "<p>Enter OLD=NEW mapping(s) for replace.</p>"), status=400)
            return
        refs = data.get("ref") or None
        dry = "dry_run" in data
        with _STATE["lock"]:
            finder = self._finder(_STATE["exports"])
            outcomes = finder.apply(result, action, mapping=mapping, ref_ids=refs, dry_run=dry)
        note = ("<p><b>Dry run</b> - nothing was changed. Go back, untick “Dry run” and submit again to apply.</p>"
                if dry else "<p><b>Applied.</b> Backups of every changed API object are in "
                            "<code>efinder-backups/</code>; modified export files are in "
                            "<code>efinder-modified-exports/</code>. Re-scan to confirm.</p>")
        self._send(_page("Change results", note + f"<pre>{html.escape(outcomes_to_text(outcomes))}</pre>"))


def serve(args: Any, cfg: Optional[EncompassConfig], host: str = "127.0.0.1", port: int = 8765) -> None:
    _STATE.update(args=args, cfg=cfg, exports=list(getattr(args, "export", None) or []))
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"Encompass Field Dependency Finder UI on http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
