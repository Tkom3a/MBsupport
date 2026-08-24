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

    def _refresh_desired(self) -> None:
        """Смотрим только живые клоны. Забаненные пары не держим на WS."""
        wanted = {str(sym).upper() for sym in self.engine.algos if not self.engine.is_banned(sym)}
        view = str(self.engine.view_symbol or "").upper()
        self._desired = set(wanted)
        if view and view not in wanted:
            self.engine.view_symbol = next(iter(wanted), "")

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
            await self._push_min_fills()
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
        # При AUTH_MODE=local/ldap CLI ходит с SESSION_SECRET / WEB_TOKEN, как браузерный API.
        machine = auth_cfg.resolve_api_token(self.cfg.web_token or self.cfg.shotcore_token) if auth_cfg.enabled else ""
        api_token = ui_token or machine
        app = web.Application(middlewares=make_middlewares(auth_cfg, api_token=api_token))
        app["core"] = self
        attach_auth(app, auth_cfg, api_token=api_token)
        app.router.add_get("/", self._index)
        app.router.add_get("/health", self._health)
        app.router.add_get("/favicon.ico", self._favicon_ico)
        app.router.add_get("/favicon.png", self._favicon_png)
        app.router.add_get("/apple-touch-icon.png", self._apple_icon)
        app.router.add_get("/api/state", self._state)
        app.router.add_post("/api/settings", self._settings)
        app.router.add_post("/api/shotcore", self._set_shotcore)
        app.router.add_get("/api/reports", self._reports)
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

    def _web_file(self, name: str) -> web.StreamResponse:
        path = WEB_DIR / name
        if not path.is_file():
            return web.Response(status=404, text="not found")
        return web.FileResponse(path)

    async def _favicon_ico(self, _request: web.Request) -> web.StreamResponse:
        return self._web_file("favicon.ico")

    async def _favicon_png(self, _request: web.Request) -> web.StreamResponse:
        return self._web_file("favicon.png")

    async def _apple_icon(self, _request: web.Request) -> web.StreamResponse:
        return self._web_file("apple-touch-icon.png")

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        path = WEB_DIR / "index.html"
        if not path.is_file():
            return web.Response(status=500, text="index.html missing")
        return web.FileResponse(path)

    async def _state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.engine.snapshot())

    async def _settings(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "нужен JSON"}, status=400)
        try:
            if "order_size_x20" in body and body["order_size_x20"] not in (None, ""):
                self.engine.order_size_x20 = max(1.0, float(body["order_size_x20"]))
                self.cfg.order_size_x20 = self.engine.order_size_x20
            if "order_size_x50" in body and body["order_size_x50"] not in (None, ""):
                self.engine.order_size_x50 = max(1.0, float(body["order_size_x50"]))
                self.cfg.order_size_x50 = self.engine.order_size_x50
            if "order_size" in body and body["order_size"] not in (None, "") and "order_size_x50" not in body:
                val = max(1.0, float(body["order_size"]))
                self.engine.order_size_x20 = val
                self.engine.order_size_x50 = val
                self.cfg.order_size_x20 = val
                self.cfg.order_size_x50 = val
            self.engine.order_size = self.engine.order_size_x50
            if "leverage" in body and body["leverage"] not in (None, ""):
                self.engine.leverage = max(1, int(float(body["leverage"])))
                self.cfg.leverage = self.engine.leverage
            if "autostop_usd" in body and body["autostop_usd"] not in (None, ""):
                self.engine.autostop_usd = max(0.5, float(body["autostop_usd"]))
                self.cfg.autostop_usd = self.engine.autostop_usd
            if "trade_long" in body:
                self.engine.trade_long = bool(body["trade_long"])
                self.cfg.trade_long = self.engine.trade_long
            if "trade_short" in body:
                self.engine.trade_short = bool(body["trade_short"])
                self.cfg.trade_short = self.engine.trade_short
            if "min_order_distance" in body and body["min_order_distance"] not in (None, ""):
                self.engine.min_order_distance = max(0.0, float(body["min_order_distance"]))
                self.cfg.min_order_distance = self.engine.min_order_distance
            if "min_v2_gap" in body and body["min_v2_gap"] not in (None, ""):
                self.engine.min_v2_gap = max(0.05, float(body["min_v2_gap"]))
                self.cfg.min_v2_gap = self.engine.min_v2_gap
            if "v1_offset" in body and body["v1_offset"] not in (None, ""):
                self.engine.v1_offset = max(-2.0, min(5.0, float(body["v1_offset"])))
                self.cfg.v1_offset = self.engine.v1_offset
            if "tp_offset" in body and body["tp_offset"] not in (None, ""):
                self.engine.tp_offset = max(0.0, min(2.0, float(body["tp_offset"])))
                self.cfg.tp_offset = self.engine.tp_offset
            if "stop_loss_pct" in body and body["stop_loss_pct"] not in (None, ""):
                self.engine.stop_loss_pct = max(0.0, min(5.0, float(body["stop_loss_pct"])))
                self.cfg.stop_loss_pct = self.engine.stop_loss_pct
            if "v1_fail_bump" in body and body["v1_fail_bump"] not in (None, ""):
                self.engine.v1_fail_bump = max(0.0, min(2.0, float(body["v1_fail_bump"])))
                self.cfg.v1_fail_bump = self.engine.v1_fail_bump
            if "pair_lose_limit" in body and body["pair_lose_limit"] not in (None, ""):
                self.engine.pair_lose_limit = max(1, int(float(body["pair_lose_limit"])))
                self.cfg.pair_lose_limit = self.engine.pair_lose_limit
            if "pair_lose_window_hours" in body and body["pair_lose_window_hours"] not in (None, ""):
                self.engine.pair_lose_window_hours = max(0.1, float(body["pair_lose_window_hours"]))
                self.cfg.pair_lose_window_hours = self.engine.pair_lose_window_hours
            if "pair_ban_hours" in body and body["pair_ban_hours"] not in (None, ""):
                self.engine.pair_ban_hours = max(0.1, float(body["pair_ban_hours"]))
                self.cfg.pair_ban_hours = self.engine.pair_ban_hours
            if "min_fills" in body and body["min_fills"] not in (None, ""):
                self.engine.min_fills = max(1, int(float(body["min_fills"])))
                self.cfg.min_fills = self.engine.min_fills
        except (TypeError, ValueError) as exc:
            return web.json_response({"ok": False, "error": f"неверное значение: {exc}"}, status=400)
        if "shotcore_url" in body:
            self._apply_shotcore_url(str(body.get("shotcore_url") or ""))
        self.engine.apply_runtime_settings()
        await self._push_min_fills()
        self.engine.note(
            f"настройки: x20={self.engine.order_size_x20:g} x50={self.engine.order_size_x50:g} "
            f"autostop={self.engine.autostop_usd:g}$ "
            f"long={'on' if self.engine.trade_long else 'off'} "
            f"short={'on' if self.engine.trade_short else 'off'} "
            f"minD={self.engine.min_order_distance:g}% v2gap={self.engine.min_v2_gap:g}% "
            f"v1off={self.engine.v1_offset:+g}% tpoff=+{self.engine.tp_offset:g}% "
            f"SL={self.engine.stop_loss_pct:g}% "
            f"v1fail=+{self.engine.v1_fail_bump:g}% "
            f"бан={self.engine.pair_lose_limit} подряд / "
            f"→ {self.engine.pair_ban_hours:g}ч  подтверждений={self.engine.min_fills}"
        )
        return web.json_response(
            {
                "ok": True,
                "order_size": self.engine.order_size_x50,
                "order_size_x20": self.engine.order_size_x20,
                "order_size_x50": self.engine.order_size_x50,
                "leverage": self.engine.leverage,
                "autostop_usd": self.engine.autostop_usd,
                "trade_long": self.engine.trade_long,
                "trade_short": self.engine.trade_short,
                "min_order_distance": self.engine.min_order_distance,
                "min_v2_gap": self.engine.min_v2_gap,
                "v1_offset": self.engine.v1_offset,
                "tp_offset": self.engine.tp_offset,
                "stop_loss_pct": self.engine.stop_loss_pct,
                "v1_fail_bump": self.engine.v1_fail_bump,
                "pair_lose_limit": self.engine.pair_lose_limit,
                "pair_lose_window_hours": self.engine.pair_lose_window_hours,
                "pair_ban_hours": self.engine.pair_ban_hours,
                "min_fills": self.engine.min_fills,
            }
        )

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
        if symbol and not self.engine.is_banned(symbol):
            self.engine.view_symbol = symbol
        return web.json_response({"ok": True})

    async def _resume(self, _request: web.Request) -> web.Response:
        self.engine.resume()
        return web.json_response({"ok": True})

    async def _panic(self, _request: web.Request) -> web.Response:
        self.engine.emergency("panic из UI")
        return web.json_response({"ok": True})

    async def _reports(self, _request: web.Request) -> web.Response:
        self.engine.roll_calendar_day()
        return web.json_response(self.engine.reports())

    async def _plan_loop(self) -> None:
        fail_sleep = 8
        while True:
            try:
                plan = await self._fetch_plan()
                raw = list(plan.get("pairs") or [])
                # План уже отфильтрован ShotCore (D>0 и плюс ≥ min_win). Здесь только валидность.
                pairs = [
                    p
                    for p in raw
                    if float(p.get("buy_pct") or 0) > 0
                    or float(p.get("sell_pct") or 0) > 0
                    or float(p.get("recommend_pct") or 0) > 0
                ]
                started = self.engine.sync_plan(pairs)
                self.engine.plan_error = ""
                self.engine.plan_updated_ts = int(time.time() * 1000)
                self._refresh_desired()
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

    def _shotcore_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = (self.cfg.shotcore_token or "").strip()
        if token:
            headers["X-Shot-Token"] = token
        return headers

    async def _push_min_fills(self) -> None:
        if self.session is None:
            return
        url = f"{self.cfg.shotcore_url}/api/algo"
        try:
            async with self.session.post(
                url,
                json={"min_fills": int(self.engine.min_fills)},
                headers=self._shotcore_headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    self.engine.note(f"порог подтверждений → ShotCore: {resp.status} {text[:120]}")
                    return
                data = await resp.json()
                got = data.get("min_fills")
                self.engine.note(f"порог подтверждений: {self.engine.min_fills} (ShotCore {got})")
        except Exception as exc:
            self.engine.note(f"порог подтверждений не ушёл в ShotCore: {exc}")

    async def _fetch_plan(self) -> dict[str, Any]:
        assert self.session is not None
        params = {"lookback": str(self.cfg.lookback_min), "min_fills": str(self.engine.min_fills)}
        headers = self._shotcore_headers()
        token = (self.cfg.shotcore_token or "").strip()
        if token:
            params["token"] = token
        url = f"{self.cfg.shotcore_url}/api/mt-plan?{urlencode(params)}"
        async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            final = str(resp.url)
            if resp.status != 200:
                text = await resp.text()
                if resp.status == 401:
                    raise RuntimeError(
                        "401 unauthorized — задайте одинаковый WEB_TOKEN/SHOTCORE_TOKEN "
                        f"(или SESSION_SECRET) на ShotCore и ShotTrader. {text[:120]}"
                    )
                raise RuntimeError(f"{resp.status} {text[:200]}")
            if "json" not in ctype or "html" in ctype or "/users/sign_in" in final:
                raise RuntimeError(
                    f"SHOTCORE_URL={self.cfg.shotcore_url} указывает не на ShotCore, "
                    f"а на другой сайт ({final}). "
                    "В shottrader/.env поставьте внутренний адрес ядра "
                    "(например http://192.168.1.26:4861 или http://shotcore:4861), "
                    "не внешний IP:4861."
                )
            return await resp.json()

    async def _follow_loop(self) -> None:
        while True:
            try:
                await self.engine.follow_once()
                self._refresh_desired()
            except Exception as exc:
                log.debug("follow: %s", exc)
            await asyncio.sleep(0.25)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            closed = self.engine.roll_calendar_day()
            if closed is not None:
                await self._send_report(f"Сутки закрыты {closed.get('date')}", closed)
            now = asyncio.get_event_loop().time()
            hour = self.engine.window_stats(1)
            if now - self._last_hour_report >= 3600:
                self._last_hour_report = now
                await self._send_report("Часовой отчёт", hour)

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
