from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque


@dataclass
class Trade:
    ts: int
    price: float
    qty: float
    side: str


@dataclass
class ShotEvent:
    symbol: str
    direction: str
    percent: float
    window_ms: int
    start_ts: int
    peak_ts: int
    start_price: float
    extreme_price: float
    last_price: float
    trades: int
    quote_volume: float
    duration_ms: int
    btc_delta_pct: float
    btc_calm: bool
    pct_300: float = 0.0
    pct_1000: float = 0.0
    pct_3000: float = 0.0
    hold_ms: int = 300
    exit_price: float = 0.0
    rollback_pct: float = 0.0
    vplus: bool = False
    pnl_pct: float = 0.0
    suggest_distance: float = 0.0
    distance_report: list[dict[str, Any]] = field(default_factory=list)
    lever: float = 0.0
    fill_ts: int = 0
    fill_price: float = 0.0


@dataclass
class _OpenShot:
    direction: str
    window_ms: int
    start_ts: int
    start_price: float
    extreme_price: float
    peak_ts: int
    peak_pct: float
    trades: int
    quote_volume: float
    peaked: bool = False


class SymbolDetector:
    def __init__(
        self,
        symbol: str,
        windows_ms: list[int],
        min_percent: float,
        min_trades: int,
        min_quote_volume: float,
        cooldown_ms: int,
        recover_ratio: float,
        hold_ms: int = 300,
        refractory_ms: int = 1_000,
        distance_levels: list[float] | None = None,
        vplus_min_pnl: float = 0.3,
        tp_min_pct: float = 0.3,
        suggest_inside_pct: float = 0.05,
    ):
        self.symbol = symbol
        self.windows_ms = sorted(windows_ms)
        self.min_percent = min_percent
        self.min_trades = min_trades
        self.min_quote_volume = min_quote_volume
        self.cooldown_ms = cooldown_ms
        self.recover_ratio = recover_ratio
        self.hold_ms = hold_ms
        self.distance_levels = distance_levels or [1.11, 1.32, 1.42, 1.63, 1.78]
        self.tp_min_pct = max(float(tp_min_pct), float(vplus_min_pnl), 0.0)
        self.vplus_min_pnl = self.tp_min_pct
        self.suggest_inside_pct = max(float(suggest_inside_pct), 0.0)
        self.trades: Deque[Trade] = deque()
        self._open: dict[str, _OpenShot] = {}
        self._refractory_until: dict[str, int] = {}
        self.max_keep_ms = max(self.windows_ms) + cooldown_ms + hold_ms + 20000
        self.quiet_ms = max(hold_ms, 400)
        self.refractory_ms = max(refractory_ms, hold_ms)
        self.max_shot_ms = 4000

    def on_trade(self, ts: int, price: float, qty: float, side: str) -> list[ShotEvent]:
        if price <= 0:
            return []
        trade = Trade(ts=ts, price=price, qty=qty, side=side)
        self.trades.append(trade)
        cutoff = ts - self.max_keep_ms
        while self.trades and self.trades[0].ts < cutoff:
            self.trades.popleft()

        metrics = self._window_metrics(ts, price)
        best: dict[str, tuple[int, float, float, float, int, float]] = {}
        for window_ms, (ref, high, low, count, qvol) in metrics.items():
            if ref <= 0 or count < self.min_trades or qvol < self.min_quote_volume:
                continue
            down_pct = (ref - low) / ref * 100.0
            up_pct = (high - ref) / ref * 100.0
            for direction, extreme, pct in (("DOWN", low, down_pct), ("UP", high, up_pct)):
                prev = best.get(direction)
                if prev is None or pct > prev[1]:
                    best[direction] = (window_ms, pct, ref, extreme, count, qvol)

        for direction, (window_ms, pct, ref, extreme, count, qvol) in best.items():
            opened = self._open.get(direction)
            if opened is None:
                locked = ts < self._refractory_until.get(direction, 0)
                if locked:
                    if pct < self.min_percent * 0.5:
                        self._refractory_until.pop(direction, None)
                    continue
                if pct >= self.min_percent:
                    self._open[direction] = _OpenShot(
                        direction=direction,
                        window_ms=window_ms,
                        start_ts=ts,
                        start_price=ref,
                        extreme_price=extreme,
                        peak_ts=ts,
                        peak_pct=pct,
                        trades=count,
                        quote_volume=qvol,
                    )
                continue
            if pct > opened.peak_pct:
                opened.peak_pct = pct
                opened.window_ms = window_ms
                opened.extreme_price = extreme
                opened.peak_ts = ts
                opened.trades = count
                opened.quote_volume = qvol
                opened.peaked = False

        closed: list[ShotEvent] = []
        for direction in list(self._open):
            opened = self._open[direction]
            quiet = ts - opened.peak_ts >= self.quiet_ms
            timed_out = ts - opened.start_ts >= self.max_shot_ms
            if not quiet and not timed_out:
                continue
            event = self._finish(opened, price, metrics)
            self._drop(direction, ts)
            if event is not None:
                closed.append(event)
        return closed

    def _drop(self, direction: str, ts: int) -> None:
        self._refractory_until[direction] = ts + self.refractory_ms
        self._open.pop(direction, None)

    def _finish(
        self,
        opened: _OpenShot,
        last_price: float,
        metrics: dict[int, tuple[float, float, float, int, float]],
    ) -> ShotEvent | None:
        extras = {}
        for window_ms, (ref, high, low, _count, _qvol) in metrics.items():
            if ref <= 0:
                continue
            if opened.direction == "DOWN":
                extras[window_ms] = (ref - low) / ref * 100.0
            else:
                extras[window_ms] = (high - ref) / ref * 100.0
        report, suggest, pnl, vplus, exit_price, rollback, fill_ts, fill_price = self._simulate(opened, last_price)
        return ShotEvent(
            symbol=self.symbol,
            direction=opened.direction,
            percent=round(opened.peak_pct, 4),
            window_ms=opened.window_ms,
            start_ts=opened.start_ts,
            peak_ts=opened.peak_ts,
            start_price=opened.start_price,
            extreme_price=opened.extreme_price,
            last_price=last_price,
            trades=opened.trades,
            quote_volume=round(opened.quote_volume, 2),
            duration_ms=max(0, opened.peak_ts - opened.start_ts),
            btc_delta_pct=0.0,
            btc_calm=False,
            pct_300=round(extras.get(500, extras.get(300, 0.0)), 4),
            pct_1000=round(extras.get(700, extras.get(1000, 0.0)), 4),
            pct_3000=round(extras.get(1200, extras.get(3000, 0.0)), 4),
            hold_ms=self.hold_ms,
            exit_price=exit_price,
            rollback_pct=round(rollback, 4),
            vplus=vplus,
            pnl_pct=round(pnl, 4),
            suggest_distance=suggest,
            distance_report=report,
            fill_ts=fill_ts,
            fill_price=fill_price,
        )

    def _simulate(
        self, opened: _OpenShot, last_price: float
    ) -> tuple[list[dict[str, Any]], float, float, bool, float, float, int, float]:
        rollback = self._bounce_from_extreme(opened, last_price)
        suggest = round(max(0.01, opened.peak_pct - self.suggest_inside_pct), 2)
        report = [
            self._simulate_distance(opened, distance, last_price)
            for distance in self._distances_to_sim(opened.peak_pct)
        ]
        chosen = next((row for row in report if abs(float(row["distance"]) - suggest) < 1e-6), None)
        if chosen is None:
            chosen = self._simulate_distance(opened, suggest, last_price)
            report.append(chosen)
        return (
            report,
            suggest,
            float(chosen.get("pnl_pct") or 0),
            bool(chosen.get("vplus")),
            float(chosen.get("exit_price") or last_price),
            rollback,
            int(chosen.get("fill_ts") or 0),
            float(chosen.get("fill_price") or 0),
        )

    def _distances_to_sim(self, peak_pct: float) -> list[float]:
        peak = min(max(float(peak_pct), 0.0), 20.0)
        dists: set[float] = set()
        for level in self.distance_levels:
            value = round(float(level), 2)
            if 0.5 <= value <= peak + 1e-9:
                dists.add(value)
        suggest = round(max(0.5, peak - self.suggest_inside_pct), 2)
        if suggest <= 20.0:
            dists.add(suggest)
        cursor = 0.50
        while cursor <= peak + 1e-9:
            dists.add(round(cursor, 2))
            cursor = round(cursor + (0.10 if cursor >= 3.99 else 0.05), 2)
            if len(dists) >= 100:
                dists.add(round(peak, 2))
                break
        return sorted(dists)

    def _simulate_distance(self, opened: _OpenShot, distance: float, last_price: float) -> dict[str, Any]:
        empty = {
            "distance": distance,
            "filled": False,
            "vplus": False,
            "pnl_pct": 0.0,
            "mfe_pct": 0.0,
            "exit_price": 0.0,
            "fill_ts": 0,
            "fill_price": 0.0,
        }
        if opened.peak_pct + 1e-9 < distance or opened.start_price <= 0:
            return empty
        if opened.direction == "DOWN":
            fill_px = opened.start_price * (1.0 - distance / 100.0)
        else:
            fill_px = opened.start_price * (1.0 + distance / 100.0)
        fill_ts = self._first_cross(opened.start_ts, opened.direction, fill_px)
        if fill_ts is None:
            return empty
        hold_end = fill_ts + self.hold_ms
        hold_px = self._price_at(hold_end, last_price)
        pnl = self._pnl_pct(opened.direction, fill_px, hold_px)
        mfe = self._mfe_until(fill_ts, hold_end, fill_px, opened.direction)
        return {
            "distance": distance,
            "filled": True,
            "vplus": pnl + 1e-12 >= self.tp_min_pct,
            "pnl_pct": round(pnl, 4),
            "mfe_pct": round(max(mfe, pnl, 0.0), 4),
            "exit_price": hold_px,
            "fill_ts": fill_ts,
            "fill_price": fill_px,
        }

    def _mfe_until(self, fill_ts: int, end_ts: int, fill_px: float, direction: str) -> float:
        best = 0.0
        for trade in self.trades:
            if trade.ts < fill_ts:
                continue
            if trade.ts > end_ts:
                break
            pnl = self._pnl_pct(direction, fill_px, trade.price)
            if pnl > best:
                best = pnl
        return best

    def _bounce_from_extreme(self, opened: _OpenShot, last_price: float) -> float:
        if opened.start_price <= 0:
            return 0.0
        if opened.direction == "DOWN":
            best = opened.extreme_price
            for trade in self.trades:
                if trade.ts >= opened.peak_ts:
                    best = max(best, trade.price)
            best = max(best, last_price)
            return (best - opened.extreme_price) / opened.start_price * 100.0
        best = opened.extreme_price
        for trade in self.trades:
            if trade.ts >= opened.peak_ts:
                best = min(best, trade.price)
        if last_price > 0:
            best = min(best, last_price)
        return (opened.extreme_price - best) / opened.start_price * 100.0

    @staticmethod
    def _pnl_pct(direction: str, fill_px: float, exit_px: float) -> float:
        if fill_px <= 0 or exit_px <= 0:
            return 0.0
        if direction == "DOWN":
            return (exit_px - fill_px) / fill_px * 100.0
        return (fill_px - exit_px) / fill_px * 100.0

    def _first_cross(self, start_ts: int, direction: str, fill_px: float) -> int | None:
        for trade in self.trades:
            if trade.ts < start_ts:
                continue
            if direction == "DOWN" and trade.price <= fill_px:
                return trade.ts
            if direction == "UP" and trade.price >= fill_px:
                return trade.ts
        return None

    def _price_at(self, ts: int, fallback: float) -> float:
        chosen = fallback
        for trade in self.trades:
            if trade.ts <= ts:
                chosen = trade.price
            elif trade.ts > ts:
                return chosen if chosen else trade.price
        return chosen or fallback

    def _window_metrics(self, ts: int, current: float) -> dict[int, tuple[float, float, float, int, float]]:
        out: dict[int, tuple[float, float, float, int, float]] = {}
        for window_ms in self.windows_ms:
            start = ts - window_ms
            ref = current
            high = current
            low = current
            count = 0
            qvol = 0.0
            found_ref = False
            for trade in reversed(self.trades):
                if trade.ts < start:
                    ref = trade.price
                    found_ref = True
                    break
                high = max(high, trade.price)
                low = min(low, trade.price)
                count += 1
                qvol += trade.qty * trade.price
            if not found_ref and self.trades:
                ref = self.trades[0].price
            out[window_ms] = (ref, high, low, count, qvol)
        return out


class BtcDeltaTracker:
    def __init__(self, symbol: str, window_sec: int, range_pct: float):
        self.symbol = symbol.upper()
        self.window_ms = window_sec * 1000
        self.range_pct = range_pct
        self.trades: Deque[Trade] = deque()
        self.last_price = 0.0

    def on_trade(self, symbol: str, ts: int, price: float) -> None:
        if symbol.upper() != self.symbol or price <= 0:
            return
        self.last_price = price
        self.trades.append(Trade(ts=ts, price=price, qty=0.0, side=""))
        cutoff = ts - self.window_ms - 2000
        while self.trades and self.trades[0].ts < cutoff:
            self.trades.popleft()

    def snapshot(self, ts: int) -> tuple[float, bool]:
        if not self.trades or self.last_price <= 0:
            return 0.0, True
        start = ts - self.window_ms
        ref = self.trades[0].price
        for trade in self.trades:
            if trade.ts >= start:
                break
            ref = trade.price
        if ref <= 0:
            return 0.0, True
        delta = (self.last_price - ref) / ref * 100.0
        calm = abs(delta) <= self.range_pct
        return round(delta, 4), calm
