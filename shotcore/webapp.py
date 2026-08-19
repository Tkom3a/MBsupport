from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .config import public_filters

if TYPE_CHECKING:
    from .main import ShotCore

WEB_DIR = Path(__file__).resolve().parent / "web"


def build_app(core: ShotCore) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware(core)])
    app["core"] = core
    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)
    app.router.add_get("/favicon.ico", _favicon_ico)
    app.router.add_get("/favicon.png", _favicon_png)
    app.router.add_get("/apple-touch-icon.png", _apple_icon)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/stats", _stats)
    app.router.add_get("/api/shots", _shots)
    app.router.add_static("/static", WEB_DIR)
    return app


def _auth_middleware(core: ShotCore):
    @web.middleware
    async def middleware(request: web.Request, handler):
        token = core.cfg.web.token
        if not token:
            return await handler(request)
        given = (
            request.rel_url.query.get("token")
            or request.headers.get("X-Shot-Token")
            or request.cookies.get("shot_token")
            or ""
        )
        if request.path in {"/health", "/favicon.ico", "/favicon.png", "/apple-touch-icon.png"}:
            return await handler(request)
        if given != token:
            return web.Response(status=401, text="Need WEB_TOKEN")
        response = await handler(request)
        if request.rel_url.query.get("token"):
            response.set_cookie("shot_token", token, httponly=True, samesite="Lax")
        return response

    return middleware


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _favicon_ico(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "favicon.ico")


async def _favicon_png(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "favicon.png")


async def _apple_icon(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "apple-touch-icon.png")


async def _index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def _status(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    payload = {
        "running": True,
        "symbols": len(core.active_symbols),
        "ws_connections": core.feed.connected,
        "shots_recorded": core.store.total,
        "filters": public_filters(core.cfg),
        "active_sample": core.active_symbols[:40],
    }
    return _json(payload)


async def _stats(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    lookback = _int(request, "lookback", core.cfg.web.stats_lookback_min)
    direction = str(request.rel_url.query.get("direction") or "")
    only_calm = str(request.rel_url.query.get("btc_calm") or "") in {"1", "true"}
    payload = core.store.stats(lookback_min=lookback, direction=direction, only_btc_calm=only_calm)
    payload["filters"] = public_filters(core.cfg)
    payload["symbols_watched"] = len(core.active_symbols)
    payload["ws_connections"] = core.feed.connected
    return _json(payload)


async def _shots(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    lookback = _int(request, "lookback", core.cfg.web.stats_lookback_min)
    limit = min(_int(request, "limit", 80), 300)
    direction = str(request.rel_url.query.get("direction") or "")
    return _json({"shots": core.store.recent(limit=limit, lookback_min=lookback, direction=direction)})


def _int(request: web.Request, name: str, default: int) -> int:
    raw = request.rel_url.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _json(payload: dict[str, Any]) -> web.Response:
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False),
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
