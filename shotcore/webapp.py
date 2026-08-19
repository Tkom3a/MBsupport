from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .config import public_filters
from .okx_rest import OkxRest

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
    app.router.add_get("/api/chart", _chart)
    if WEB_DIR.is_dir():
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


_chart_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_chart_lock: asyncio.Lock | None = None
_CHART_TTL = 90.0
_CHART_MAX = 24


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
    return _json({"shots": core.store.recent(limit=limit, lookback_min=lookback, direction=direction)})


def _get_chart_lock() -> asyncio.Lock:
    global _chart_lock
    if _chart_lock is None:
        _chart_lock = asyncio.Lock()
    return _chart_lock


async def _chart(request: web.Request) -> web.Response:
    core: ShotCore = request.app["core"]
    symbol = str(request.rel_url.query.get("symbol") or "").strip().upper()
    ts = _int(request, "ts", 0)
    if not symbol or ts <= 0:
        return web.Response(status=400, text="Need symbol and ts")
    key = f"{symbol}:{ts // 1000}"
    now = time.monotonic()
    cached = _chart_cache.get(key)
    if cached and now - cached[0] < _CHART_TTL:
        return _json(cached[1])

    async with _get_chart_lock():
        cached = _chart_cache.get(key)
        if cached and time.monotonic() - cached[0] < _CHART_TTL:
            return _json(cached[1])
        shot_row = core.store.find_shot(symbol, ts)
        shot = core.store.as_public(shot_row) if shot_row else {
            "symbol": symbol,
            "peak_ts": ts,
            "percent": 0,
            "direction": "",
        }
        bar = "1s"
        candles: list[dict[str, Any]] = []
        if core._session is not None:
            rest = OkxRest(core.cfg, core._session)
            try:
                bar, candles = await rest.fetch_candles_around(symbol, ts)
            except Exception:
                bar, candles = "1s", []
        payload = {"bar": bar, "candles": candles, "shot": shot}
        if candles:
            _chart_cache[key] = (time.monotonic(), payload)
            if len(_chart_cache) > _CHART_MAX:
                oldest = sorted(_chart_cache.items(), key=lambda item: item[1][0])[: len(_chart_cache) - _CHART_MAX]
                for drop, _value in oldest:
                    _chart_cache.pop(drop, None)
        return _json(payload)


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
