from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from mbauth import load_auth_config
from mbauth.web import attach_auth, make_middlewares

from .config import TraderConfig, load_trader_config
from .engine import ShotEngine
from .okx_broker import OkxBroker

log = logging.getLogger("shottrader")
WEB_DIR = Path(__file__).resolve().parent / "web"


class ShotTrader:
    def __init__(self, cfg: TraderConfig, root: Path):
        self.cfg = cfg
        self.root = root
        self.session: aiohttp.ClientSession | None = None
        self.broker: OkxBroker | None = None
        self.engine = ShotEngine(cfg, None, root / cfg.data_dir)
        self._ws_task: asyncio.Task | None = None
        self._desired: set[str] = set()
        self._last_hour_report = 0
        self._last_day_report = 0

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            self.broker = OkxBroker(
                self.cfg.okx_rest,
                session,
                self.cfg.okx_api_key,
                self.cfg.okx_secret,
                self.cfg.okx_passphrase,
                self.cfg.okx_simulated,
            )
            self.engine.broker = self.broker
            if self.cfg.live_trading and not self.broker.ready:
                self.engine.emulate = True
                self.engine.note("LIVE_TRADING без ключей OKX — остаюсь в эмуляции")
            elif self.cfg.live_trading:
                self.engine.emulate = False
                self.engine.note("LIVE trading включён")
            else:
                self.engine.emulate = True
                self.engine.note("режим эмуляции (ордера на биржу не уходят)")
            runner = await self._start_web()
            try:
                await asyncio.gather(
                    self._plan_loop(),
                    self._follow_loop(),
                    self._report_loop(),
                    self._ws_loop(),
                )
            finally:
                await runner.cleanup()

    async def _start_web(self) -> web.AppRunner:
        auth_cfg = load_auth_config(brand="ShotTrader", token_fallback=self.cfg.web_token)
        # UI lock ≠ токен для ShotCore. WEB_TOKEN/SHOTCORE_TOKEN не закрывают страницу.
        ui_token = auth_cfg.resolve_ui_token(self.cfg.web_token)
        app = web.Application(middlewares=make_middlewares(auth_cfg, api_token=ui_token))
        app["core"] = self
        attach_auth(app, auth_cfg, api_token=ui_token)
        app.router.add_get("/", self._index)
        app.router.add_get("/health", self._health)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/settings", self._settings)
        app.router.add_post("/api/shotcore", self._set_shotcore)
        app.router.add_post("/api/view", self._view)
        app.router.add_post("/api/resume", self._resume)
        app.router.add_post("/api/panic", self._panic)
        if WEB_DIR.is_dir():
            app.router.add_static("/static", WEB_DIR)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.host, self.cfg.port)
        await site.start()
        log.info(
            "ShotTrader UI http://%s:%s/  emulate=%s auth=%s",
            self.cfg.host,
            self.cfg.port,
            self.engine.emulate,
            auth_cfg.mode if auth_cfg.enabled else ("token" if ui_token else "off"),
        )
        return runner

    async def _health(self, _request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        path = WEB_DIR / "index.html"
        if not path.is_file():
            return web.Response(status=500, text="index.html missing")
        return web.FileResponse(path)

    async def _state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.engine.snapshot())

    async def _settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        if "order_size" in body:
            self.engine.order_size = max(1.0, float(body["order_size"]))
            self.cfg.order_size_usdt = self.engine.order_size
        if "leverage" in body:
            self.engine.leverage = max(1, int(float(body["leverage"])))
            self.cfg.leverage = self.engine.leverage
        if "autostop_usd" in body:
            self.engine.autostop_usd = max(0.5, float(body["autostop_usd"]))
            self.cfg.autostop_usd = self.engine.autostop_usd
        if "shotcore_url" in body:
            self._apply_shotcore_url(str(body.get("shotcore_url") or ""))
        self.engine.note(
            f"настройки: size={self.engine.order_size:g} x{self.engine.leverage} autostop={self.engine.autostop_usd:g}$"
        )
        return web.json_response({"ok": True})

    async def _set_shotcore(self, request: web.Request) -> web.Response:
        body = await request.json()
        url = self._apply_shotcore_url(str(body.get("url") or body.get("shotcore_url") or ""))
        token = str(body.get("token") or body.get("shotcore_token") or "").strip()
        if token:
            self.cfg.shotcore_token = token
            self.engine.note("ShotCore token задан (для /api/mt-plan)")
        return web.json_response({"ok": True, "shotcore_url": url, "has_token": bool(self.cfg.shotcore_token)})

    def _apply_shotcore_url(self, raw: str) -> str:
        url = (raw or "").strip().rstrip("/")
        if not url:
            return self.cfg.shotcore_url
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url
        prev = self.cfg.shotcore_url
        self.cfg.shotcore_url = url
        if url != prev:
            self.engine.note(f"ShotCore URL → {url}")
            self.engine.plan_error = ""
        return url

    async def _view(self, request: web.Request) -> web.Response:
        body = await request.json()
        symbol = str(body.get("symbol") or "").upper()
        if symbol:
            self.engine.view_symbol = symbol
            self._desired.add(symbol)
        return web.json_response({"ok": True})

    async def _resume(self, _request: web.Request) -> web.Response:
        self.engine.resume()
        return web.json_response({"ok": True})

    async def _panic(self, _request: web.Request) -> web.Response:
        self.engine.emergency("panic из UI")
        return web.json_response({"ok": True})

    async def _plan_loop(self) -> None:
        fail_sleep = 8
        while True:
            try:
                plan = await self._fetch_plan()
                raw = list(plan.get("pairs") or [])
                # План уже отфильтрован ShotCore (D>0 и плюс ≥ min_win). Здесь только валидность.
                pairs = [p for p in raw if float(p.get("recommend_pct") or 0) > 0]
                started = self.engine.sync_plan(pairs)
                self.engine.plan_error = ""
                self.engine.plan_updated_ts = int(time.time() * 1000)
                for sym in list(self.engine.algos) + [self.engine.view_symbol]:
                    if sym:
                        self._desired.add(sym.upper())
                if started:
                    self.engine.note(f"новые клоны: {', '.join(started)}")
                elif not pairs:
                    self.engine.note(
                        f"план ShotCore пуст ({self.cfg.shotcore_url}/api/mt-plan) — нет пар с D>0"
                    )
                fail_sleep = 8
                await asyncio.sleep(self.cfg.poll_sec)
            except Exception as exc:
                self.engine.plan_error = str(exc)
                self.engine.note(f"план ShotCore: {exc}")
                await asyncio.sleep(fail_sleep)
                fail_sleep = min(60, fail_sleep + 5)

    async def _fetch_plan(self) -> dict[str, Any]:
        assert self.session is not None
        params = {"lookback": str(self.cfg.lookback_min)}
        headers: dict[str, str] = {}
        token = (self.cfg.shotcore_token or "").strip()
        if token:
            headers["X-Shot-Token"] = token
            params["token"] = token
        url = f"{self.cfg.shotcore_url}/api/mt-plan?{urlencode(params)}"
        async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                text = await resp.text()
                if resp.status == 401:
                    raise RuntimeError(
                        "401 unauthorized — задайте одинаковый WEB_TOKEN/SHOTCORE_TOKEN "
                        f"(или SESSION_SECRET) на ShotCore и ShotTrader. {text[:120]}"
                    )
                raise RuntimeError(f"{resp.status} {text[:200]}")
            return await resp.json()

    async def _follow_loop(self) -> None:
        while True:
            try:
                await self.engine.follow_once()
            except Exception as exc:
                log.debug("follow: %s", exc)
            await asyncio.sleep(0.25)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = asyncio.get_event_loop().time()
            hour = self.engine.window_stats(1)
            day = self.engine.window_stats(24)
            if now - self._last_hour_report >= 3600:
                self._last_hour_report = now
                await self._send_report("Часовой отчёт", hour)
            if now - self._last_day_report >= 86400:
                self._last_day_report = now
                await self._send_report("Суточный отчёт", day)

    async def _send_report(self, title: str, stats: dict[str, Any]) -> None:
        text = (
            f"ShotTrader · {title}\n"
            f"сделок: {stats['trades']} (+{stats['plus']}/−{stats['minus']})\n"
            f"PnL: {stats['pnl_usd']:+.2f} USDT\n"
            f"нереализ.: {self.engine.unrealized():+.2f}$\n"
            f"режим: {'эмуляция' if self.engine.emulate else 'LIVE'}"
        )
        self.engine.note(text.replace("\n", " · "))
        token = self.cfg.telegram_bot_token
        chat = self.cfg.telegram_chat_id
        if not token or not chat or self.session is None:
            return
        try:
            await self.session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text},
            )
        except Exception as exc:
            log.warning("telegram report: %s", exc)

    async def _ws_loop(self) -> None:
        import websockets

        while True:
            symbols = sorted(self._desired)
            if not symbols:
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(
                    self.cfg.okx_ws,
                    ping_interval=15,
                    ping_timeout=20,
                    max_size=2**22,
                    compression=None,
                ) as ws:
                    for i in range(0, len(symbols), 40):
                        chunk = symbols[i : i + 40]
                        await ws.send(
                            json.dumps(
                                {
                                    "op": "subscribe",
                                    "args": [{"channel": "trades", "instId": s} for s in chunk],
                                }
                            )
                        )
                    self.engine.note(f"WS trades: {len(symbols)} пар")
                    subscribed = set(symbols)
                    while True:
                        if set(self._desired) != subscribed:
                            break
                        raw = await asyncio.wait_for(ws.recv(), timeout=25)
                        msg = json.loads(raw)
                        if "data" not in msg:
                            continue
                        for row in msg["data"]:
                            symbol = str(row.get("instId") or "")
                            try:
                                ts = int(float(row.get("ts") or 0))
                                price = float(row.get("px") or 0)
                                qty = float(row.get("sz") or 0)
                            except (TypeError, ValueError):
                                continue
                            side = "buy" if str(row.get("side") or "").lower() == "buy" else "sell"
                            self.engine.on_trade(symbol, ts, price, qty, side)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("WS reconnect: %s", exc)
                await asyncio.sleep(2)


def setup_logging(root: Path) -> None:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = TimedRotatingFileHandler(logs / "shottrader.log", when="midnight", backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root_log.handlers.clear()
    root_log.addHandler(sh)
    root_log.addHandler(fh)


def main() -> None:
    root = Path.cwd()
    setup_logging(root)
    cfg = load_trader_config(root)
    core = ShotTrader(cfg, root)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_args: Any) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(core.run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
