from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import TraderConfig
from .okx_broker import OkxBroker

log = logging.getLogger("shottrader.engine")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pnl_usd(side: str, entry: float, exit_px: float, size_usdt: float) -> float:
    if entry <= 0 or size_usdt <= 0:
        return 0.0
    chg = (exit_px - entry) / entry if side == "buy" else (entry - exit_px) / entry
    return round(size_usdt * chg, 4)


@dataclass
class Tape:
    pts: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=4000))
    ticks: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    last: float = 0.0

    def push(self, ts: int, price: float, side: str, qty: float) -> None:
        if price <= 0:
            return
        self.last = price
        self.pts.append((ts, price))
        self.ticks.append({"ts": ts, "price": price, "side": side, "qty": qty})
        cutoff = ts - 20_000
        while self.pts and self.pts[0][0] < cutoff:
            self.pts.popleft()

    def delayed(self, delay_ms: int, now: int | None = None) -> float:
        now = now or _now_ms()
        target = now - delay_ms
        px = 0.0
        for ts, price in self.pts:
            if ts <= target:
                px = price
            else:
                break
        return px or self.last


@dataclass
class Algo:
    symbol: str
    distance: float
    tp: float
    lever: int
    size_usdt: float
    started_ts: int
    until_ts: int
    buy_px: float = 0.0
    sell_px: float = 0.0
    buy_id: str = ""
    sell_id: str = ""
    state: str = "hunt"
    pos_side: str = ""
    entry: float = 0.0
    fill_ts: int = 0
    qty: str = "1"
    fingerprint: str = ""


class ShotEngine:
    def __init__(self, cfg: TraderConfig, broker: OkxBroker | None, data_dir: Path):
        self.cfg = cfg
        self.broker = broker
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = data_dir / "trader_journal.jsonl"
        self.tapes: dict[str, Tape] = defaultdict(Tape)
        self.algos: dict[str, Algo] = {}
        self.log_lines: deque[str] = deque(maxlen=200)
        self.journal: deque[dict[str, Any]] = deque(maxlen=400)
        self.emulate = cfg.emulate or not (broker and broker.ready and cfg.live_trading)
        self.order_size = cfg.order_size_usdt
        self.leverage = cfg.leverage
        self.autostop_usd = cfg.autostop_usd
        self.halted = False
        self.halt_reason = ""
        self.view_symbol = ""
        self.plan_pairs: list[dict[str, Any]] = []
        self.plan_error = ""
        self.plan_updated_ts = 0
        self._load_journal()

    def note(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {text}"
        self.log_lines.append(line)
        log.info("%s", text)

    def _load_journal(self) -> None:
        if not self.journal_path.is_file():
            return
        try:
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()[-400:]:
                if line.strip():
                    self.journal.append(json.loads(line))
        except Exception:
            pass

    def _write_trade(self, row: dict[str, Any]) -> None:
        self.journal.append(row)
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def on_trade(self, symbol: str, ts: int, price: float, qty: float, side: str) -> None:
        self.tapes[symbol].push(ts, price, side, qty)
        if self.halted:
            return
        algo = self.algos.get(symbol)
        if not algo:
            return
        self._check_fill(algo, ts, price)
        self._maybe_exit(algo, ts, price)
        self._check_open_loss(algo, price)

    def sync_plan(self, pairs: list[dict[str, Any]]) -> list[str]:
        self.plan_pairs = pairs
        wanted: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            symbol = str(pair.get("symbol") or "").upper()
            dist = round(float(pair.get("recommend_pct") or 0), 2)
            tp = round(float(pair.get("tp_pct") or 0), 2)
            if not symbol or dist <= 0:
                continue
            wanted[symbol] = pair
        started: list[str] = []
        now = _now_ms()
        for symbol, algo in list(self.algos.items()):
            if now >= algo.until_ts:
                self.note(f"{symbol} 3ч истекли — снимаю")
                self._kill(symbol, "expired")
                continue
            fresh = wanted.get(symbol)
            if fresh is None:
                continue
            fp = f"{round(float(fresh.get('recommend_pct') or 0), 2)}|{round(float(fresh.get('tp_pct') or 0), 2)}"
            if fp != algo.fingerprint:
                self.note(f"{symbol} новая рекомендация {fp} — перезапуск клона")
                self._kill(symbol, "replaced")
        for symbol, pair in wanted.items():
            if self.halted:
                break
            if symbol in self.algos:
                continue
            self._start(pair)
            started.append(symbol)
        if not self.view_symbol and self.algos:
            self.view_symbol = next(iter(self.algos))
        return started

    def _start(self, pair: dict[str, Any]) -> None:
        symbol = str(pair.get("symbol") or "").upper()
        dist = round(float(pair.get("recommend_pct") or 0), 2)
        tp = round(float(pair.get("tp_pct") or 0), 2)
        lever = int(pair.get("lever") or self.leverage) or self.leverage
        now = _now_ms()
        algo = Algo(
            symbol=symbol,
            distance=dist,
            tp=tp,
            lever=lever,
            size_usdt=self.order_size,
            started_ts=now,
            until_ts=now + int(self.cfg.run_hours * 3600 * 1000),
            fingerprint=f"{dist}|{tp}",
        )
        self.algos[symbol] = algo
        self.note(
            f"старт {symbol} D{dist}% TP{tp}% x{lever} size={self.order_size:g} "
            f"{'эмуляция' if self.emulate else 'LIVE'} на {self.cfg.run_hours:g}ч"
        )

    def _kill(self, symbol: str, reason: str) -> None:
        algo = self.algos.pop(symbol, None)
        if not algo:
            return
        self.note(f"стоп {symbol} ({reason})")
        if self.emulate or not self.broker:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if algo.buy_id:
            loop.create_task(self.broker.cancel(symbol, algo.buy_id))
        if algo.sell_id:
            loop.create_task(self.broker.cancel(symbol, algo.sell_id))
        if algo.state == "pos" and algo.entry > 0:
            close_side = "sell" if algo.pos_side == "buy" else "buy"
            loop.create_task(self.broker.close_market(symbol, close_side, algo.qty))

    async def follow_once(self) -> None:
        if self.halted:
            return
        now = _now_ms()
        for algo in list(self.algos.values()):
            if now >= algo.until_ts:
                self._kill(algo.symbol, "expired")
                continue
            if algo.state != "hunt":
                continue
            px = self.tapes[algo.symbol].delayed(self.cfg.follow_delay_ms, now)
            if px <= 0:
                continue
            buy = px * (1.0 - algo.distance / 100.0)
            sell = px * (1.0 + algo.distance / 100.0)
            moved = abs(buy - algo.buy_px) / px * 100.0 if algo.buy_px else 99
            if moved < 0.01:
                continue
            algo.buy_px = buy
            algo.sell_px = sell
            if self.emulate or not (self.broker and self.broker.ready):
                continue
            try:
                await self.broker.load_spec(algo.symbol)
                algo.qty = self.broker.contracts_for(algo.symbol, algo.size_usdt, px)
                bpx = self.broker.round_px(algo.symbol, buy)
                spx = self.broker.round_px(algo.symbol, sell)
                if algo.buy_id:
                    await self.broker.amend(algo.symbol, algo.buy_id, bpx)
                else:
                    algo.buy_id = await self.broker.place_limit(algo.symbol, "buy", bpx, algo.qty)
                if algo.sell_id:
                    await self.broker.amend(algo.symbol, algo.sell_id, spx)
                else:
                    algo.sell_id = await self.broker.place_limit(algo.symbol, "sell", spx, algo.qty)
            except Exception as exc:
                self.note(f"{algo.symbol} follow: {exc}")

    def _check_fill(self, algo: Algo, ts: int, price: float) -> None:
        if algo.state != "hunt" or price <= 0:
            return
        if algo.buy_px > 0 and price <= algo.buy_px:
            self._enter(algo, "buy", algo.buy_px, ts)
        elif algo.sell_px > 0 and price >= algo.sell_px:
            self._enter(algo, "sell", algo.sell_px, ts)

    def _enter(self, algo: Algo, side: str, px: float, ts: int) -> None:
        algo.state = "pos"
        algo.pos_side = side
        algo.entry = px
        algo.fill_ts = ts
        if not self.emulate and self.broker:
            try:
                loop = asyncio.get_running_loop()
                other = algo.sell_id if side == "buy" else algo.buy_id
                if other:
                    loop.create_task(self.broker.cancel(algo.symbol, other))
            except RuntimeError:
                pass
        algo.buy_id = ""
        algo.sell_id = ""
        self.note(f"вход {algo.symbol} {side.upper()} @ {px:.6g} D{algo.distance}%")

    def _maybe_exit(self, algo: Algo, ts: int, price: float) -> None:
        if algo.state != "pos" or algo.entry <= 0:
            return
        favor = (price - algo.entry) / algo.entry * 100.0 if algo.pos_side == "buy" else (algo.entry - price) / algo.entry * 100.0
        hit_tp = algo.tp > 0 and favor + 1e-12 >= algo.tp
        timed = ts - algo.fill_ts >= self.cfg.hold_ms
        if not hit_tp and not timed:
            return
        exit_px = algo.entry * (1 + algo.tp / 100.0) if hit_tp and algo.pos_side == "buy" else (
            algo.entry * (1 - algo.tp / 100.0) if hit_tp else price
        )
        self._close(algo, exit_px, "TP" if hit_tp else "0.3с")

    def _close(self, algo: Algo, exit_px: float, why: str) -> None:
        pnl = _pnl_usd(algo.pos_side, algo.entry, exit_px, algo.size_usdt)
        row = {
            "ts": _now_ms(),
            "symbol": algo.symbol,
            "side": algo.pos_side,
            "entry": algo.entry,
            "exit": exit_px,
            "pnl_usd": pnl,
            "why": why,
            "distance": algo.distance,
            "tp": algo.tp,
            "emulate": self.emulate,
        }
        self._write_trade(row)
        if not self.emulate and self.broker and self.broker.ready:
            try:
                loop = asyncio.get_running_loop()
                close_side = "sell" if algo.pos_side == "buy" else "buy"
                loop.create_task(self.broker.close_market(algo.symbol, close_side, algo.qty))
            except RuntimeError:
                pass
        self.note(f"выход {algo.symbol} {why} pnl {pnl:+.2f}$")
        algo.state = "hunt"
        algo.pos_side = ""
        algo.entry = 0.0
        algo.fill_ts = 0
        algo.buy_px = 0.0
        algo.sell_px = 0.0
        if pnl <= -self.autostop_usd:
            self.emergency(f"сделка {algo.symbol} {pnl:.2f}$ ≤ -{self.autostop_usd:g}$")

    def _check_open_loss(self, algo: Algo, price: float) -> None:
        if algo.state != "pos" or algo.entry <= 0:
            return
        pnl = _pnl_usd(algo.pos_side, algo.entry, price, algo.size_usdt)
        if pnl <= -self.autostop_usd:
            self._close(algo, price, "autostop")
            self.emergency(f"открытый минус {algo.symbol} {pnl:.2f}$")

    def emergency(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.note(f"АВТО-СТОП: {reason} — снимаю все ордера")
        for symbol in list(self.algos):
            self._kill(symbol, "autostop")

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.note("авто-стоп снят, жду новые записи плана")

    def window_stats(self, hours: float) -> dict[str, Any]:
        cutoff = _now_ms() - int(hours * 3600 * 1000)
        rows = [r for r in self.journal if int(r.get("ts") or 0) >= cutoff]
        pnl = sum(float(r.get("pnl_usd") or 0) for r in rows)
        plus = sum(1 for r in rows if float(r.get("pnl_usd") or 0) > 0)
        return {
            "trades": len(rows),
            "plus": plus,
            "minus": len(rows) - plus,
            "pnl_usd": round(pnl, 4),
        }

    def unrealized(self) -> float:
        total = 0.0
        for algo in self.algos.values():
            if algo.state != "pos" or algo.entry <= 0:
                continue
            last = self.tapes[algo.symbol].last
            if last <= 0:
                continue
            total += _pnl_usd(algo.pos_side, algo.entry, last, algo.size_usdt)
        return round(total, 4)

    def snapshot(self) -> dict[str, Any]:
        hour = self.window_stats(1)
        day = self.window_stats(24)
        view = self.view_symbol or (next(iter(self.algos), "") or "")
        markets: list[dict[str, Any]] = []
        for a in self.algos.values():
            last = float(self.tapes[a.symbol].last or 0)
            buy_dist = ((last - a.buy_px) / last * 100.0) if last > 0 and a.buy_px > 0 else None
            sell_dist = ((a.sell_px - last) / last * 100.0) if last > 0 and a.sell_px > 0 else None
            u_pnl = 0.0
            if a.state == "pos" and a.entry > 0 and last > 0:
                u_pnl = _pnl_usd(a.pos_side, a.entry, last, a.size_usdt)
            markets.append(
                {
                    "symbol": a.symbol,
                    "last": last,
                    "distance": a.distance,
                    "tp": a.tp,
                    "lever": a.lever,
                    "state": a.state,
                    "side": a.pos_side,
                    "entry": a.entry,
                    "buy": a.buy_px,
                    "sell": a.sell_px,
                    "buy_dist_pct": None if buy_dist is None else round(buy_dist, 4),
                    "sell_dist_pct": None if sell_dist is None else round(sell_dist, 4),
                    "unrealized": round(u_pnl, 4),
                    "left_min": max(0, int((a.until_ts - _now_ms()) / 60000)),
                }
            )
        markets.sort(key=lambda row: row["symbol"])
        return {
            "emulate": self.emulate,
            "live": (not self.emulate) and bool(self.broker and self.broker.ready),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "order_size": self.order_size,
            "leverage": self.leverage,
            "autostop_usd": self.autostop_usd,
            "follow_delay_ms": self.cfg.follow_delay_ms,
            "hold_ms": self.cfg.hold_ms,
            "run_hours": self.cfg.run_hours,
            "unrealized": self.unrealized(),
            "hour": hour,
            "day": day,
            "view": view,
            "markets": markets,
            "plan": self.plan_pairs,
            "plan_error": self.plan_error,
            "plan_updated_ts": self.plan_updated_ts,
            "shotcore_url": self.cfg.shotcore_url,
            "algos": markets,
            "journal": list(self.journal)[-40:],
            "log": list(self.log_lines)[-60:],
        }
