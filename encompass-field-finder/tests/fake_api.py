"""A tiny in-memory stand-in for the Encompass REST API used by the tests."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


class FakeResponse:
    def __init__(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = "" if body is None else json.dumps(body)

    def json(self) -> Any:
        return self._body


Handler = Callable[[str, Dict[str, Any], Any], Tuple[int, Any, Dict[str, str]]]


class FakeSession:
    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Handler] = {}
        self.calls: List[Tuple[str, str, Dict[str, Any], Any]] = []
        self.token_requests = 0

    def route(self, method: str, path: str, handler_or_body: Any, status: int = 200,
              headers: Optional[Dict[str, str]] = None) -> None:
        if callable(handler_or_body):
            self.routes[(method, path)] = handler_or_body
        else:
            self.routes[(method, path)] = lambda p, params, body: (status, handler_or_body, headers or {})

    # requests.Session API -------------------------------------------------
    def post(self, url: str, data=None, timeout=None, verify=None):  # token endpoint
        self.token_requests += 1
        assert url.endswith("/oauth2/v1/token")
        assert data["grant_type"] == "password"
        assert "@encompass:" in data["username"]
        return FakeResponse(200, {"access_token": "tok", "expires_in": 1800})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=None):
        assert headers["Authorization"] == "Bearer tok"
        path = url.split("api.test", 1)[1]
        self.calls.append((method, path, dict(params or {}), json))
        h = self.routes.get((method, path))
        if h is None:
            return FakeResponse(404, {"summary": "Not Found"})
        status, body, hdrs = h(path, dict(params or {}), json)
        return FakeResponse(status, body, hdrs)
