from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import aiohttp
from aiohttp import web

from .config import AppConfig, load_config
from .detector import BtcDeltaTracker, SymbolDetector
from .okx_rest import OkxRest, apply_market_filters
from .okx_ws import OkxPublicFeed
from .store import ShotStore
from .webapp import build_app

log = logging.getLogger("shotcore")


class ShotCore:
    def __init__(self, cfg: AppConfig, root: Path):
        self.cfg = cfg
        self.root = root
        self.store = ShotStore(
            directory=root / cfg.output.dir,
            csv_name=cfg.output.csv_name,
            jsonl_name=cfg.output.jsonl_name,
            hints_name=cfg.output.hints_name,
            tz_name=cfg.web.timezone,
            distance_levels=cfg.shot.distance_levels,
        )
        self.btc = BtcDeltaTracker(
            cfg.btc_filter.symbol,
            cfg.btc_filter.window_sec,
            cfg.btc_filter.range_pct,
        )
        self.detectors: dict[str, SymbolDetector] = {}
        self.active_symbols: list[str] = []
        self.feed = OkxPublicFeed(
            cfg.exchange.ws_public,
            self._on_trade_sync,
            batch_size=cfg.market.ws_batch_size,
            symbols_per_connection=cfg.market.symbols_per_connection,
        )
        self._session: aiohttp.ClientSession | None = None
        self._tg_queue: asyncio.Queue[str] = asyncio.Queue()

    def _detector(self, symbol: str) -> SymbolDetector:
        det = self.detectors.get(symbol)
        if det is None:
            det = SymbolDetector(
                symbol=symbol,
                windows_ms=self.cfg.shot.windows_ms,
                min_percent=self.cfg.shot.min_percent,
                min_trades=self.cfg.shot.min_trades,
                min_quote_volume=self.cfg.shot.min_quote_volume,
                cooldown_ms=self.cfg.shot.cooldown_ms,
                recover_ratio=self.cfg.shot.recover_ratio,
                hold_ms=self.cfg.shot.hold_ms,
                distance_levels=self.cfg.shot.distance_levels,
                vplus_min_pnl=self.cfg.shot.vplus_min_pnl,
            )
            self.detectors[symbol] = det
        return det

    def _on_trade_sync(self, symbol: str, ts: int, price: float, qty: float, side: str) -> None:
        self.btc.on_trade(symbol, ts, price)
        events = self._detector(symbol).on_trade(ts, price, qty, side)
        if not events:
            return
        btc_delta, btc_calm = self.btc.snapshot(ts)
        for event in events:
            event.btc_delta_pct = btc_delta
            event.btc_calm = btc_calm
            self.store.write(event)
            if (
                self.cfg.notify.telegram_bot_token
                and event.percent >= self.cfg.notify.telegram_min_percent
            ):
                text = (
                    f"{'В+' if event.vplus else 'В−'} {event.direction} {event.symbol} "
                    f"прострел {event.percent:.2f}%  ордер {event.suggest_distance:.2f}%  "
                    f"PnL {event.pnl_pct:+.3f}% / {event.hold_ms}мс"
                )
                try:
                    self._tg_queue.put_nowait(text)
                except asyncio.QueueFull:
                    pass

    async def refresh_universe(self) -> None:
        assert self._session is not None
        rest = OkxRest(self.cfg, self._session)
        instruments = await rest.fetch_universe()
        selected = apply_market_filters(instruments, self.cfg.market, self.cfg.filters)
        symbols = [item.inst_id for item in selected]
        btc = self.cfg.btc_filter.symbol
        if btc not in symbols:
            symbols.append(btc)
        if symbols == self.active_symbols:
            return
        log.info(
            "Universe refresh: %s symbols (QAV %.0f ... %.0f)",
            len(selected),
            self.cfg.filters.qav_24h_min,
            self.cfg.filters.qav_24h_max,
        )
        self.active_symbols = symbols
        await self.feed.set_symbols(symbols)

    async def _telegram_worker(self) -> None:
        token = self.cfg.notify.telegram_bot_token
        chat = self.cfg.notify.telegram_chat_id
        if not token or not chat or self._session is None:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        while True:
            text = await self._tg_queue.get()
            try:
                await self._session.post(url, json={"chat_id": chat, "text": text})
            except Exception as exc:
                log.warning("Telegram send failed: %s", exc)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            runner = await self._start_web()
            tg_task = asyncio.create_task(self._telegram_worker())
            try:
                await self.refresh_universe()
                while True:
                    await asyncio.sleep(self.cfg.market.refresh_sec)
                    try:
                        await self.refresh_universe()
                        if self.store.total:
                            self.store.write_hints(self.cfg.web.stats_lookback_min)
                    except Exception as exc:
                        log.warning("Universe refresh failed: %s", exc)
            finally:
                tg_task.cancel()
                await self.feed.stop()
                await runner.cleanup()
                if self.store.total:
                    self.store.write_hints(self.cfg.web.stats_lookback_min)

    async def _start_web(self) -> web.AppRunner:
        app = build_app(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.web.host, self.cfg.web.port)
        await site.start()
        log.info("Dashboard: http://%s:%s/", self.cfg.web.host, self.cfg.web.port)
        return runner


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(log_dir / "shotcore.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file_handler)


async def _amain(config_path: Path) -> None:
    cfg = load_config(config_path)
    root = config_path.parent if config_path.exists() else Path.cwd()
    setup_logging(root / "logs")
    core = ShotCore(cfg, root)
    log.info("ShotCore start, config=%s lookback=%smin", config_path, cfg.web.stats_lookback_min)
    stop = asyncio.Event()

    def _stop(*_args: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass
    task = asyncio.create_task(core.run())
    await stop.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ShotCore: OKX futures shot logger")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    asyncio.run(_amain(Path(args.config).resolve()))
