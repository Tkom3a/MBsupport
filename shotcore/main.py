from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import aiohttp
from aiohttp import web

from .active_markets import ActiveMarket, board_from_qav, rank_active_markets
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
            retain_hours=cfg.output.retain_hours,
            tp_min_pct=cfg.shot.tp_min_pct,
            hold_ms=cfg.shot.hold_ms,
            suggest_inside_pct=cfg.shot.suggest_inside_pct,
            mt_plan_name=cfg.output.mt_plan_name,
            mt_run_hours=cfg.output.mt_run_hours,
        )
        self.btc = BtcDeltaTracker(
            cfg.btc_filter.symbol,
            cfg.btc_filter.window_sec,
            cfg.btc_filter.range_pct,
        )
        self.detectors: dict[str, SymbolDetector] = {}
        self.active_symbols: list[str] = []
        self.active_board: list[ActiveMarket] = []
        self.leverage: dict[str, float] = {}
        self.universe_size: int = 0
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
                refractory_ms=self.cfg.shot.refractory_ms,
                distance_levels=self.cfg.shot.distance_levels,
                vplus_min_pnl=self.cfg.shot.vplus_min_pnl,
                tp_min_pct=self.cfg.shot.tp_min_pct,
                suggest_inside_pct=self.cfg.shot.suggest_inside_pct,
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
            event.lever = self.leverage.get(event.symbol, 0.0)
            self.store.write(event)
            if (
                self.cfg.notify.telegram_bot_token
                and event.percent >= self.cfg.notify.telegram_min_percent
            ):
                lever = f"x{event.lever:.0f}" if event.lever else ""
                text = (
                    f"{event.direction} {event.symbol} {lever} "
                    f"прострел {event.percent:.2f}%  ордер {event.suggest_distance:.2f}%  "
                    f"TP {event.pnl_pct:+.3f}% (мин {self.cfg.shot.tp_min_pct:.2f}%)"
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
        self.universe_size = len(selected)
        self.leverage = {item.inst_id: item.lever for item in instruments}
        try:
            board = await rank_active_markets(rest, selected, self.cfg.active)
        except Exception as exc:
            log.warning("Active-market ranking failed, fallback to QAV: %s", exc)
            board = board_from_qav(selected, self.cfg.active)
        if not board:
            board = board_from_qav(selected, self.cfg.active)
        self.active_board = board
        symbols = [item.inst_id for item in board if item.subscribed]
        btc = self.cfg.btc_filter.symbol
        if btc not in symbols:
            symbols.append(btc)
            if btc not in self.leverage:
                match = next((item for item in instruments if item.inst_id == btc), None)
                if match:
                    self.leverage[btc] = match.lever
        if symbols == self.active_symbols:
            return
        log.info(
            "Universe refresh: pool %s -> active %s (QAV %.0f-%.0f, lever x%.0f-x%.0f)",
            len(selected),
            len(symbols),
            self.cfg.filters.qav_24h_min,
            self.cfg.filters.qav_24h_max,
            self.cfg.filters.min_leverage,
            self.cfg.filters.max_leverage,
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

    async def _retention_loop(self) -> None:
        hours = self.cfg.output.retain_hours
        interval = max(300, int(self.cfg.output.cleanup_sec))
        while True:
            try:
                dropped = self.store.prune()
                files = self.store.purge_sidecar_files()
                logs_n = purge_old_logs(self.root / "logs", hours)
                if dropped or files or logs_n:
                    log.info(
                        "Retention %sh: shots-%s files-%s logs-%s",
                        hours,
                        dropped,
                        files,
                        logs_n,
                    )
            except Exception as exc:
                log.warning("Retention failed: %s", exc)
            await asyncio.sleep(interval)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._session = session
            runner = await self._start_web()
            tg_task = asyncio.create_task(self._telegram_worker())
            retain_task = asyncio.create_task(self._retention_loop())
            try:
                while True:
                    try:
                        await self.refresh_universe()
                        if self.store.total:
                            self.store.write_hints(
                                self.cfg.web.stats_lookback_min,
                                subscribed=set(self.active_symbols),
                                run_hours=self.cfg.output.mt_run_hours,
                            )
                    except Exception as exc:
                        log.warning("Universe refresh failed: %s", exc)
                    await asyncio.sleep(self.cfg.active.sort_sec)
            finally:
                retain_task.cancel()
                tg_task.cancel()
                await self.feed.stop()
                await runner.cleanup()
                if self.store.total:
                    self.store.write_hints(
                        self.cfg.web.stats_lookback_min,
                        subscribed=set(self.active_symbols),
                        run_hours=self.cfg.output.mt_run_hours,
                    )

    async def _start_web(self) -> web.AppRunner:
        app = build_app(self)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.web.host or "0.0.0.0", int(self.cfg.web.port), reuse_address=True)
        await site.start()
        log.info("Dashboard: http://%s:%s/", self.cfg.web.host, self.cfg.web.port)
        return runner


def purge_old_logs(log_dir: Path, retain_hours: int) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(1, retain_hours) * 3600
    removed = 0
    for path in log_dir.glob("*"):
        if not path.is_file() or path.name == "shotcore.log":
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def setup_logging(log_dir: Path, retain_hours: int = 48) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log_path = log_dir / "shotcore.log"
    max_bytes = 40 * 1024 * 1024
    if log_path.exists() and log_path.stat().st_size > max_bytes:
        stamped = log_dir / ("shotcore.log." + time.strftime("%Y-%m-%d") + ".old")
        try:
            log_path.replace(stamped)
        except OSError:
            pass
    purge_old_logs(log_dir, retain_hours)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    backups = max(2, int(retain_hours / 24) + 1)
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=backups,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file_handler)


async def _amain(config_path: Path) -> None:
    cfg = load_config(config_path)
    root = config_path.parent if config_path.exists() else Path.cwd()
    setup_logging(root / "logs", retain_hours=cfg.output.retain_hours)
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
    task.add_done_callback(lambda _t: stop.set())
    await stop.wait()
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    elif task.exception():
        log.error("ShotCore stopped: %s", task.exception())
        raise task.exception()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="ShotCore: OKX futures shot logger")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    asyncio.run(_amain(Path(args.config).resolve()))
