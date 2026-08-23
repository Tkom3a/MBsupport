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


def _pnl_pct(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0 or exit_px <= 0:
        return 0.0
    chg = (exit_px - entry) / entry if side == "buy" else (entry - exit_px) / entry
    return round(chg * 100.0, 4)


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
class Sides:
    buy_d: float = 0.0
    buy_tp: float = 0.0
    sell_d: float = 0.0
    sell_tp: float = 0.0
    buy_v2: float = 0.0
    buy_v2_tp: float = 0.0
    sell_v2: float = 0.0
    sell_v2_tp: float = 0.0

    def any(self) -> bool:
        return self.buy_d > 0 or self.sell_d > 0 or self.buy_v2 > 0 or self.sell_v2 > 0

    def fingerprint(self) -> str:
        return (
            f"{self.buy_d}|{self.buy_tp}|{self.sell_d}|{self.sell_tp}|"
            f"{self.buy_v2}|{self.sell_v2}"
        )


def _sides_from_pair(pair: dict[str, Any]) -> Sides:
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
    buy_v2 = round(float(pair.get("buy_v2_pct") or 0), 2)
    sell_v2 = round(float(pair.get("sell_v2_pct") or 0), 2)
    buy_v2_tp = round(float(pair.get("buy_v2_tp_pct") or buy_tp or 0), 2)
    sell_v2_tp = round(float(pair.get("sell_v2_tp_pct") or sell_tp or 0), 2)
    return Sides(buy_d, buy_tp, sell_d, sell_tp, buy_v2, buy_v2_tp, sell_v2, sell_v2_tp)


@dataclass
class Algo:
    symbol: str
    distance: float
    tp: float
    buy_distance: float = 0.0
    sell_distance: float = 0.0
    buy_tp: float = 0.0
    sell_tp: float = 0.0
    buy_v2_distance: float = 0.0
    sell_v2_distance: float = 0.0
    buy_v2_tp: float = 0.0
    sell_v2_tp: float = 0.0
    lever: int = 50
    size_usdt: float = 10.0
    started_ts: int = 0
    until_ts: int = 0
    buy_px: float = 0.0
    sell_px: float = 0.0
    buy_id: str = ""
    sell_id: str = ""
    buy_v2_px: float = 0.0
    sell_v2_px: float = 0.0
    buy_v2_id: str = ""
    sell_v2_id: str = ""
    state: str = "hunt"
    pos_side: str = ""
    entry: float = 0.0
    fill_ts: int = 0
    v2_state: str = "off"
    v2_pos_side: str = ""
    v2_entry: float = 0.0
    v2_fill_ts: int = 0
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
        self.journal: deque[dict[str, Any]] = deque(maxlen=2000)
        self._journal_all: list[dict[str, Any]] = []
        self.days: dict[str, dict[str, Any]] = {}
        self.today_key = _local_date(_now_ms(), self.tz)
        self.emulate = cfg.emulate or not (broker and broker.ready and cfg.live_trading)
        self.order_size_x20 = cfg.order_size_x20
        self.order_size_x50 = cfg.order_size_x50
        self.order_size = cfg.order_size_x50
        self.leverage = cfg.leverage
        self.autostop_usd = cfg.autostop_usd
        self.min_order_distance = cfg.min_order_distance
        self.min_v2_gap = cfg.min_v2_gap
        self.halted = False
        self.halt_reason = ""
        self.trade_long = True
        self.trade_short = True
        self.view_symbol = ""
        self.plan_pairs: list[dict[str, Any]] = []
        self.plan_error = ""
        self.plan_updated_ts = 0
        self.runtime_path = self.data_dir / "trader_runtime.json"
        self._load_journal()
        self._rebuild_days()
        self._load_runtime()

    def _load_runtime(self) -> None:
        if not self.runtime_path.is_file():
            self.trade_long = bool(self.cfg.trade_long)
            self.trade_short = bool(self.cfg.trade_short)
            return
        try:
            raw = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except Exception:
            self.trade_long = bool(self.cfg.trade_long)
            self.trade_short = bool(self.cfg.trade_short)
            return
        if "trade_long" in raw:
            self.trade_long = bool(raw["trade_long"])
        else:
            self.trade_long = bool(self.cfg.trade_long)
        if "trade_short" in raw:
            self.trade_short = bool(raw["trade_short"])
        else:
            self.trade_short = bool(self.cfg.trade_short)
        if raw.get("order_size_x20"):
            self.order_size_x20 = max(1.0, float(raw["order_size_x20"]))
        if raw.get("order_size_x50"):
            self.order_size_x50 = max(1.0, float(raw["order_size_x50"]))
            self.order_size = self.order_size_x50
        if raw.get("autostop_usd"):
            self.autostop_usd = max(0.5, float(raw["autostop_usd"]))
        if raw.get("min_order_distance") not in (None, ""):
            self.min_order_distance = max(0.0, float(raw["min_order_distance"]))
        if raw.get("min_v2_gap") not in (None, ""):
            self.min_v2_gap = max(0.05, float(raw["min_v2_gap"]))

    def save_runtime(self) -> None:
        payload = {
            "trade_long": self.trade_long,
            "trade_short": self.trade_short,
            "order_size_x20": self.order_size_x20,
            "order_size_x50": self.order_size_x50,
            "autostop_usd": self.autostop_usd,
            "min_order_distance": self.min_order_distance,
            "min_v2_gap": self.min_v2_gap,
        }
        tmp = self.runtime_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.runtime_path)

    def _filter_sides(self, sides: Sides) -> Sides:
        if not self.trade_long:
            sides.buy_d = sides.buy_tp = sides.buy_v2 = sides.buy_v2_tp = 0.0
        if not self.trade_short:
            sides.sell_d = sides.sell_tp = sides.sell_v2 = sides.sell_v2_tp = 0.0
        return sides

    def _plan_sides(self, pair: dict[str, Any]) -> Sides:
        return self._apply_distance_rules(self._filter_sides(_sides_from_pair(pair)))

    def _apply_distance_rules(self, sides: Sides) -> Sides:
        """V2 не ближе min_v2_gap к первому; ордера короче min_order_distance не ставим."""
        gap = max(0.05, round(float(self.min_v2_gap or 0.3), 2))
        floor = max(0.0, round(float(self.min_order_distance or 0), 2))

        def bump_v2(d: float, v2: float, tp: float, v2_tp: float) -> tuple[float, float]:
            if d <= 0:
                return 0.0, 0.0
            want = round(d + gap, 2)
            if v2 <= 0 or v2 + 1e-9 < want:
                v2 = want
            if v2_tp + 1e-9 < MIN_TP_PCT:
                v2_tp = max(tp, MIN_TP_PCT)
            return v2, v2_tp

        if sides.buy_d > 0:
            sides.buy_v2, sides.buy_v2_tp = bump_v2(sides.buy_d, sides.buy_v2, sides.buy_tp, sides.buy_v2_tp)
        if sides.sell_d > 0:
            sides.sell_v2, sides.sell_v2_tp = bump_v2(sides.sell_d, sides.sell_v2, sides.sell_tp, sides.sell_v2_tp)

        def keep(d: float) -> bool:
            return d > 0 and d + 1e-9 >= floor

        if not keep(sides.buy_d):
            sides.buy_d = sides.buy_tp = 0.0
        if not keep(sides.sell_d):
            sides.sell_d = sides.sell_tp = 0.0
        if not keep(sides.buy_v2):
            sides.buy_v2 = sides.buy_v2_tp = 0.0
        if not keep(sides.sell_v2):
            sides.sell_v2 = sides.sell_v2_tp = 0.0
        return sides

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
            for line in lines[-2000:]:
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
            sides = self._plan_sides(pair)
            if not symbol or not sides.any():
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
            sides = self._plan_sides(fresh)
            fp = sides.fingerprint()
            if fp != algo.fingerprint:
                self.note(
                    f"{symbol} новая рекомендация BUY {sides.buy_d}/SHORT {sides.sell_d}"
                    f" V2 {sides.buy_v2}/{sides.sell_v2} — перезапуск клона"
                )
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

    def size_for_lever(self, lever: int | float) -> float:
        """Номинал с плечом: x20 и ближе → size x20, иначе size x50."""
        lv = int(round(float(lever or 0))) or int(self.leverage or 50)
        if abs(lv - 20) <= abs(lv - 50):
            return float(self.order_size_x20)
        return float(self.order_size_x50)

    def _start(self, pair: dict[str, Any]) -> None:
        symbol = str(pair.get("symbol") or "").upper()
        sides = self._plan_sides(pair)
        if not sides.any():
            return
        lever = int(pair.get("lever") or self.leverage) or self.leverage
        size = self.size_for_lever(lever)
        now = _now_ms()
        dist = sides.buy_d if sides.buy_d > 0 else sides.sell_d
        tp = sides.buy_tp if sides.buy_d > 0 else sides.sell_tp
        has_v2 = sides.buy_v2 > 0 or sides.sell_v2 > 0
        algo = Algo(
            symbol=symbol,
            distance=dist,
            tp=tp,
            buy_distance=sides.buy_d,
            sell_distance=sides.sell_d,
            buy_tp=sides.buy_tp,
            sell_tp=sides.sell_tp,
            buy_v2_distance=sides.buy_v2,
            sell_v2_distance=sides.sell_v2,
            buy_v2_tp=sides.buy_v2_tp,
            sell_v2_tp=sides.sell_v2_tp,
            lever=lever,
            size_usdt=size,
            started_ts=now,
            until_ts=now + int(self.cfg.run_hours * 3600 * 1000),
            v2_state="hunt" if has_v2 else "off",
            fingerprint=sides.fingerprint(),
        )
        self.algos[symbol] = algo
        labels = []
        if sides.buy_d > 0:
            labels.append(f"BUY D{sides.buy_d}% TP{sides.buy_tp}%")
        if sides.sell_d > 0:
            labels.append(f"SHORT D{sides.sell_d}% TP{sides.sell_tp}%")
        if sides.buy_v2 > 0:
            labels.append(f"BUY V2 D{sides.buy_v2}% (+{sides.buy_v2 - sides.buy_d:g})")
        if sides.sell_v2 > 0:
            labels.append(f"SHORT V2 D{sides.sell_v2}% (+{sides.sell_v2 - sides.sell_d:g})")
        self.note(
            f"старт {symbol} {' · '.join(labels) or 'нет сторон'} x{lever} size={size:g} "
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
        for oid in (algo.buy_id, algo.sell_id, algo.buy_v2_id, algo.sell_v2_id):
            if oid:
                loop.create_task(self.broker.cancel(symbol, oid))
        if algo.state == "pos" and algo.entry > 0:
            close_side = "sell" if algo.pos_side == "buy" else "buy"
            loop.create_task(self.broker.close_market(symbol, close_side, algo.qty))
        if algo.v2_state == "pos" and algo.v2_entry > 0:
            close_side = "sell" if algo.v2_pos_side == "buy" else "buy"
            loop.create_task(self.broker.close_market(symbol, close_side, algo.qty))

    async def follow_once(self) -> None:
        if self.halted:
            return
        now = _now_ms()
        for algo in list(self.algos.values()):
            if now >= algo.until_ts:
                self._kill(algo.symbol, "expired")
                continue
            hunting = algo.state == "hunt" or algo.v2_state == "hunt"
            if not hunting:
                continue
            px = self.tapes[algo.symbol].delayed(self.cfg.follow_delay_ms, now)
            if px <= 0:
                continue
            buy = px * (1.0 - algo.buy_distance / 100.0) if algo.state == "hunt" and algo.buy_distance > 0 else 0.0
            sell = px * (1.0 + algo.sell_distance / 100.0) if algo.state == "hunt" and algo.sell_distance > 0 else 0.0
            buy_v2 = px * (1.0 - algo.buy_v2_distance / 100.0) if algo.v2_state == "hunt" and algo.buy_v2_distance > 0 else 0.0
            sell_v2 = px * (1.0 + algo.sell_v2_distance / 100.0) if algo.v2_state == "hunt" and algo.sell_v2_distance > 0 else 0.0
            moved = 0.0
            for new_px, old_px in (
                (buy, algo.buy_px),
                (sell, algo.sell_px),
                (buy_v2, algo.buy_v2_px),
                (sell_v2, algo.sell_v2_px),
            ):
                if new_px > 0:
                    moved = max(moved, abs(new_px - old_px) / px * 100.0 if old_px else 99)
            if moved < 0.01:
                continue
            algo.buy_px = buy
            algo.sell_px = sell
            algo.buy_v2_px = buy_v2
            algo.sell_v2_px = sell_v2
            await self._sync_limit(algo, "buy_id", buy, "buy")
            await self._sync_limit(algo, "sell_id", sell, "sell")
            await self._sync_limit(algo, "buy_v2_id", buy_v2, "buy")
            await self._sync_limit(algo, "sell_v2_id", sell_v2, "sell")
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
                if buy_v2 > 0:
                    bpx = self.broker.round_px(algo.symbol, buy_v2)
                    if algo.buy_v2_id:
                        await self.broker.amend(algo.symbol, algo.buy_v2_id, bpx)
                    else:
                        algo.buy_v2_id = await self.broker.place_limit(algo.symbol, "buy", bpx, algo.qty)
                if sell_v2 > 0:
                    spx = self.broker.round_px(algo.symbol, sell_v2)
                    if algo.sell_v2_id:
                        await self.broker.amend(algo.symbol, algo.sell_v2_id, spx)
                    else:
                        algo.sell_v2_id = await self.broker.place_limit(algo.symbol, "sell", spx, algo.qty)
            except Exception as exc:
                self.note(f"{algo.symbol} follow: {exc}")

    async def _sync_limit(self, algo: Algo, attr: str, px: float, _side: str) -> None:
        oid = getattr(algo, attr)
        if px <= 0 and oid and self.broker and not self.emulate:
            try:
                await self.broker.cancel(algo.symbol, oid)
            except Exception:
                pass
            setattr(algo, attr, "")

    def _check_fill(self, algo: Algo, ts: int, price: float) -> None:
        if price <= 0:
            return
        if algo.state == "hunt":
            if algo.buy_px > 0 and price <= algo.buy_px:
                self._enter(algo, "buy", algo.buy_px, ts, "v1")
            elif algo.sell_px > 0 and price >= algo.sell_px:
                self._enter(algo, "sell", algo.sell_px, ts, "v1")
        if algo.v2_state == "hunt":
            if algo.buy_v2_px > 0 and price <= algo.buy_v2_px:
                self._enter(algo, "buy", algo.buy_v2_px, ts, "v2")
            elif algo.sell_v2_px > 0 and price >= algo.sell_v2_px:
                self._enter(algo, "sell", algo.sell_v2_px, ts, "v2")

    def _enter(self, algo: Algo, side: str, px: float, ts: int, layer: str = "v1") -> None:
        if layer == "v2":
            algo.v2_state = "pos"
            algo.v2_pos_side = side
            algo.v2_entry = px
            algo.v2_fill_ts = ts
            other = algo.sell_v2_id if side == "buy" else algo.buy_v2_id
            if not self.emulate and self.broker and other:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.broker.cancel(algo.symbol, other))
                except RuntimeError:
                    pass
            algo.buy_v2_id = ""
            algo.sell_v2_id = ""
            d = algo.buy_v2_distance if side == "buy" else algo.sell_v2_distance
            self.note(f"вход V2 {algo.symbol} {side.upper()} @ {px:.6g} D{d}% size={algo.size_usdt:g}$")
            return
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
        if algo.state == "pos" and algo.entry > 0:
            self._maybe_exit_layer(algo, ts, price, "v1")
        if algo.v2_state == "pos" and algo.v2_entry > 0:
            self._maybe_exit_layer(algo, ts, price, "v2")

    def _maybe_exit_layer(self, algo: Algo, ts: int, price: float, layer: str) -> None:
        if layer == "v2":
            side, entry, fill_ts = algo.v2_pos_side, algo.v2_entry, algo.v2_fill_ts
            tp = algo.buy_v2_tp if side == "buy" else algo.sell_v2_tp
        else:
            side, entry, fill_ts = algo.pos_side, algo.entry, algo.fill_ts
            tp = algo.buy_tp if side == "buy" else algo.sell_tp
        if entry <= 0:
            return
        favor = (price - entry) / entry * 100.0 if side == "buy" else (entry - price) / entry * 100.0
        tp = max(tp, MIN_TP_PCT)
        hit_tp = tp > 0 and favor + 1e-12 >= tp
        timed = ts - fill_ts >= self.cfg.hold_ms
        if not hit_tp and not timed:
            return
        exit_px = entry * (1 + tp / 100.0) if hit_tp and side == "buy" else (
            entry * (1 - tp / 100.0) if hit_tp else price
        )
        tag = "TP" if hit_tp else "0.3с"
        if layer == "v2":
            tag = f"V2 {tag}"
        self._close(algo, exit_px, tag, layer)

    def _close(self, algo: Algo, exit_px: float, why: str, layer: str = "v1") -> None:
        if layer == "v2":
            side, entry = algo.v2_pos_side, algo.v2_entry
            distance = algo.buy_v2_distance if side == "buy" else algo.sell_v2_distance
            tp = algo.buy_v2_tp if side == "buy" else algo.sell_v2_tp
        else:
            side, entry = algo.pos_side, algo.entry
            distance = algo.buy_distance if side == "buy" else algo.sell_distance
            tp = algo.buy_tp if side == "buy" else algo.sell_tp
        pnl = _pnl_usd(side, entry, exit_px, algo.size_usdt)
        pct = _pnl_pct(side, entry, exit_px)
        row = {
            "ts": _now_ms(),
            "symbol": algo.symbol,
            "side": side,
            "entry": entry,
            "exit": exit_px,
            "pnl_usd": pnl,
            "pnl_pct": pct,
            "why": why,
            "layer": layer,
            "distance": distance,
            "tp": tp,
            "size_usdt": algo.size_usdt,
            "lever": int(algo.lever or 0),
            "emulate": self.emulate,
        }
        self._write_trade(row)
        if not self.emulate and self.broker and self.broker.ready:
            try:
                loop = asyncio.get_running_loop()
                close_side = "sell" if side == "buy" else "buy"
                loop.create_task(self.broker.close_market(algo.symbol, close_side, algo.qty))
            except RuntimeError:
                pass
        tag = "V2 " if layer == "v2" else ""
        self.note(f"выход {tag}{algo.symbol} {why} pnl {pnl:+.2f}$")
        if layer == "v2":
            algo.v2_state = "hunt" if (algo.buy_v2_distance > 0 or algo.sell_v2_distance > 0) else "off"
            algo.v2_pos_side = ""
            algo.v2_entry = 0.0
            algo.v2_fill_ts = 0
            algo.buy_v2_px = 0.0
            algo.sell_v2_px = 0.0
        else:
            algo.state = "hunt"
            algo.pos_side = ""
            algo.entry = 0.0
            algo.fill_ts = 0
            algo.buy_px = 0.0
            algo.sell_px = 0.0
        if pnl <= -self.autostop_usd:
            self.emergency(f"сделка {algo.symbol} {pnl:.2f}$ ≤ -{self.autostop_usd:g}$")

    def _check_open_loss(self, algo: Algo, price: float) -> None:
        if algo.state == "pos" and algo.entry > 0:
            pnl = _pnl_usd(algo.pos_side, algo.entry, price, algo.size_usdt)
            if pnl <= -self.autostop_usd:
                self._close(algo, price, "autostop", "v1")
                self.emergency(f"открытый минус {algo.symbol} {pnl:.2f}$")
                return
        if algo.v2_state == "pos" and algo.v2_entry > 0:
            pnl = _pnl_usd(algo.v2_pos_side, algo.v2_entry, price, algo.size_usdt)
            if pnl <= -self.autostop_usd:
                self._close(algo, price, "autostop", "v2")
                self.emergency(f"открытый минус V2 {algo.symbol} {pnl:.2f}$")

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
        """Протянуть размеры x20/x50 и направления на активные клоны."""
        for algo in list(self.algos.values()):
            if algo.state == "hunt":
                algo.size_usdt = self.size_for_lever(algo.lever)
            self._apply_directions_to_algo(algo)
            if not self.emulate and self.broker and self.broker.ready:
                try:
                    loop = asyncio.get_running_loop()
                    for attr in ("buy_id", "sell_id", "buy_v2_id", "sell_v2_id"):
                        oid = getattr(algo, attr)
                        if oid:
                            loop.create_task(self.broker.cancel(algo.symbol, oid))
                except RuntimeError:
                    pass
            algo.buy_id = algo.sell_id = algo.buy_v2_id = algo.sell_v2_id = ""
            if algo.state == "hunt":
                algo.buy_px = 0.0
                algo.sell_px = 0.0
            if algo.v2_state == "hunt":
                algo.buy_v2_px = 0.0
                algo.sell_v2_px = 0.0
            algo.qty = "1"
            if (
                algo.state != "pos"
                and algo.v2_state != "pos"
                and algo.buy_distance <= 0
                and algo.sell_distance <= 0
                and algo.buy_v2_distance <= 0
                and algo.sell_v2_distance <= 0
            ):
                self._kill(algo.symbol, "direction-off")
        self.save_runtime()
        if self.plan_pairs:
            self.sync_plan(self.plan_pairs)

    def _apply_directions_to_algo(self, algo: Algo) -> None:
        if not self.trade_long:
            if algo.state == "hunt":
                algo.buy_distance = 0.0
                algo.buy_tp = 0.0
                algo.buy_px = 0.0
            if algo.v2_state == "hunt":
                algo.buy_v2_distance = 0.0
                algo.buy_v2_tp = 0.0
                algo.buy_v2_px = 0.0
        if not self.trade_short:
            if algo.state == "hunt":
                algo.sell_distance = 0.0
                algo.sell_tp = 0.0
                algo.sell_px = 0.0
            if algo.v2_state == "hunt":
                algo.sell_v2_distance = 0.0
                algo.sell_v2_tp = 0.0
                algo.sell_v2_px = 0.0
        if algo.v2_state != "pos" and algo.buy_v2_distance <= 0 and algo.sell_v2_distance <= 0:
            algo.v2_state = "off"
        elif algo.v2_state == "off" and (algo.buy_v2_distance > 0 or algo.sell_v2_distance > 0):
            algo.v2_state = "hunt"

    def day_trades(self) -> list[dict[str, Any]]:
        """Сделки текущих суток (по TZ) — для окна журнала."""
        self.roll_calendar_day()
        key = self.today_key
        rows: list[dict[str, Any]] = []
        for row in getattr(self, "_journal_all", list(self.journal)):
            ts = int(row.get("ts") or 0)
            if ts <= 0:
                continue
            if _local_date(ts, self.tz) != key:
                continue
            item = dict(row)
            if not int(item.get("lever") or 0):
                algo = self.algos.get(str(item.get("symbol") or "").upper())
                if algo and algo.lever:
                    item["lever"] = int(algo.lever)
            if item.get("pnl_pct") in (None, ""):
                item["pnl_pct"] = _pnl_pct(
                    str(item.get("side") or ""),
                    float(item.get("entry") or 0),
                    float(item.get("exit") or 0),
                )
            rows.append(item)
        return rows

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
            last = self.tapes[algo.symbol].last
            if last <= 0:
                continue
            if algo.state == "pos" and algo.entry > 0:
                total += _pnl_usd(algo.pos_side, algo.entry, last, algo.size_usdt)
            if algo.v2_state == "pos" and algo.v2_entry > 0:
                total += _pnl_usd(algo.v2_pos_side, algo.v2_entry, last, algo.size_usdt)
        return round(total, 4)

    @staticmethod
    def _side_margin(algo: Algo) -> float:
        lever = max(int(algo.lever or 1), 1)
        return float(algo.size_usdt or 0) / lever

    def _frozen_budget(self, algos: list[Algo] | None = None) -> dict[str, Any]:
        """Сколько USDT без плеча нужно на бирже под все текущие ордера/позиции."""
        pairs = 0
        sides = 0
        margin = 0.0
        notional = 0.0
        for algo in algos if algos is not None else self.algos.values():
            one = self._side_margin(algo)
            n = 0
            if algo.state == "pos":
                n += 1
            else:
                n += int(algo.buy_distance > 0) + int(algo.sell_distance > 0)
            if algo.v2_state == "pos":
                n += 1
            elif algo.v2_state == "hunt":
                n += int(algo.buy_v2_distance > 0) + int(algo.sell_v2_distance > 0)
            if n <= 0:
                continue
            pairs += 1
            sides += n
            margin += one * n
            notional += float(algo.size_usdt or 0) * n
        return {
            "pairs": pairs,
            "sides": sides,
            "margin_usdt": round(margin, 4),
            "notional_usdt": round(notional, 4),
        }

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
            buy_v2_dist = ((last - a.buy_v2_px) / last * 100.0) if last > 0 and a.buy_v2_px > 0 else None
            sell_v2_dist = ((a.sell_v2_px - last) / last * 100.0) if last > 0 and a.sell_v2_px > 0 else None
            u_pnl = 0.0
            u_pnl_v2 = 0.0
            in_trade = 0.0
            if a.state == "pos" and a.entry > 0:
                in_trade += a.size_usdt
                if last > 0:
                    u_pnl = _pnl_usd(a.pos_side, a.entry, last, a.size_usdt)
            if a.v2_state == "pos" and a.v2_entry > 0:
                in_trade += a.size_usdt
                if last > 0:
                    u_pnl_v2 = _pnl_usd(a.v2_pos_side, a.v2_entry, last, a.size_usdt)
            sc = self.symbol_today(a.symbol)
            margin = self._side_margin(a)
            frozen_sides = 0
            if a.state == "pos":
                frozen_sides += 1
            else:
                frozen_sides += int(a.buy_distance > 0) + int(a.sell_distance > 0)
            if a.v2_state == "pos":
                frozen_sides += 1
            elif a.v2_state == "hunt":
                frozen_sides += int(a.buy_v2_distance > 0) + int(a.sell_v2_distance > 0)
            markets.append(
                {
                    "symbol": a.symbol,
                    "last": last,
                    "distance": a.distance,
                    "buy_distance": a.buy_distance,
                    "sell_distance": a.sell_distance,
                    "buy_v2_distance": a.buy_v2_distance,
                    "sell_v2_distance": a.sell_v2_distance,
                    "tp": a.tp,
                    "buy_tp": a.buy_tp,
                    "sell_tp": a.sell_tp,
                    "buy_v2_tp": a.buy_v2_tp,
                    "sell_v2_tp": a.sell_v2_tp,
                    "lever": a.lever,
                    "size_usdt": a.size_usdt,
                    "margin_usdt": round(margin, 4),
                    "frozen_sides": frozen_sides,
                    "frozen_margin_usdt": round(margin * frozen_sides, 4),
                    "in_trade": round(in_trade, 4),
                    "wins": sc["plus"],
                    "losses": sc["minus"],
                    "day_pnl": sc["pnl_usd"],
                    "state": a.state,
                    "side": a.pos_side,
                    "entry": a.entry,
                    "v2_state": a.v2_state,
                    "v2_side": a.v2_pos_side,
                    "v2_entry": a.v2_entry,
                    "buy": a.buy_px,
                    "sell": a.sell_px,
                    "buy_v2": a.buy_v2_px,
                    "sell_v2": a.sell_v2_px,
                    "buy_dist_pct": None if buy_dist is None else round(buy_dist, 4),
                    "sell_dist_pct": None if sell_dist is None else round(sell_dist, 4),
                    "buy_v2_dist_pct": None if buy_v2_dist is None else round(buy_v2_dist, 4),
                    "sell_v2_dist_pct": None if sell_v2_dist is None else round(sell_v2_dist, 4),
                    "unrealized": round(u_pnl, 4),
                    "unrealized_v2": round(u_pnl_v2, 4),
                    "left_min": max(0, int((a.until_ts - _now_ms()) / 60000)),
                }
            )
        markets.sort(key=lambda row: row["symbol"])
        in_trade_total = round(sum(float(r.get("in_trade") or 0) for r in markets), 4)
        budget = self._frozen_budget(list(self.algos.values()))
        return {
            "emulate": self.emulate,
            "live": (not self.emulate) and bool(self.broker and self.broker.ready),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "order_size": self.order_size_x50,
            "order_size_x20": self.order_size_x20,
            "order_size_x50": self.order_size_x50,
            "leverage": self.leverage,
            "autostop_usd": self.autostop_usd,
            "trade_long": self.trade_long,
            "trade_short": self.trade_short,
            "min_order_distance": self.min_order_distance,
            "min_v2_gap": self.min_v2_gap,
            "follow_delay_ms": self.cfg.follow_delay_ms,
            "hold_ms": self.cfg.hold_ms,
            "run_hours": self.cfg.run_hours,
            "unrealized": self.unrealized(),
            "in_trade": in_trade_total,
            "frozen_margin_usdt": budget["margin_usdt"],
            "frozen_notional_usdt": budget["notional_usdt"],
            "frozen_pairs": budget["pairs"],
            "frozen_sides": budget["sides"],
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
            "journal": self.day_trades(),
            "log": list(self.log_lines)[-60:],
        }
