from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from mbauth import load_auth_config
from mbauth.web import attach_auth, make_middlewares

from .config import public_filters

if TYPE_CHECKING:
    from .main import ShotCore

WEB_DIR = Path(__file__).resolve().parent / "web"


def build_app(core: ShotCore) -> web.Application:
    auth_cfg = load_auth_config(brand="ShotCore", token_fallback=core.cfg.web.token)
    api_token = auth_cfg.resolve_api_token(core.cfg.web.token)
    app = web.Application(middlewares=make_middlewares(auth_cfg, api_token=api_token))
    app["core"] = core
    attach_auth(app, auth_cfg, api_token=api_token)
    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)
    app.router.add_get("/favicon.ico", _favicon_ico)
    app.router.add_get("/favicon.png", _favicon_png)
    app.router.add_get("/apple-touch-icon.png", _apple_icon)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/stats", _stats)
    app.router.add_get("/api/shots", _shots)
    app.router.add_get("/api/mt-plan", _mt_plan)
    if WEB_DIR.is_dir():
        app.router.add_static("/static", WEB_DIR)
    return app


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _file(name: str) -> web.StreamResponse:
    path = WEB_DIR / name
    if not path.is_file():
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


async def _favicon_ico(_request: web.Request) -> web.StreamResponse:
    return _file("favicon.ico")


async def _favicon_png(_request: web.Request) -> web.StreamResponse:
    return _file("favicon.png")


async def _apple_icon(_request: web.Request) -> web.StreamResponse:
    return _file("apple-touch-icon.png")


async def _index(_request: web.Request) -> web.StreamResponse:
    path = WEB_DIR / "index.html"
    if not path.is_file():
        return web.Response(status=500, text="index.html missing")
    return web.FileResponse(path)


async def _status(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    payload = {
        "running": True,
        "symbols": len(core.active_symbols),
        "ws_connections": core.feed.connected,
        "shots_recorded": core.store.total,
        "filters": public_filters(core.cfg),
        "active_sample": core.active_symbols[:40],
        "active_markets": [item.as_public() for item in core.active_board],
        "universe_size": core.universe_size,
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
    payload["universe_size"] = core.universe_size
    payload["active_markets"] = [item.as_public() for item in core.active_board]
    for row in payload.get("rows") or []:
        if not row.get("lever"):
            row["lever"] = core.leverage.get(row["symbol"], 0.0)
    return _json(payload)


async def _shots(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    lookback = _int(request, "lookback", core.cfg.web.stats_lookback_min)
    limit = min(_int(request, "limit", 80), 300)
    direction = str(request.rel_url.query.get("direction") or "")
    only_calm = str(request.rel_url.query.get("btc_calm") or "") in {"1", "true"}
    symbol = str(request.rel_url.query.get("symbol") or "").strip()
    return _json(
        {
            "shots": core.store.recent(
                limit=limit,
                lookback_min=lookback,
                direction=direction,
                only_btc_calm=only_calm,
                symbol=symbol,
            )
        }
    )


async def _mt_plan(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    lookback = _int(request, "lookback", core.cfg.web.stats_lookback_min)
    plan = core.store.build_mt_plan(
        lookback_min=lookback,
        subscribed=set(core.active_symbols),
        run_hours=core.cfg.output.mt_run_hours,
    )
    return _json(plan)


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
