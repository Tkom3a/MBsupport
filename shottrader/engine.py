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
DEPTH_RECOVERY_INSIDE = 0.05  # D_new = D + |ход%| − 0.05
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


def _fee_round_trip_pct(why: str, maker_pct: float, taker_pct: float) -> float:
    maker = max(0.0, float(maker_pct or 0))
    taker = max(0.0, float(taker_pct or 0))
    if "TP" in str(why or "").upper():
        return round(maker + maker, 4)
    return round(maker + taker, 4)


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
    last_v1_pnl: float | None = None
    last_v2_pnl: float | None = None
    last_v1_ts: int = 0
    last_v2_ts: int = 0
    v1_rescue_key: str = ""
    depth_bump_key: str = ""


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
        self.v1_offset = cfg.v1_offset
        self.tp_offset = cfg.tp_offset
        self.stop_loss_long_pct = cfg.stop_loss_long_pct
        self.stop_loss_short_pct = cfg.stop_loss_short_pct
        self.stop_loss_v2_long_pct = cfg.stop_loss_v2_long_pct
        self.stop_loss_v2_short_pct = cfg.stop_loss_v2_short_pct
        self.v1_fail_bump = cfg.v1_fail_bump
        self.v1_fail_bumps: dict[str, float] = {}
        self.pair_lose_limit = cfg.pair_lose_limit
        self.pair_lose_window_hours = cfg.pair_lose_window_hours
        self.pair_ban_hours = cfg.pair_ban_hours
        self.min_fills = 5 if int(cfg.min_fills) == 2 else cfg.min_fills
        self.max_rec_age_min = cfg.max_rec_age_min
        self.fee_maker_pct = cfg.fee_maker_pct
        self.fee_taker_pct = cfg.fee_taker_pct
        self.bans: dict[str, int] = {}
        self.ban_until_hist: dict[str, int] = {}
        self.ban_d_lock: dict[str, dict[str, float]] = {}
        self._d_lock_noted: set[str] = set()
        self.bans_path = self.data_dir / "pair_bans.json"
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
        self._load_bans()

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
        if raw.get("v1_offset") not in (None, ""):
            self.v1_offset = max(-2.0, min(5.0, float(raw["v1_offset"])))
        if raw.get("tp_offset") not in (None, ""):
            self.tp_offset = max(0.0, min(2.0, float(raw["tp_offset"])))
        legacy_sl = None
        if raw.get("stop_loss_pct") not in (None, ""):
            legacy_sl = max(0.0, min(5.0, float(raw["stop_loss_pct"])))
        for key in (
            "stop_loss_long_pct",
            "stop_loss_short_pct",
            "stop_loss_v2_long_pct",
            "stop_loss_v2_short_pct",
        ):
            if raw.get(key) not in (None, ""):
                setattr(self, key, max(0.0, min(5.0, float(raw[key]))))
            elif legacy_sl is not None:
                setattr(self, key, legacy_sl)
        if raw.get("v1_fail_bump") not in (None, ""):
            self.v1_fail_bump = max(0.0, min(2.0, float(raw["v1_fail_bump"])))
        if raw.get("pair_lose_limit") not in (None, ""):
            val = max(1, int(float(raw["pair_lose_limit"])))
            # старый дефолт был 2; правило сменилось на 3 минуса подряд
            self.pair_lose_limit = 3 if val == 2 else val
        if raw.get("pair_lose_window_hours") not in (None, ""):
            self.pair_lose_window_hours = max(0.1, float(raw["pair_lose_window_hours"]))
        if raw.get("pair_ban_hours") not in (None, ""):
            self.pair_ban_hours = max(0.1, float(raw["pair_ban_hours"]))
        if raw.get("min_fills") not in (None, ""):
            val = max(1, int(float(raw["min_fills"])))
            # старый дефолт был 2; для статистики нужно больше подтверждений
            self.min_fills = 5 if val == 2 else val
        if raw.get("max_rec_age_min") not in (None, ""):
            self.max_rec_age_min = max(0, int(float(raw["max_rec_age_min"])))
        if raw.get("fee_maker_pct") not in (None, ""):
            self.fee_maker_pct = max(0.0, min(0.5, float(raw["fee_maker_pct"])))
        if raw.get("fee_taker_pct") not in (None, ""):
            self.fee_taker_pct = max(0.0, min(0.5, float(raw["fee_taker_pct"])))

    def save_runtime(self) -> None:
        payload = {
            "trade_long": self.trade_long,
            "trade_short": self.trade_short,
            "order_size_x20": self.order_size_x20,
            "order_size_x50": self.order_size_x50,
            "autostop_usd": self.autostop_usd,
            "min_order_distance": self.min_order_distance,
            "min_v2_gap": self.min_v2_gap,
            "v1_offset": self.v1_offset,
            "tp_offset": self.tp_offset,
            "stop_loss_long_pct": self.stop_loss_long_pct,
            "stop_loss_short_pct": self.stop_loss_short_pct,
            "stop_loss_v2_long_pct": self.stop_loss_v2_long_pct,
            "stop_loss_v2_short_pct": self.stop_loss_v2_short_pct,
            "v1_fail_bump": self.v1_fail_bump,
            "pair_lose_limit": self.pair_lose_limit,
            "pair_lose_window_hours": self.pair_lose_window_hours,
            "pair_ban_hours": self.pair_ban_hours,
            "min_fills": self.min_fills,
            "max_rec_age_min": self.max_rec_age_min,
            "fee_maker_pct": self.fee_maker_pct,
            "fee_taker_pct": self.fee_taker_pct,
        }
        tmp = self.runtime_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.runtime_path)

    def _load_bans(self) -> None:
        if not self.bans_path.is_file():
            return
        try:
            raw = json.loads(self.bans_path.read_text(encoding="utf-8"))
        except Exception:
            return
        now = _now_ms()
        for key, val in (raw.get("bans") or {}).items():
            until = int(val or 0)
            if until > now:
                self.bans[str(key).upper()] = until
            if until > 0:
                self.ban_until_hist[str(key).upper()] = until
        for key, val in (raw.get("hist") or {}).items():
            self.ban_until_hist[str(key).upper()] = max(int(val or 0), self.ban_until_hist.get(str(key).upper(), 0))
        for key, val in (raw.get("v1_fail_bumps") or {}).items():
            extra = round(float(val or 0), 2)
            if extra > 0:
                self.v1_fail_bumps[str(key).upper()] = extra
        for key, val in (raw.get("ban_d_lock") or {}).items():
            if not isinstance(val, dict):
                continue
            self.ban_d_lock[str(key).upper()] = {
                "buy": round(float(val.get("buy") or 0), 2),
                "sell": round(float(val.get("sell") or 0), 2),
            }

    def _save_bans(self) -> None:
        payload = {
            "bans": self.bans,
            "hist": self.ban_until_hist,
            "v1_fail_bumps": self.v1_fail_bumps,
            "ban_d_lock": self.ban_d_lock,
        }
        tmp = self.bans_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.bans_path)

    def _prune_bans(self) -> list[str]:
        now = _now_ms()
        expired = [sym for sym, until in self.bans.items() if now >= until]
        for sym in expired:
            self.ban_until_hist[sym] = max(self.ban_until_hist.get(sym, 0), self.bans.pop(sym))
            self.note(f"{sym} бан истёк — снова отслеживаем")
        if expired:
            self._save_bans()
        return expired

    def is_banned(self, symbol: str) -> bool:
        until = self.bans.get(str(symbol or "").upper(), 0)
        return until > _now_ms()

    def public_bans(self) -> list[dict[str, Any]]:
        now = _now_ms()
        rows = []
        for symbol, until in sorted(self.bans.items()):
            if until <= now:
                continue
            left = max(0, int((until - now) / 60000))
            rows.append({"symbol": symbol, "until_ts": until, "left_min": left})
        return rows

    def _pair_trades_since_ban(self, symbol: str) -> list[dict[str, Any]]:
        last_ban_end = int(self.ban_until_hist.get(symbol, 0) or 0)
        rows: list[dict[str, Any]] = []
        for row in getattr(self, "_journal_all", list(self.journal)):
            if str(row.get("symbol") or "").upper() != symbol:
                continue
            if last_ban_end and int(row.get("ts") or 0) < last_ban_end:
                continue
            rows.append(row)
        rows.sort(key=lambda item: int(item.get("ts") or 0))
        return rows

    def _loss_streak(self, symbol: str) -> int:
        """Сколько минусов подряд с конца журнала пары (после прошлого бана)."""
        streak = 0
        for row in reversed(self._pair_trades_since_ban(symbol)):
            if float(row.get("pnl_usd") or 0) <= 0:
                streak += 1
            else:
                break
        return streak

    def _plan_raw_d(self, symbol: str) -> tuple[float, float]:
        for pair in self.plan_pairs:
            if str(pair.get("symbol") or "").upper() == symbol:
                return (
                    round(float(pair.get("buy_pct") or 0), 2),
                    round(float(pair.get("sell_pct") or 0), 2),
                )
        return 0.0, 0.0

    def _d_lock_blocks(self, symbol: str, pair: dict[str, Any]) -> bool:
        lock = self.ban_d_lock.get(symbol)
        if not lock:
            return False
        buy = round(float(pair.get("buy_pct") or 0), 2)
        sell = round(float(pair.get("sell_pct") or 0), 2)
        lb = round(float(lock.get("buy") or 0), 2)
        ls = round(float(lock.get("sell") or 0), 2)

        def same(now: float, old: float) -> bool:
            if old <= 0 and now <= 0:
                return True
            if old <= 0 or now <= 0:
                return False
            return abs(now - old) < 0.02

        if same(buy, lb) and same(sell, ls):
            if symbol not in self._d_lock_noted and not self.is_banned(symbol):
                self._d_lock_noted.add(symbol)
                self.note(
                    f"{symbol} бан истёк, D та же BUY {buy:g}/SHORT {sell:g} — жду новую дистанцию"
                )
            return True
        self.ban_d_lock.pop(symbol, None)
        self._d_lock_noted.discard(symbol)
        self._save_bans()
        self.note(f"{symbol} новая D BUY {buy:g}/SHORT {sell:g} — лок бана снят, можно ставить")
        return False

    def public_d_locks(self) -> list[dict[str, Any]]:
        rows = []
        for symbol, lock in sorted(self.ban_d_lock.items()):
            if self.is_banned(symbol):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "buy": lock.get("buy") or 0,
                    "sell": lock.get("sell") or 0,
                }
            )
        return rows

    def _maybe_pair_ban(self, symbol: str) -> bool:
        symbol = str(symbol or "").upper()
        if not symbol or self.is_banned(symbol):
            return False
        if self._loss_streak(symbol) < self.pair_lose_limit:
            return False
        until = _now_ms() + int(self.pair_ban_hours * 3600 * 1000)
        buy, sell = self._plan_raw_d(symbol)
        algo = self.algos.get(symbol)
        if buy <= 0 and sell <= 0 and algo:
            buy = round(float(algo.buy_distance or 0), 2)
            sell = round(float(algo.sell_distance or 0), 2)
        self.bans[symbol] = until
        self.ban_until_hist[symbol] = until
        self.ban_d_lock[symbol] = {"buy": buy, "sell": sell}
        self._d_lock_noted.discard(symbol)
        self.v1_fail_bumps.pop(symbol, None)
        self._save_bans()
        self.note(
            f"БАН {symbol}: {self.pair_lose_limit} минуса подряд "
            f"— не следим {self.pair_ban_hours:g}ч, ордера только после новой D"
        )
        self._kill(symbol, "pair-ban")
        return True

    def _raise_v1_by(self, algo: Algo, add: float, why: str) -> None:
        add = round(float(add or 0), 2)
        if add <= 0 or self.is_banned(algo.symbol):
            return
        symbol = algo.symbol
        self.v1_fail_bumps[symbol] = round(self.v1_fail_bumps.get(symbol, 0) + add, 2)
        self._save_bans()
        if algo.buy_distance > 0:
            algo.buy_distance = round(algo.buy_distance + add, 2)
        if algo.sell_distance > 0:
            algo.sell_distance = round(algo.sell_distance + add, 2)
        algo.distance = algo.buy_distance if algo.buy_distance > 0 else algo.sell_distance
        gap = max(0.05, round(float(self.min_v2_gap or 0.3), 2))
        if algo.buy_distance > 0:
            want = round(algo.buy_distance + gap, 2)
            if algo.buy_v2_distance <= 0 or algo.buy_v2_distance + 1e-9 < want:
                algo.buy_v2_distance = want
                if algo.v2_state == "off":
                    algo.v2_state = "hunt"
                if algo.v2_state == "hunt":
                    algo.buy_v2_px = 0.0
                    algo.buy_v2_id = ""
        if algo.sell_distance > 0:
            want = round(algo.sell_distance + gap, 2)
            if algo.sell_v2_distance <= 0 or algo.sell_v2_distance + 1e-9 < want:
                algo.sell_v2_distance = want
                if algo.v2_state == "off":
                    algo.v2_state = "hunt"
                if algo.v2_state == "hunt":
                    algo.sell_v2_px = 0.0
                    algo.sell_v2_id = ""
        if algo.state == "hunt":
            algo.buy_px = 0.0
            algo.sell_px = 0.0
            algo.buy_id = algo.sell_id = ""
        algo.fingerprint = (
            f"{algo.buy_distance}|{algo.buy_tp}|{algo.sell_distance}|{algo.sell_tp}|"
            f"{algo.buy_v2_distance}|{algo.sell_v2_distance}"
        )
        self.note(
            f"{symbol} {why} — V1 +{add:g}% "
            f"(накоплено +{self.v1_fail_bumps[symbol]:g}%), V2 = V1+{gap:g}%"
        )

    def _maybe_v1_fail_bump(self, algo: Algo) -> None:
        """V1 минус → поднять первую D. V2 только следует за V1, в расчёт не входит."""
        bump = round(float(self.v1_fail_bump or 0), 2)
        if bump <= 0 or self.is_banned(algo.symbol):
            return
        if algo.last_v1_pnl is None or algo.last_v1_pnl > 0:
            return
        key = f"v1:{int(algo.last_v1_ts or 0)}"
        if algo.v1_rescue_key == key:
            return
        algo.v1_rescue_key = key
        self._raise_v1_by(algo, bump, "V1 минус")

    def _maybe_double_loss_depth(self, algo: Algo) -> None:
        """Два минуса V1 подряд → D_new = D + |ход%| − 0.05, V2 с зазором. V2 в расчёт не входит."""
        symbol = algo.symbol
        last_two: list[dict[str, Any]] = []
        v1_streak = 0
        for row in reversed(self._pair_trades_since_ban(symbol)):
            if str(row.get("layer") or "v1").lower() == "v2":
                continue
            if float(row.get("pnl_usd") or 0) > 0:
                break
            v1_streak += 1
            if len(last_two) < 2:
                last_two.append(row)
        if v1_streak < 2 or v1_streak >= self.pair_lose_limit or len(last_two) < 2:
            return
        key = f"{int(last_two[0].get('ts') or 0)}:{int(last_two[1].get('ts') or 0)}"
        if algo.depth_bump_key == key:
            return
        v1_row = last_two[0]
        move = abs(float(v1_row.get("pnl_pct") or 0))
        add = round(move - DEPTH_RECOVERY_INSIDE, 2)
        if add <= 0:
            return
        algo.depth_bump_key = key
        old_d = float(v1_row.get("distance") or algo.distance or 0)
        self._raise_v1_by(
            algo,
            add,
            f"два минуса V1: D {old_d:g}+|{move:g}|−{DEPTH_RECOVERY_INSIDE:g}",
        )

    def _filter_sides(self, sides: Sides) -> Sides:
        if not self.trade_long:
            sides.buy_d = sides.buy_tp = sides.buy_v2 = sides.buy_v2_tp = 0.0
        if not self.trade_short:
            sides.sell_d = sides.sell_tp = sides.sell_v2 = sides.sell_v2_tp = 0.0
        return sides

    def _plan_sides(self, pair: dict[str, Any]) -> Sides:
        symbol = str(pair.get("symbol") or "").upper()
        return self._apply_distance_rules(self._filter_sides(_sides_from_pair(pair)), symbol)

    def _apply_distance_rules(self, sides: Sides, symbol: str = "") -> Sides:
        """Смещение первой D от рекомендации; V2 не ближе min_v2_gap; пол min_order_distance."""
        off = round(float(self.v1_offset or 0), 2)
        extra = round(float(self.v1_fail_bumps.get(str(symbol or "").upper(), 0) or 0), 2)
        shift = round(off + extra, 2)
        if abs(shift) > 1e-9:
            if sides.buy_d > 0:
                sides.buy_d = round(sides.buy_d + shift, 2)
            if sides.sell_d > 0:
                sides.sell_d = round(sides.sell_d + shift, 2)
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

        tp_off = round(float(self.tp_offset or 0), 2)

        def apply_tp(tp: float, active: bool) -> float:
            if not active:
                return 0.0
            return round(max(float(tp or 0) + tp_off, MIN_TP_PCT), 2)

        sides.buy_tp = apply_tp(sides.buy_tp, sides.buy_d > 0)
        sides.sell_tp = apply_tp(sides.sell_tp, sides.sell_d > 0)
        sides.buy_v2_tp = apply_tp(sides.buy_v2_tp, sides.buy_v2 > 0)
        sides.sell_v2_tp = apply_tp(sides.sell_v2_tp, sides.sell_v2 > 0)
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
        if self.bans.get(str(symbol or "").upper(), 0) > _now_ms():
            return
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
        self._prune_bans()
        wanted: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            symbol = str(pair.get("symbol") or "").upper()
            if not symbol or self.is_banned(symbol):
                continue
            if self._d_lock_blocks(symbol, pair):
                continue
            sides = self._plan_sides(pair)
            if not sides.any():
                continue
            score_n = int(pair.get("score_plus") or 0) + int(pair.get("score_minus") or 0)
            if score_n and score_n < self.min_fills:
                continue
            age_min = int(self.max_rec_age_min or 0)
            last_ts = int(pair.get("last_ts") or 0)
            if age_min > 0 and last_ts > 0 and _now_ms() - last_ts > age_min * 60_000:
                continue
            if self._loss_streak(symbol) >= self.pair_lose_limit:
                self._maybe_pair_ban(symbol)
                continue
            wanted[symbol] = pair
        started: list[str] = []
        now = _now_ms()
        for symbol, algo in list(self.algos.items()):
            if self.is_banned(symbol):
                self._kill(symbol, "pair-ban")
                continue
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
        if not symbol or self.is_banned(symbol):
            return
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
        expired = self._prune_bans()
        if expired and self.plan_pairs and not self.halted:
            self.sync_plan(self.plan_pairs)
        if self.halted:
            return
        now = _now_ms()
        for algo in list(self.algos.values()):
            if now >= algo.until_ts:
                self._kill(algo.symbol, "expired")
                continue
            hunting_v2 = algo.v2_state == "hunt" and algo.state != "pos"
            hunting = algo.state == "hunt" or hunting_v2
            if not hunting:
                continue
            px = self.tapes[algo.symbol].delayed(self.cfg.follow_delay_ms, now)
            if px <= 0:
                continue
            buy = px * (1.0 - algo.buy_distance / 100.0) if algo.state == "hunt" and algo.buy_distance > 0 else 0.0
            sell = px * (1.0 + algo.sell_distance / 100.0) if algo.state == "hunt" and algo.sell_distance > 0 else 0.0
            buy_v2 = px * (1.0 - algo.buy_v2_distance / 100.0) if hunting_v2 and algo.buy_v2_distance > 0 else 0.0
            sell_v2 = px * (1.0 + algo.sell_v2_distance / 100.0) if hunting_v2 and algo.sell_v2_distance > 0 else 0.0
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
        if algo.v2_state == "hunt" and algo.state != "pos":
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
            sl = self.sl_for(side, "v2")
            self.note(f"вход V2 {algo.symbol} {side.upper()} @ {px:.6g} D{d}% SL{sl:g}% size={algo.size_usdt:g}$")
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
        if not self.emulate and self.broker:
            try:
                loop = asyncio.get_running_loop()
                for oid in (algo.buy_v2_id, algo.sell_v2_id):
                    if oid:
                        loop.create_task(self.broker.cancel(algo.symbol, oid))
            except RuntimeError:
                pass
        algo.buy_v2_id = ""
        algo.sell_v2_id = ""
        algo.buy_v2_px = 0.0
        algo.sell_v2_px = 0.0
        d = algo.buy_distance if side == "buy" else algo.sell_distance
        tp = algo.buy_tp if side == "buy" else algo.sell_tp
        algo.distance = d
        algo.tp = tp
        sl = self.sl_for(side, "v1")
        self.note(f"вход {algo.symbol} {side.upper()} @ {px:.6g} D{d}% SL{sl:g}% size={algo.size_usdt:g}$")

    def _maybe_exit(self, algo: Algo, ts: int, price: float) -> None:
        if algo.state == "pos" and algo.entry > 0:
            self._maybe_exit_layer(algo, ts, price, "v1")
        if algo.v2_state == "pos" and algo.v2_entry > 0:
            self._maybe_exit_layer(algo, ts, price, "v2")

    def sl_for(self, side: str, layer: str = "v1") -> float:
        if layer == "v2":
            raw = self.stop_loss_v2_long_pct if side == "buy" else self.stop_loss_v2_short_pct
        else:
            raw = self.stop_loss_long_pct if side == "buy" else self.stop_loss_short_pct
        return max(0.0, round(float(raw or 0), 2))

    def _maybe_exit_layer(self, algo: Algo, ts: int, price: float, layer: str) -> None:
        if layer == "v2":
            side, entry, _fill_ts = algo.v2_pos_side, algo.v2_entry, algo.v2_fill_ts
            tp = algo.buy_v2_tp if side == "buy" else algo.sell_v2_tp
        else:
            side, entry, _fill_ts = algo.pos_side, algo.entry, algo.fill_ts
            tp = algo.buy_tp if side == "buy" else algo.sell_tp
        if entry <= 0:
            return
        favor = (price - entry) / entry * 100.0 if side == "buy" else (entry - price) / entry * 100.0
        tp = max(tp, MIN_TP_PCT)
        sl = self.sl_for(side, layer)
        hit_tp = tp > 0 and favor + 1e-12 >= tp
        hit_sl = sl > 0 and favor - 1e-12 <= -sl
        if not hit_tp and not hit_sl:
            return
        if hit_tp:
            exit_px = entry * (1 + tp / 100.0) if side == "buy" else entry * (1 - tp / 100.0)
            tag = "TP"
        else:
            exit_px = entry * (1 - sl / 100.0) if side == "buy" else entry * (1 + sl / 100.0)
            tag = "SL"
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
        fee_pct = _fee_round_trip_pct(why, self.fee_maker_pct, self.fee_taker_pct)
        fee_usd = round(float(algo.size_usdt or 0) * fee_pct / 100.0, 4)
        net_usd = round(pnl - fee_usd, 4)
        net_pct = round(pct - fee_pct, 4)
        row = {
            "ts": _now_ms(),
            "symbol": algo.symbol,
            "side": side,
            "entry": entry,
            "exit": exit_px,
            "pnl_usd": net_usd,
            "pnl_pct": net_pct,
            "pnl_gross_usd": pnl,
            "pnl_gross_pct": pct,
            "fee_pct": fee_pct,
            "fee_usd": fee_usd,
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
                if "TP" in str(why).upper() and exit_px > 0:
                    px = self.broker.round_px(algo.symbol, exit_px)
                    loop.create_task(self.broker.close_limit(algo.symbol, close_side, px, algo.qty))
                else:
                    loop.create_task(self.broker.close_market(algo.symbol, close_side, algo.qty))
            except RuntimeError:
                pass
        tag = "V2 " if layer == "v2" else ""
        self.note(f"выход {tag}{algo.symbol} {why} pnl {net_usd:+.2f}$ (fee {fee_usd:.2f}$)")
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
        if layer == "v1":
            algo.last_v1_pnl = net_usd
            algo.last_v1_ts = int(row["ts"])
            if net_usd <= 0:
                self._maybe_v1_fail_bump(algo)
                self._maybe_double_loss_depth(algo)
        else:
            algo.last_v2_pnl = net_usd
            algo.last_v2_ts = int(row["ts"])
        if net_usd <= 0:
            self._maybe_pair_ban(algo.symbol)
        if net_usd <= -self.autostop_usd:
            self.emergency(f"сделка {algo.symbol} {net_usd:.2f}$ ≤ -{self.autostop_usd:g}$")

    def _check_open_loss(self, algo: Algo, price: float) -> None:
        entry_fee = float(algo.size_usdt or 0) * float(self.fee_maker_pct or 0) / 100.0
        if algo.state == "pos" and algo.entry > 0:
            pnl = _pnl_usd(algo.pos_side, algo.entry, price, algo.size_usdt) - entry_fee
            if pnl <= -self.autostop_usd:
                self._close(algo, price, "autostop", "v1")
                self.emergency(f"открытый минус {algo.symbol} {pnl:.2f}$")
                return
        if algo.v2_state == "pos" and algo.v2_entry > 0:
            pnl = _pnl_usd(algo.v2_pos_side, algo.v2_entry, price, algo.size_usdt) - entry_fee
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
                total -= float(algo.size_usdt or 0) * float(self.fee_maker_pct or 0) / 100.0
            if algo.v2_state == "pos" and algo.v2_entry > 0:
                total += _pnl_usd(algo.v2_pos_side, algo.v2_entry, last, algo.size_usdt)
                total -= float(algo.size_usdt or 0) * float(self.fee_maker_pct or 0) / 100.0
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
            "v1_offset": self.v1_offset,
            "tp_offset": self.tp_offset,
            "stop_loss_long_pct": self.stop_loss_long_pct,
            "stop_loss_short_pct": self.stop_loss_short_pct,
            "stop_loss_v2_long_pct": self.stop_loss_v2_long_pct,
            "stop_loss_v2_short_pct": self.stop_loss_v2_short_pct,
            "min_tp_pct": MIN_TP_PCT,
            "v1_fail_bump": self.v1_fail_bump,
            "v1_fail_bumps": [
                {"symbol": sym, "extra": extra}
                for sym, extra in sorted(self.v1_fail_bumps.items())
                if extra > 0
            ],
            "pair_lose_limit": self.pair_lose_limit,
            "pair_lose_window_hours": self.pair_lose_window_hours,
            "pair_ban_hours": self.pair_ban_hours,
            "min_fills": self.min_fills,
            "max_rec_age_min": self.max_rec_age_min,
            "fee_maker_pct": self.fee_maker_pct,
            "fee_taker_pct": self.fee_taker_pct,
            "bans": self.public_bans(),
            "d_locks": self.public_d_locks(),
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
