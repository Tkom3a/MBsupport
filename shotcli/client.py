from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


class ShotClient:
    def __init__(
        self,
        core_url: str | None = None,
        trader_url: str | None = None,
        core_token: str | None = None,
        trader_token: str | None = None,
        timeout: float = 12.0,
    ):
        self.core_url = (core_url or _env("SHOTCORE_URL", "http://127.0.0.1:4861")).rstrip("/")
        self.trader_url = (trader_url or _env("SHOTTRADER_URL", "http://127.0.0.1:4863")).rstrip("/")
        self.core_token = core_token if core_token is not None else (
            _env("SHOTCORE_TOKEN") or _env("WEB_TOKEN") or _env("SESSION_SECRET")
        )
        self.trader_token = trader_token if trader_token is not None else (
            _env("TRADER_TOKEN") or _env("UI_TOKEN")
        )
        self.timeout = timeout

    def core_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(self.core_url, "GET", path, token=self.core_token, params=params)

    def core_post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request(self.core_url, "POST", path, token=self.core_token, body=body)

    def trader_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(self.trader_url, "GET", path, token=self.trader_token, params=params)

    def trader_post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request(self.trader_url, "POST", path, token=self.trader_token, body=body)

    def ping(self) -> dict[str, str]:
        out = {"core": "down", "trader": "down"}
        try:
            text = self._raw(self.core_url, "GET", "/health", self.core_token)
            out["core"] = "ok" if text.strip() == "ok" else text.strip()[:40]
        except Exception as exc:
            out["core"] = f"down ({exc})"
        try:
            text = self._raw(self.trader_url, "GET", "/health", self.trader_token)
            out["trader"] = "ok" if text.strip() == "ok" else text.strip()[:40]
        except Exception as exc:
            out["trader"] = f"down ({exc})"
        return out

    def _request(
        self,
        base: str,
        method: str,
        path: str,
        token: str = "",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        text = self._raw(base, method, path, token, params, body)
        if not text:
            return {}
        return json.loads(text)

    def _raw(
        self,
        base: str,
        method: str,
        path: str,
        token: str = "",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> str:
        query = dict(params or {})
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Shot-Token"] = token
            query.setdefault("token", token)
        url = base + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:240]
            raise RuntimeError(f"{exc.code} {exc.reason} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"нет связи с {url}: {exc.reason}") from exc
