"""Desktop-style local app: a small JSON API + single-page UI served on 127.0.0.1.

Everything runs on the user's own machine. Credentials are kept in memory, or (if the user
ticks "remember") in the operating system's credential store (Windows Credential Manager /
macOS Keychain) through the optional ``keyring`` package. Non-secret settings are saved to a
JSON file in the user's app-data folder.

Start it with ``efinder-app`` / ``python -m encompass_field_finder.app`` or the Windows .exe.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from . import __version__
from .client import ApiError, DEFAULT_BASE_URL, EncompassClient, EncompassConfig
from .finder import FieldDependencyFinder
from .models import ScanResult
from .report import render

log = logging.getLogger(__name__)

KEYRING_SERVICE = "EncompassFieldFinder"


# ====================================================================== settings
def app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "EncompassFieldFinder")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, "encompass-field-finder")
    os.makedirs(path, exist_ok=True)
    return path


def work_dir() -> str:
    """Where backups / modified exports / saved results go (Documents\\EncompassFieldFinder)."""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    path = os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "EncompassFieldFinder")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class Settings:
    base_url: str = DEFAULT_BASE_URL
    instance_id: str = ""
    username: str = ""
    client_id: str = ""
    remember_secrets: bool = False
    export_paths: List[str] = field(default_factory=list)
    catalog_path: str = ""
    include_undocumented: bool = False
    workers: int = 4
    output_root: str = ""


def _keyring():
    try:
        import keyring  # type: ignore

        keyring.get_keyring()
        return keyring
    except Exception:  # noqa: BLE001
        return None


class SettingsStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(app_data_dir(), "settings.json")
        self.settings = Settings()
        self._secrets: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            self.settings = Settings(**known)
        except (OSError, ValueError, TypeError):
            self.settings = Settings()
        if self.settings.remember_secrets:
            kr = _keyring()
            if kr:
                for name in ("password", "client_secret", "token"):
                    try:
                        val = kr.get_password(KEYRING_SERVICE, self._kr_key(name))
                    except Exception:  # noqa: BLE001
                        val = None
                    if val:
                        self._secrets[name] = val

    def _kr_key(self, name: str) -> str:
        s = self.settings
        return f"{s.instance_id}:{s.username}:{name}"

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(asdict(self.settings), fh, indent=2)
            os.replace(tmp, self.path)

    def update(self, data: Dict[str, Any]) -> None:
        s = self.settings
        for k in ("base_url", "instance_id", "username", "client_id", "catalog_path", "output_root"):
            if k in data and data[k] is not None:
                setattr(s, k, str(data[k]).strip())
        s.base_url = (s.base_url or DEFAULT_BASE_URL).rstrip("/")
        for k in ("remember_secrets", "include_undocumented"):
            if k in data:
                setattr(s, k, bool(data[k]))
        if "workers" in data:
            try:
                s.workers = max(1, min(16, int(data["workers"])))
            except (TypeError, ValueError):
                pass
        if isinstance(data.get("export_paths"), list):
            s.export_paths = [str(p) for p in data["export_paths"] if str(p).strip()]
        for name in ("password", "client_secret", "token"):
            val = data.get(name)
            if val:  # empty string = keep the existing secret
                self._secrets[name] = str(val)
        self.save()
        kr = _keyring()
        if kr:
            for name in ("password", "client_secret", "token"):
                try:
                    if s.remember_secrets and self._secrets.get(name):
                        kr.set_password(KEYRING_SERVICE, self._kr_key(name), self._secrets[name])
                    elif not s.remember_secrets:
                        kr.delete_password(KEYRING_SERVICE, self._kr_key(name))
                except Exception:  # noqa: BLE001 - missing entry etc.
                    pass

    def clear_secrets(self) -> None:
        kr = _keyring()
        for name in ("password", "client_secret", "token"):
            self._secrets.pop(name, None)
            if kr:
                try:
                    kr.delete_password(KEYRING_SERVICE, self._kr_key(name))
                except Exception:  # noqa: BLE001
                    pass

    def config(self) -> Optional[EncompassConfig]:
        s = self.settings
        cfg = EncompassConfig(
            base_url=s.base_url or DEFAULT_BASE_URL, instance_id=s.instance_id, username=s.username,
            password=self._secrets.get("password", ""), client_id=s.client_id,
            client_secret=self._secrets.get("client_secret", ""), token=self._secrets.get("token", ""),
            verify_tls=os.environ.get("ENCOMPASS_CA_BUNDLE") or True,
        )
        return cfg if cfg.is_configured() else None

    def public(self) -> Dict[str, Any]:
        d = asdict(self.settings)
        d.update(
            has_password=bool(self._secrets.get("password")),
            has_client_secret=bool(self._secrets.get("client_secret")),
            has_token=bool(self._secrets.get("token")),
            keyring_available=_keyring() is not None,
            api_configured=self.config() is not None,
            settings_file=self.path,
        )
        return d


# ====================================================================== jobs
@dataclass
class Job:
    id: str
    kind: str                      # scan | apply | loaded
    status: str = "running"        # running | done | error
    log: List[str] = field(default_factory=list)
    result: Optional[ScanResult] = None
    outcomes: Optional[List[Dict[str, Any]]] = None
    error: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def progress(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")


class JobManager:
    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, kind: str, target, params: Dict[str, Any]) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, params=params)
        with self.lock:
            self.jobs[job.id] = job

        def run() -> None:
            try:
                target(job)
                job.status = "done"
            except Exception as err:  # noqa: BLE001
                job.error = f"{type(err).__name__}: {err}"
                job.progress("ERROR " + job.error)
                log.debug(traceback.format_exc())
                job.status = "error"
            job.finished = time.time()

        threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
        return job

    def add_done(self, kind: str, result: ScanResult, params: Dict[str, Any]) -> Job:
        job = Job(uuid.uuid4().hex[:12], kind, status="done", result=result, params=params,
                  finished=time.time())
        with self.lock:
            self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)


# ====================================================================== application
class App:
    def __init__(self, store: Optional[SettingsStore] = None) -> None:
        self.store = store or SettingsStore()
        self.jobs = JobManager()
        self.token = secrets.token_urlsafe(24)
        self._pick_lock = threading.Lock()

    def _finder(self, params: Dict[str, Any], progress) -> FieldDependencyFinder:
        s = self.store.settings
        cfg = self.store.config() if params.get("use_api", True) else None
        return FieldDependencyFinder(
            config=cfg,
            export_paths=params.get("export_paths") or [],
            catalog_path=s.catalog_path or None,
            include_undocumented=s.include_undocumented,
            include_loan_usage=bool(params.get("loan_usage")),
            include_templates=bool(params.get("templates", True)),
            min_confidence=params.get("min_confidence") or "medium",
            workers=s.workers,
            progress=progress,
        )

    def _out_root(self) -> str:
        root = self.store.settings.output_root or work_dir()
        os.makedirs(root, exist_ok=True)
        return root

    # ------------------------------------------------------------------ actions
    def start_scan(self, body: Dict[str, Any]) -> Job:
        fields = [f.strip() for f in body.get("fields", []) if str(f).strip()]
        if not fields:
            raise ValueError("Enter at least one field id.")
        params = {
            "fields": fields,
            "export_paths": [p for p in body.get("export_paths", []) if str(p).strip()],
            "use_api": bool(body.get("use_api", True)),
            "templates": bool(body.get("templates", True)),
            "loan_usage": bool(body.get("loan_usage", False)),
            "min_confidence": body.get("min_confidence", "medium"),
        }
        missing = [p for p in params["export_paths"] if not os.path.exists(p)]
        if missing:
            raise ValueError("These export paths do not exist: " + "; ".join(missing))
        if params["use_api"] and self.store.config() is None and not params["export_paths"]:
            raise ValueError("No Encompass connection is set up and no export folders were added. "
                             "Open Settings to connect, or add export folders.")
        # remember export folders for next time
        self.store.update({"export_paths": params["export_paths"]})

        def run(job: Job) -> None:
            finder = self._finder(params, job.progress)
            if params["use_api"] and finder.client is None:
                job.progress("No API connection configured - scanning export files only.")
            job.progress(f"Scanning for {', '.join(fields)}")
            job.result = finder.scan(fields)
            path = os.path.join(self._out_root(), f"scan-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(job.result.to_json())
                job.params["saved_to"] = path
                job.progress(f"Saved results to {path}")
            except OSError as err:
                job.progress(f"Could not save results: {err}")
            job.progress(f"Done: {len(job.result.references)} reference(s)")

        return self.jobs.start("scan", run, params)

    def start_apply(self, body: Dict[str, Any]) -> Job:
        scan = self.jobs.get(str(body.get("scan_job", "")))
        if scan is None or scan.result is None:
            raise ValueError("Run (or load) a scan first.")
        action = body.get("action")
        if action not in ("replace", "remove"):
            raise ValueError("Choose replace or remove.")
        mapping = {str(k).strip(): str(v).strip() for k, v in (body.get("mapping") or {}).items()
                   if str(k).strip() and str(v).strip()}
        if action == "replace" and not mapping:
            raise ValueError("Enter the new field id to replace with.")
        ref_ids = [str(r) for r in body.get("ref_ids") or []] or None
        dry_run = bool(body.get("dry_run", True))
        params = dict(scan.params)
        params.update(action=action, mapping=mapping, ref_ids=ref_ids, dry_run=dry_run)
        root = self._out_root()

        def run(job: Job) -> None:
            finder = self._finder(params, job.progress)
            outs = finder.apply(scan.result, action, mapping=mapping, ref_ids=ref_ids, dry_run=dry_run,
                                backup_dir=os.path.join(root, "backups"),
                                output_dir=os.path.join(root, "modified-exports"))
            job.outcomes = [asdict(o) for o in outs]
            job.progress(("Preview" if dry_run else "Apply") + f" finished: {len(outs)} item(s)")

        return self.jobs.start("apply", run, params)

    def load_results(self, body: Dict[str, Any]) -> Job:
        data = body.get("result")
        if isinstance(data, str):
            data = json.loads(data)
        result = ScanResult.from_dict(data)
        params = {"fields": result.field_ids, "export_paths": body.get("export_paths") or [],
                  "use_api": True, "templates": True}
        return self.jobs.add_done("loaded", result, params)

    def test_connection(self) -> Dict[str, Any]:
        cfg = self.store.config()
        if cfg is None:
            return {"ok": False, "message": "Fill in instance id, user name, password, client id and "
                                            "client secret (or a token) first."}
        client = EncompassClient(cfg)
        try:
            client.get("/encompass/v3/loanPipeline/canonicalFields", {"fieldIds": "4000"})
        except ApiError as err:
            if err.status in (0, 400, 401):
                return {"ok": False, "message": f"Login failed: {err}"}
            return {"ok": True, "message": f"Logged in, but a test call returned HTTP {err.status}. "
                                           "The API user may lack some persona rights."}
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "message": f"Could not reach {cfg.base_url}: {err}"}
        return {"ok": True, "message": f"Connected to {cfg.base_url} as {cfg.username or 'token'}."}

    def pick(self, kind: str) -> List[str]:
        """Open a native folder/file dialog on this machine (the server is local)."""
        with self._pick_lock:
            try:
                import tkinter as tk
                from tkinter import filedialog
            except Exception:  # noqa: BLE001
                raise ValueError("Native file dialogs are not available - type or paste the path instead.")
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:  # noqa: BLE001
                pass
            try:
                if kind == "files":
                    paths = list(filedialog.askopenfilenames(parent=root, title="Choose exported files"))
                elif kind == "json":
                    p = filedialog.askopenfilename(parent=root, title="Choose saved results",
                                                   filetypes=[("Results", "*.json"), ("All files", "*.*")])
                    paths = [p] if p else []
                elif kind == "catalog":
                    p = filedialog.askopenfilename(parent=root, title="Choose catalog file",
                                                   filetypes=[("Catalog", "*.json *.yaml *.yml")])
                    paths = [p] if p else []
                else:
                    p = filedialog.askdirectory(parent=root, title="Choose an export folder")
                    paths = [p] if p else []
            finally:
                root.destroy()
            return [os.path.normpath(p) for p in paths if p]

    def job_view(self, job: Job, since: int = 0) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": job.id, "kind": job.kind, "status": job.status, "error": job.error,
            "log": job.log[since:], "log_total": len(job.log), "params": job.params,
            "elapsed": round((job.finished or time.time()) - job.started, 1),
        }
        if job.status != "running":
            if job.result is not None:
                d["result"] = job.result.to_dict()
            if job.outcomes is not None:
                d["outcomes"] = job.outcomes
        return d


# ====================================================================== HTTP
def _ui_html() -> bytes:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "static", "app.html")]
    if getattr(sys, "_MEIPASS", None):  # PyInstaller
        candidates.append(os.path.join(sys._MEIPASS, "encompass_field_finder", "static", "app.html"))
    for c in candidates:
        if os.path.isfile(c):
            with open(c, "rb") as fh:
                return fh.read()
    return b"<h1>UI file missing</h1>"


def make_handler(app: App, port_ref: Dict[str, int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EncompassFieldFinder"

        def log_message(self, fmt: str, *a: Any) -> None:
            log.debug("http: " + fmt, *a)

        # -------------------------------------------------------------- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            port = port_ref.get("port")
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100 * 1024 * 1024:
                raise ValueError("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        # -------------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                html = _ui_html().replace(b"__APP_TOKEN__", app.token.encode()) \
                                 .replace(b"__APP_VERSION__", __version__.encode())
                return self._send(200, html, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                              "connect-src 'self'; frame-ancestors 'none'"})
            if url.path == "/api/download":
                job = app.jobs.get(q.get("job", [""])[0])
                if job is None or job.result is None:
                    return self._send(404, b"no such result", "text/plain")
                fmt = q.get("fmt", ["html"])[0]
                fmt = fmt if fmt in ("html", "csv", "json", "md", "text") else "html"
                ext = {"text": "txt"}.get(fmt, fmt)
                ctype = {"html": "text/html", "csv": "text/csv", "json": "application/json",
                         "md": "text/markdown", "text": "text/plain"}[fmt]
                name = f"encompass-field-references-{time.strftime('%Y%m%d-%H%M')}.{ext}"
                return self._send(200, render(job.result, fmt).encode("utf-8"), f"{ctype}; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            # all other GET endpoints need the token too (they expose results/settings)
            if self.headers.get("X-App-Token") != app.token:
                return self._send(403, b"missing token", "text/plain")
            if url.path == "/api/state":
                return self._json(200, {"version": __version__, "settings": app.store.public(),
                                        "work_dir": app._out_root()})
            if url.path.startswith("/api/jobs/"):
                job = app.jobs.get(url.path.rsplit("/", 1)[-1])
                if job is None:
                    return self._json(404, {"error": "no such job"})
                since = int(q.get("since", ["0"])[0] or 0)
                return self._json(200, app.job_view(job, since))
            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            if self.headers.get("X-App-Token") != app.token:
                return self._send(403, b"missing token", "text/plain")
            try:
                body = self._body()
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/settings":
                    app.store.update(body)
                    return self._json(200, {"settings": app.store.public()})
                if path == "/api/settings/clear-secrets":
                    app.store.clear_secrets()
                    return self._json(200, {"settings": app.store.public()})
                if path == "/api/test-connection":
                    if body:
                        app.store.update(body)
                    return self._json(200, app.test_connection())
                if path == "/api/scan":
                    return self._json(200, {"job_id": app.start_scan(body).id})
                if path == "/api/apply":
                    return self._json(200, {"job_id": app.start_apply(body).id})
                if path == "/api/load-results":
                    return self._json(200, {"job_id": app.load_results(body).id})
                if path == "/api/pick":
                    return self._json(200, {"paths": app.pick(str(body.get("kind", "folder")))})
                if path == "/api/open-folder":
                    target = app._out_root()
                    if os.name == "nt":
                        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
                    else:
                        webbrowser.open("file://" + target)
                    return self._json(200, {"ok": True, "path": target})
                return self._send(404, b"not found", "text/plain")
            except ValueError as err:
                return self._json(400, {"error": str(err)})
            except Exception as err:  # noqa: BLE001
                log.debug(traceback.format_exc())
                return self._json(500, {"error": f"{type(err).__name__}: {err}"})

    return Handler


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def create_server(app: App, port: int = 8765) -> ThreadingHTTPServer:
    port_ref: Dict[str, int] = {}
    chosen = _free_port(port) if port else 0
    httpd = ThreadingHTTPServer(("127.0.0.1", chosen), make_handler(app, port_ref))
    port_ref["port"] = httpd.server_address[1]
    return httpd


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="efinder-app", description="Encompass Field Finder - local app")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    app = App()
    httpd = create_server(app, args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print("=" * 64)
    print(f" Encompass Field Finder {__version__}")
    print(f" Running on this computer only: {url}")
    print(" Keep this window open while you use the app. Close it to stop.")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
