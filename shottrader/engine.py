from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import TraderConfig
from .okx_broker import OkxBroker

log = logging.getLogger("shottrader.engine")
MIN_TP_PCT = 0.3
KEEP_DAYS = 8  # сегодня + 7 предыдущих


def _now_ms() -> int:
    return int(time.time() * 1000)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Europe/Moscow")
    except Exception:
        return ZoneInfo("UTC")


def _local_date(ts_ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


def _empty_day(date: str) -> dict[str, Any]:
    return {
        "date": date,
        "trades": 0,
        "plus": 0,
        "minus": 0,
        "pnl_usd": 0.0,
        "by_symbol": {},
    }


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


def _sides_from_pair(pair: dict[str, Any]) -> tuple[float, float, float, float]:
    """BUY из DOWN-прострелов, SHORT из UP. Старый план — одна D на обе стороны."""
    buy_d = round(float(pair.get("buy_pct") or 0), 2)
    sell_d = round(float(pair.get("sell_pct") or 0), 2)
    buy_tp = round(float(pair.get("buy_tp_pct") or 0), 2)
    sell_tp = round(float(pair.get("sell_tp_pct") or 0), 2)
    if buy_d <= 0 and sell_d <= 0:
        d = round(float(pair.get("recommend_pct") or 0), 2)
        tp = round(float(pair.get("tp_pct") or 0), 2)
        buy_d = sell_d = d
        buy_tp = sell_tp = tp
    if buy_d > 0 and buy_tp + 1e-9 < MIN_TP_PCT:
        buy_d = 0.0
        buy_tp = 0.0
    if sell_d > 0 and sell_tp + 1e-9 < MIN_TP_PCT:
        sell_d = 0.0
        sell_tp = 0.0
    return buy_d, buy_tp, sell_d, sell_tp


@dataclass
class Algo:
    symbol: str
    distance: float
    tp: float
    buy_distance: float = 0.0
    sell_distance: float = 0.0
    buy_tp: float = 0.0
    sell_tp: float = 0.0
    lever: int = 50
    size_usdt: float = 10.0
    started_ts: int = 0
    until_ts: int = 0
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
        self.days_path = data_dir / "daily_reports.json"
        self.tz = _zone(cfg.timezone)
        self.tapes: dict[str, Tape] = defaultdict(Tape)
        self.algos: dict[str, Algo] = {}
        self.log_lines: deque[str] = deque(maxlen=200)
        self.journal: deque[dict[str, Any]] = deque(maxlen=400)
        self._journal_all: list[dict[str, Any]] = []
        self.days: dict[str, dict[str, Any]] = {}
        self.today_key = _local_date(_now_ms(), self.tz)
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
        self._rebuild_days()

    def note(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp}  {text}"
        self.log_lines.append(line)
        log.info("%s", text)

    def _load_journal(self) -> None:
        if not self.journal_path.is_file():
            return
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
            for line in lines[-400:]:
                if line.strip():
                    self.journal.append(json.loads(line))
            self._journal_all = []
            cutoff = _now_ms() - KEEP_DAYS * 86400 * 1000
            for line in lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if int(row.get("ts") or 0) >= cutoff:
                    self._journal_all.append(row)
        except Exception:
            self._journal_all = list(self.journal)

    def _rebuild_days(self) -> None:
        saved: dict[str, Any] = {}
        if self.days_path.is_file():
            try:
                raw = json.loads(self.days_path.read_text(encoding="utf-8"))
                saved = {str(d.get("date")): d for d in (raw.get("days") or []) if d.get("date")}
            except Exception:
                saved = {}
        days: dict[str, dict[str, Any]] = {}
        for row in getattr(self, "_journal_all", list(self.journal)):
            self._apply_trade_to_day(days, row)
        for key, day in saved.items():
            if key not in days:
                days[key] = day
        self.days = days
        self.today_key = _local_date(_now_ms(), self.tz)
        if self.today_key not in self.days:
            self.days[self.today_key] = _empty_day(self.today_key)
        self._prune_days()
        self._save_days()

    def _apply_trade_to_day(self, days: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
        ts = int(row.get("ts") or 0)
        if ts <= 0:
            return
        key = _local_date(ts, self.tz)
        day = days.setdefault(key, _empty_day(key))
        pnl = float(row.get("pnl_usd") or 0)
        day["trades"] = int(day.get("trades") or 0) + 1
        day["pnl_usd"] = round(float(day.get("pnl_usd") or 0) + pnl, 4)
        if pnl > 0:
            day["plus"] = int(day.get("plus") or 0) + 1
        else:
            day["minus"] = int(day.get("minus") or 0) + 1
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            return
        by_map = day.setdefault("by_symbol", {})
        slot = by_map.setdefault(symbol, {"trades": 0, "plus": 0, "minus": 0, "pnl_usd": 0.0})
        slot["trades"] += 1
        slot["pnl_usd"] = round(float(slot.get("pnl_usd") or 0) + pnl, 4)
        if pnl > 0:
            slot["plus"] += 1
        else:
            slot["minus"] += 1

    def _prune_days(self) -> None:
        today = datetime.now(self.tz).date()
        keep = {(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(KEEP_DAYS)}
        self.days = {k: v for k, v in self.days.items() if k in keep}

    def _save_days(self) -> None:
        payload = {
            "updated": datetime.now(self.tz).strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": str(self.tz),
            "days": [self.days[k] for k in sorted(self.days)],
        }
        tmp = self.days_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.days_path)

    def roll_calendar_day(self) -> dict[str, Any] | None:
        """Если наступили новые сутки — зафиксировать вчерашний отчёт. Возвращает закрытый день."""
        now_key = _local_date(_now_ms(), self.tz)
        if now_key == self.today_key:
            return None
        closed = self.days.get(self.today_key) or _empty_day(self.today_key)
        self.today_key = now_key
        if now_key not in self.days:
            self.days[now_key] = _empty_day(now_key)
        self._prune_days()
        self._save_days()
        self.note(
            f"сутки {closed.get('date')}: сделок {closed.get('trades')} "
            f"(+{closed.get('plus')}/−{closed.get('minus')}) PnL {float(closed.get('pnl_usd') or 0):+.2f}$"
        )
        return closed

    def today_stats(self) -> dict[str, Any]:
        day = self.days.get(self.today_key) or _empty_day(self.today_key)
        return {
            "date": day["date"],
            "trades": int(day.get("trades") or 0),
            "plus": int(day.get("plus") or 0),
            "minus": int(day.get("minus") or 0),
            "pnl_usd": round(float(day.get("pnl_usd") or 0), 4),
        }

    def symbol_today(self, symbol: str) -> dict[str, int]:
        day = self.days.get(self.today_key) or {}
        slot = (day.get("by_symbol") or {}).get(symbol.upper()) or {}
        return {
            "plus": int(slot.get("plus") or 0),
            "minus": int(slot.get("minus") or 0),
            "trades": int(slot.get("trades") or 0),
            "pnl_usd": round(float(slot.get("pnl_usd") or 0), 4),
        }

    def reports(self) -> dict[str, Any]:
        today = datetime.now(self.tz).date()
        rows = []
        for i in range(KEEP_DAYS):
            key = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            day = self.days.get(key) or _empty_day(key)
            rows.append(
                {
                    "date": key,
                    "label": "сегодня" if i == 0 else key,
                    "trades": int(day.get("trades") or 0),
                    "plus": int(day.get("plus") or 0),
                    "minus": int(day.get("minus") or 0),
                    "pnl_usd": round(float(day.get("pnl_usd") or 0), 4),
                    "by_symbol": day.get("by_symbol") or {},
                }
            )
        return {"timezone": str(self.tz), "today": self.today_key, "days": rows}

    def _write_trade(self, row: dict[str, Any]) -> None:
        self.roll_calendar_day()
        self.journal.append(row)
        self._journal_all.append(row)
        self._apply_trade_to_day(self.days, row)
        self._save_days()
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
            buy_d, _buy_tp, sell_d, _sell_tp = _sides_from_pair(pair)
            if not symbol or (buy_d <= 0 and sell_d <= 0):
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
            buy_d, buy_tp, sell_d, sell_tp = _sides_from_pair(fresh)
            fp = f"{buy_d}|{buy_tp}|{sell_d}|{sell_tp}"
            if fp != algo.fingerprint:
                self.note(f"{symbol} новая рекомендация BUY {buy_d}/SHORT {sell_d} — перезапуск клона")
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
        buy_d, buy_tp, sell_d, sell_tp = _sides_from_pair(pair)
        lever = int(pair.get("lever") or self.leverage) or self.leverage
        now = _now_ms()
        dist = buy_d if buy_d > 0 else sell_d
        tp = buy_tp if buy_d > 0 else sell_tp
        algo = Algo(
            symbol=symbol,
            distance=dist,
            tp=tp,
            buy_distance=buy_d,
            sell_distance=sell_d,
            buy_tp=buy_tp,
            sell_tp=sell_tp,
            lever=lever,
            size_usdt=self.order_size,
            started_ts=now,
            until_ts=now + int(self.cfg.run_hours * 3600 * 1000),
            fingerprint=f"{buy_d}|{buy_tp}|{sell_d}|{sell_tp}",
        )
        self.algos[symbol] = algo
        sides = []
        if buy_d > 0:
            sides.append(f"BUY D{buy_d}% TP{buy_tp}%")
        if sell_d > 0:
            sides.append(f"SHORT D{sell_d}% TP{sell_tp}%")
        self.note(
            f"старт {symbol} {' · '.join(sides) or 'нет сторон'} x{lever} size={self.order_size:g} "
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
            buy = px * (1.0 - algo.buy_distance / 100.0) if algo.buy_distance > 0 else 0.0
            sell = px * (1.0 + algo.sell_distance / 100.0) if algo.sell_distance > 0 else 0.0
            moved = 0.0
            if buy > 0:
                moved = max(moved, abs(buy - algo.buy_px) / px * 100.0 if algo.buy_px else 99)
            if sell > 0:
                moved = max(moved, abs(sell - algo.sell_px) / px * 100.0 if algo.sell_px else 99)
            if moved < 0.01:
                continue
            algo.buy_px = buy
            algo.sell_px = sell
            if buy <= 0 and algo.buy_id and self.broker and not self.emulate:
                try:
                    await self.broker.cancel(algo.symbol, algo.buy_id)
                except Exception:
                    pass
                algo.buy_id = ""
            if sell <= 0 and algo.sell_id and self.broker and not self.emulate:
                try:
                    await self.broker.cancel(algo.symbol, algo.sell_id)
                except Exception:
                    pass
                algo.sell_id = ""
            if self.emulate or not (self.broker and self.broker.ready):
                continue
            try:
                await self.broker.load_spec(algo.symbol)
                algo.qty = self.broker.contracts_for(algo.symbol, algo.size_usdt, px)
                if buy > 0:
                    bpx = self.broker.round_px(algo.symbol, buy)
                    if algo.buy_id:
                        await self.broker.amend(algo.symbol, algo.buy_id, bpx)
                    else:
                        algo.buy_id = await self.broker.place_limit(algo.symbol, "buy", bpx, algo.qty)
                if sell > 0:
                    spx = self.broker.round_px(algo.symbol, sell)
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
        d = algo.buy_distance if side == "buy" else algo.sell_distance
        tp = algo.buy_tp if side == "buy" else algo.sell_tp
        algo.distance = d
        algo.tp = tp
        self.note(f"вход {algo.symbol} {side.upper()} @ {px:.6g} D{d}% size={algo.size_usdt:g}$")

    def _maybe_exit(self, algo: Algo, ts: int, price: float) -> None:
        if algo.state != "pos" or algo.entry <= 0:
            return
        favor = (price - algo.entry) / algo.entry * 100.0 if algo.pos_side == "buy" else (algo.entry - price) / algo.entry * 100.0
        tp = algo.buy_tp if algo.pos_side == "buy" else algo.sell_tp
        tp = max(tp, MIN_TP_PCT)
        hit_tp = tp > 0 and favor + 1e-12 >= tp
        timed = ts - algo.fill_ts >= self.cfg.hold_ms
        if not hit_tp and not timed:
            return
        exit_px = algo.entry * (1 + tp / 100.0) if hit_tp and algo.pos_side == "buy" else (
            algo.entry * (1 - tp / 100.0) if hit_tp else price
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
            "distance": algo.buy_distance if algo.pos_side == "buy" else algo.sell_distance,
            "tp": algo.buy_tp if algo.pos_side == "buy" else algo.sell_tp,
            "size_usdt": algo.size_usdt,
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

    def apply_runtime_settings(self) -> None:
        """Протянуть order size / leverage с панели на активные клоны."""
        for algo in self.algos.values():
            algo.size_usdt = self.order_size
            algo.lever = self.leverage
            if algo.state != "hunt":
                continue
            # Переставить лимиты с новым размером на следующем follow
            if not self.emulate and self.broker and self.broker.ready:
                try:
                    loop = asyncio.get_running_loop()
                    if algo.buy_id:
                        loop.create_task(self.broker.cancel(algo.symbol, algo.buy_id))
                    if algo.sell_id:
                        loop.create_task(self.broker.cancel(algo.symbol, algo.sell_id))
                except RuntimeError:
                    pass
            algo.buy_id = ""
            algo.sell_id = ""
            algo.buy_px = 0.0
            algo.sell_px = 0.0
            algo.qty = "1"

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
        self.roll_calendar_day()
        hour = self.window_stats(1)
        today = self.today_stats()
        day = today
        view = self.view_symbol or (next(iter(self.algos), "") or "")
        markets: list[dict[str, Any]] = []
        for a in self.algos.values():
            last = float(self.tapes[a.symbol].last or 0)
            buy_dist = ((last - a.buy_px) / last * 100.0) if last > 0 and a.buy_px > 0 else None
            sell_dist = ((a.sell_px - last) / last * 100.0) if last > 0 and a.sell_px > 0 else None
            u_pnl = 0.0
            in_trade = 0.0
            if a.state == "pos" and a.entry > 0:
                in_trade = a.size_usdt
                if last > 0:
                    u_pnl = _pnl_usd(a.pos_side, a.entry, last, a.size_usdt)
            sc = self.symbol_today(a.symbol)
            markets.append(
                {
                    "symbol": a.symbol,
                    "last": last,
                    "distance": a.distance,
                    "buy_distance": a.buy_distance,
                    "sell_distance": a.sell_distance,
                    "tp": a.tp,
                    "buy_tp": a.buy_tp,
                    "sell_tp": a.sell_tp,
                    "lever": a.lever,
                    "size_usdt": a.size_usdt,
                    "in_trade": round(in_trade, 4),
                    "wins": sc["plus"],
                    "losses": sc["minus"],
                    "day_pnl": sc["pnl_usd"],
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
        in_trade_total = round(sum(float(r.get("in_trade") or 0) for r in markets), 4)
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
            "in_trade": in_trade_total,
            "hour": hour,
            "day": day,
            "today": today,
            "reports": self.reports(),
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
