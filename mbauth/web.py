from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from .config import AuthConfig
from .session import read_session, sign_session
from .verify import authenticate

LOGIN_HTML = Path(__file__).resolve().parent / "login.html"

PUBLIC_PATHS = {
    "/health",
    "/login",
    "/api/login",
    "/api/auth/status",
    "/favicon.ico",
    "/favicon.png",
    "/apple-touch-icon.png",
}


def auth_enabled(cfg: AuthConfig | None) -> bool:
    return bool(cfg and cfg.enabled)


def _wants_html(request: web.Request) -> bool:
    accept = request.headers.get("Accept", "")
    if "text/html" in accept:
        return True
    if request.path == "/" or request.path.endswith(".html"):
        return True
    return False


def _machine_token_ok(request: web.Request, api_token: str) -> bool:
    if not api_token:
        return False
    given = (
        request.rel_url.query.get("token")
        or request.headers.get("X-Shot-Token")
        or request.cookies.get("shot_token")
        or ""
    )
    return given == api_token


def _session_user(request: web.Request, cfg: AuthConfig) -> str | None:
    raw = request.cookies.get(cfg.cookie_name) or ""
    data = read_session(cfg.session_secret, raw)
    if not data:
        return None
    return str(data["username"])


def build_middleware(cfg: AuthConfig, *, api_token: str = "") -> Any:
    @web.middleware
    async def middleware(request: web.Request, handler):
        if not cfg.enabled:
            # Legacy: only WEB_TOKEN if set
            if api_token and request.path not in PUBLIC_PATHS and request.path not in {
                "/favicon.ico",
                "/favicon.png",
                "/apple-touch-icon.png",
                "/health",
            }:
                if not _machine_token_ok(request, api_token):
                    return web.Response(status=401, text="Need WEB_TOKEN")
                response = await handler(request)
                if request.rel_url.query.get("token"):
                    response.set_cookie("shot_token", api_token, httponly=True, samesite="Lax")
                return response
            return await handler(request)

        if request.path in PUBLIC_PATHS or request.path.startswith("/static/login"):
            return await handler(request)

        user = _session_user(request, cfg)
        if user:
            request["auth_user"] = user
            return await handler(request)

        if _machine_token_ok(request, api_token):
            request["auth_user"] = "token"
            response = await handler(request)
            if request.rel_url.query.get("token"):
                response.set_cookie("shot_token", api_token, httponly=True, samesite="Lax")
            return response

        if _wants_html(request) and request.method == "GET":
            raise web.HTTPFound("/login")
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    return middleware


async def _login_page(request: web.Request) -> web.StreamResponse:
    cfg: AuthConfig = request.app["auth_cfg"]
    if LOGIN_HTML.is_file():
        text = LOGIN_HTML.read_text(encoding="utf-8").replace("{{BRAND}}", cfg.brand)
        text = text.replace("{{MODE}}", cfg.mode)
        return web.Response(text=text, content_type="text/html", charset="utf-8")
    return web.Response(text="login.html missing", status=500)


async def _api_login(request: web.Request) -> web.Response:
    cfg: AuthConfig = request.app["auth_cfg"]
    if not cfg.enabled:
        return web.json_response({"ok": False, "error": "auth disabled"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")
    result = authenticate(cfg, username, password)
    if not result.ok:
        return web.json_response({"ok": False, "error": result.error}, status=401)
    token = sign_session(cfg.session_secret, result.username, cfg.session_ttl_hours)
    resp = web.json_response({"ok": True, "username": result.username})
    resp.set_cookie(
        cfg.cookie_name,
        token,
        httponly=True,
        samesite="Lax",
        max_age=cfg.session_ttl_hours * 3600,
        path="/",
    )
    return resp


async def _logout(request: web.Request) -> web.StreamResponse:
    cfg: AuthConfig = request.app["auth_cfg"]
    resp = web.HTTPFound("/login")
    resp.del_cookie(cfg.cookie_name, path="/")
    resp.del_cookie("shot_token", path="/")
    return resp


async def _auth_status(request: web.Request) -> web.Response:
    cfg: AuthConfig = request.app["auth_cfg"]
    api_token = request.app.get("api_token") or ""
    user = _session_user(request, cfg) if cfg.enabled else None
    token_ok = _machine_token_ok(request, api_token)
    return web.json_response(
        {
            "enabled": cfg.enabled,
            "mode": cfg.mode,
            "authenticated": bool(user or (token_ok and not cfg.enabled) or (token_ok and cfg.enabled)),
            "username": user or ("token" if token_ok else ""),
        }
    )


def attach_auth(
    app: web.Application,
    cfg: AuthConfig,
    *,
    api_token: str = "",
) -> None:
    """Register auth state, routes. Caller must pass middlewares=[build_middleware(...)] at Application().

    Or use make_app() helper pattern — here we only store config + routes.
    """
    app["auth_cfg"] = cfg
    app["api_token"] = api_token or ""
    app.router.add_get("/login", _login_page)
    app.router.add_post("/api/login", _api_login)
    app.router.add_get("/logout", _logout)
    app.router.add_get("/api/auth/status", _auth_status)


def make_middlewares(cfg: AuthConfig, *, api_token: str = "") -> list:
    return [build_middleware(cfg, api_token=api_token)]
