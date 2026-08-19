from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .config import ActiveMarketsConfig
from .okx_rest import Instrument, OkxRest

log = logging.getLogger("shotcore.active")


class _Pace:
    def __init__(self, n: int = 18, window: float = 2.0):
        self.n = n
        self.window = window
        self.times: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        while True:
            async with self.lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                while self.times and now - self.times[0] >= self.window:
                    self.times.popleft()
                if len(self.times) < self.n:
                    self.times.append(now)
                    return
                delay = self.window - (now - self.times[0]) + 0.02
            await asyncio.sleep(max(delay, 0.02))


@dataclass
class ActiveMarket:
    inst_id: str
    lever: float
    delta_1h: float
    open_delta_15m: float
    qav_24h: float
    rank: int = 0
    ignored: bool = False
    subscribed: bool = False

    def as_public(self) -> dict[str, Any]:
        return asdict(self)


def metrics_from_candles(rows: list[Any]) -> tuple[float, float]:
    """1ч Δ = high−low range % over ~4×15m. 15м оΔ = (close−open)/open of latest 15m."""
    parsed: list[tuple[float, float, float, float]] = []
    for row in rows:
        try:
            open_px = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if open_px <= 0:
            continue
        parsed.append((open_px, high, low, close))
    if not parsed:
        return 0.0, 0.0
    latest_open, _h, _l, latest_close = parsed[0]
    open_delta_15m = (latest_close - latest_open) / latest_open * 100.0
    highs = [item[1] for item in parsed]
    lows = [item[2] for item in parsed]
    oldest_open = parsed[-1][0]
    delta_1h = (max(highs) - min(lows)) / oldest_open * 100.0 if oldest_open else 0.0
    return round(delta_1h, 4), round(open_delta_15m, 4)


async def rank_active_markets(
    rest: OkxRest,
    instruments: list[Instrument],
    cfg: ActiveMarketsConfig,
    concurrency: int = 10,
) -> list[ActiveMarket]:
    if not instruments:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))
    pace = _Pace(n=18, window=2.0)

    async def _one(inst: Instrument) -> ActiveMarket:
        async with sem:
            await pace.wait()
            try:
                rows = await rest.fetch_candles(inst.inst_id, bar="15m", limit=4)
            except Exception as exc:
                log.debug("candles %s: %s", inst.inst_id, exc)
                rows = []
        delta_1h, open_delta_15m = metrics_from_candles(rows)
        return ActiveMarket(
            inst_id=inst.inst_id,
            lever=inst.lever,
            delta_1h=delta_1h,
            open_delta_15m=open_delta_15m,
            qav_24h=inst.qav_24h,
        )

    scored = await asyncio.gather(*[_one(inst) for inst in instruments])
    ranked = list(scored)
    ranked.sort(key=lambda item: (item.delta_1h, abs(item.open_delta_15m)), reverse=True)
    ignore_first = max(0, cfg.ignore_first)
    max_markets = max(0, cfg.max_markets)
    board: list[ActiveMarket] = []
    for index, item in enumerate(ranked):
        item.rank = index + 1
        item.ignored = index < ignore_first
        item.subscribed = (not item.ignored) and (index < ignore_first + max_markets)
        if index >= ignore_first + max_markets:
            break
        board.append(item)
    log.info(
        "Active markets: ranked %s, skip first %s, subscribe %s (sort %ss)",
        len(ranked),
        ignore_first,
        sum(1 for item in board if item.subscribed),
        cfg.sort_sec,
    )
    return board


def board_from_qav(instruments: list[Instrument], cfg: ActiveMarketsConfig) -> list[ActiveMarket]:
    """Fallback if candle ranking fails: same 25/skip-2 window, order by 24h quote volume."""
    ranked = [
        ActiveMarket(
            inst_id=item.inst_id,
            lever=item.lever,
            delta_1h=0.0,
            open_delta_15m=0.0,
            qav_24h=item.qav_24h,
        )
        for item in sorted(instruments, key=lambda row: row.qav_24h, reverse=True)
    ]
    ignore_first = max(0, cfg.ignore_first)
    max_markets = max(0, cfg.max_markets)
    board: list[ActiveMarket] = []
    for index, item in enumerate(ranked):
        item.rank = index + 1
        item.ignored = index < ignore_first
        item.subscribed = (not item.ignored) and (index < ignore_first + max_markets)
        if index >= ignore_first + max_markets:
            break
        board.append(item)
    return board
