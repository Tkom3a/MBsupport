from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def load_repo_env() -> list[str]:
    """Подхватить .env из корня и shottrader/.env, не затирая уже заданные переменные."""
    here = Path(__file__).resolve().parents[1]
    cwd = Path.cwd()
    loaded: list[str] = []
    seen: set[Path] = set()
    for root in (cwd, here):
        for path in (root / ".env", root / "shottrader" / ".env"):
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            if _apply_dotenv(resolved):
                loaded.append(str(resolved))
    return loaded


def _apply_dotenv(path: Path) -> bool:
    changed = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        changed = True
    return changed


def _first_local_user() -> tuple[str, str]:
    user = _env("SHOT_USER") or _env("AUTH_USER")
    password = _env("SHOT_PASSWORD") or _env("AUTH_PASSWORD")
    if user and password:
        return user, password
    raw = _env("AUTH_USERS")
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        name, _, pwd = part.partition(":")
        name, pwd = name.strip(), pwd.strip()
        if name and pwd:
            return name, pwd
    return "", ""


class ShotClient:
    def __init__(
        self,
        core_url: str | None = None,
        trader_url: str | None = None,
        core_token: str | None = None,
        trader_token: str | None = None,
        username: str = "",
        password: str = "",
        timeout: float = 12.0,
    ):
        self.env_files = load_repo_env()
        self.core_url = (core_url or _env("SHOTCORE_URL", "http://127.0.0.1:4861")).rstrip("/")
        self.trader_url = (trader_url or _env("SHOTTRADER_URL", "http://127.0.0.1:4863")).rstrip("/")
        self.core_token = core_token if core_token is not None else (
            _env("SHOTCORE_TOKEN")
            or _env("WEB_TOKEN")
            or _env("AUTH_API_TOKEN")
            or _env("SESSION_SECRET")
        )
        self.trader_token = trader_token if trader_token is not None else (
            _env("TRADER_TOKEN")
            or _env("UI_TOKEN")
            or _env("WEB_TOKEN")
            or _env("AUTH_API_TOKEN")
            or _env("SESSION_SECRET")
        )
        if username and password:
            self.username, self.password = username, password
        else:
            self.username, self.password = _first_local_user()
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.logged_in: list[str] = []

    def ensure_auth(self) -> None:
        """Логин по AUTH_USERS / SHOT_USER, если API закрыт сессией."""
        if not self.username or not self.password:
            return
        for label, base in (("trader", self.trader_url), ("core", self.core_url)):
            try:
                self._raw(base, "POST", "/api/login", "", body={"username": self.username, "password": self.password})
                self.logged_in.append(label)
            except Exception:
                continue

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
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:240]
            if exc.code == 401:
                raise RuntimeError(
                    "401 unauthorized — страница закрыта логином. "
                    "CLI читает .env (SESSION_SECRET / WEB_TOKEN / AUTH_USERS) "
                    "или: python3 -m shotcli --user ИМЯ --password ПАРОЛЬ. "
                    f"{detail}"
                ) from exc
            raise RuntimeError(f"{exc.code} {exc.reason} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"нет связи с {url}: {exc.reason}") from exc
