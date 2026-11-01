"""Thin Encompass Developer Connect REST client.

Auth: OAuth2 resource-owner password grant against ``/oauth2/v1/token`` with the username in
``<user>@encompass:<instanceId>`` form (standard Developer Connect "lp" scope), or a
pre-issued bearer token.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elliemae.com"


class ApiError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str = "") -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


@dataclass
class EncompassConfig:
    base_url: str = DEFAULT_BASE_URL
    instance_id: str = ""
    username: str = ""
    password: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = "lp"
    token: str = ""                # use a pre-issued bearer token instead of logging in
    timeout: float = 60.0
    max_retries: int = 4
    verify_tls: Any = True         # bool or CA bundle path
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "ENCOMPASS_") -> "EncompassConfig":
        e = lambda k, d="": os.environ.get(prefix + k, d)  # noqa: E731
        return cls(
            base_url=e("API_BASE", DEFAULT_BASE_URL).rstrip("/"),
            instance_id=e("INSTANCE_ID"),
            username=e("USERNAME"),
            password=e("PASSWORD"),
            client_id=e("CLIENT_ID"),
            client_secret=e("CLIENT_SECRET"),
            scope=e("SCOPE", "lp"),
            token=e("TOKEN"),
            verify_tls=e("CA_BUNDLE") or True,
        )

    def is_configured(self) -> bool:
        return bool(self.token or (self.instance_id and self.username and self.password
                                   and self.client_id and self.client_secret))


class EncompassClient:
    """Minimal JSON client with token refresh, 401 retry and 429/5xx backoff."""

    def __init__(self, config: EncompassConfig, session: Any = None) -> None:
        self.config = config
        if session is None:
            import requests  # imported lazily so offline-only use needs no dependency

            session = requests.Session()
        self.session = session
        self._token: Optional[str] = config.token or None
        self._token_expiry: float = float("inf") if config.token else 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def _login(self) -> None:
        c = self.config
        if c.token:
            self._token, self._token_expiry = c.token, float("inf")
            return
        if not c.is_configured():
            raise ApiError(0, "POST", "/oauth2/v1/token",
                           "missing credentials (instance id, username, password, client id/secret)")
        user = c.username if "@encompass:" in c.username else f"{c.username}@encompass:{c.instance_id}"
        resp = self.session.post(
            f"{c.base_url}/oauth2/v1/token",
            data={
                "grant_type": "password",
                "username": user,
                "password": c.password,
                "client_id": c.client_id,
                "client_secret": c.client_secret,
                "scope": c.scope,
            },
            timeout=c.timeout,
            verify=c.verify_tls,
        )
        if resp.status_code != 200:
            raise ApiError(resp.status_code, "POST", "/oauth2/v1/token", resp.text)
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 1800)) - 60

    def _auth_header(self) -> Dict[str, str]:
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self._login()
            return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------ requests
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        expect_json: bool = True,
    ) -> Tuple[Any, Dict[str, str]]:
        """Return ``(parsed_body, headers)``. Raises :class:`ApiError` on non-2xx."""
        url = path if path.startswith("http") else f"{self.config.base_url}{path}"
        attempt, relogged = 0, False
        while True:
            headers = {"Accept": "application/json", **self.config.extra_headers, **self._auth_header()}
            resp = self.session.request(
                method, url, params=params, json=json_body, headers=headers,
                timeout=self.config.timeout, verify=self.config.verify_tls,
            )
            status = resp.status_code
            if status == 401 and not relogged and not self.config.token:
                relogged = True
                with self._lock:
                    self._token = None
                continue
            if status in (429, 502, 503, 504) and attempt < self.config.max_retries:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                log.info("HTTP %s on %s %s; retrying in %.0fs", status, method, path, delay)
                time.sleep(delay)
                continue
            if status >= 400:
                raise ApiError(status, method, path, getattr(resp, "text", ""))
            hdrs = {k.lower(): v for k, v in dict(resp.headers or {}).items()}
            if not expect_json or status == 204 or not getattr(resp, "content", b""):
                return None, hdrs
            try:
                return resp.json(), hdrs
            except ValueError:
                return resp.text, hdrs

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)[0]

    def post(self, path: str, body: Any, params: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, str]]:
        return self.request("POST", path, params=params, json_body=body)

    def patch(self, path: str, body: Any, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("PATCH", path, params=params, json_body=body)[0]

    def put(self, path: str, body: Any, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("PUT", path, params=params, json_body=body)[0]
