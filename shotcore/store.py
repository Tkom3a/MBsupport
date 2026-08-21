from __future__ import annotations

import csv
import json
import logging
import math
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .detector import ShotEvent

log = logging.getLogger("shotcore.store")

CSV_FIELDS = [
    "time",
    "symbol",
    "direction",
    "percent",
    "window_ms",
    "start_price",
    "extreme_price",
    "last_price",
    "trades",
    "quote_volume",
    "duration_ms",
    "btc_delta_pct",
    "btc_calm",
    "pct_300",
    "pct_1000",
    "pct_3000",
    "hold_ms",
    "exit_price",
    "rollback_pct",
    "vplus",
    "pnl_pct",
    "suggest_distance",
    "distance_report",
    "lever",
]


class ShotStore:
    def __init__(
        self,
        directory: Path,
        csv_name: str,
        jsonl_name: str,
        hints_name: str,
        tz_name: str = "UTC",
        distance_levels: list[float] | None = None,
        retain_hours: int = 24,
        tp_min_pct: float = 0.3,
        hold_ms: int = 300,
        suggest_inside_pct: float = 0.05,
        suggest_inside_max_pct: float = 0.10,
        min_win_pct: float = 70.0,
        min_fills: int = 3,
        mt_plan_name: str = "mt_plan.json",
        mt_run_hours: float = 3.0,
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.directory / csv_name
        self.jsonl_path = self.directory / jsonl_name
        self.hints_path = self.directory / hints_name
        self.mt_plan_path = self.directory / mt_plan_name
        self.mt_plan_csv_path = self.mt_plan_path.with_suffix(".csv")
        self.mt_run_hours = max(float(mt_run_hours), 0.1)
        self.tz = _zone(tz_name)
        self.distance_levels = distance_levels or [1.11, 1.32, 1.42, 1.63, 1.78]
        self.retain_hours = max(1, int(retain_hours))
        self.tp_min_pct = max(float(tp_min_pct), 0.0)
        self.hold_ms = max(int(hold_ms), 50)
        self.suggest_inside_pct = max(float(suggest_inside_pct), 0.0)
        self.suggest_inside_max_pct = max(float(suggest_inside_max_pct), self.suggest_inside_pct)
        self.min_win_pct = max(float(min_win_pct), 0.0)
        self.min_fills = max(int(min_fills), 1)
        self.events: deque[dict[str, Any]] = deque(maxlen=50_000)
        self.total = 0
        self._stats_memo: tuple[Any, dict[str, Any]] | None = None
        self._ensure_csv()
        skipped, dropped_paths = self._load_existing()
        if skipped or dropped_paths:
            self._rewrite_files()
            log.info(
                "Dropped %s shots older than %sh, stripped tick paths from %s",
                skipped,
                self.retain_hours,
                dropped_paths,
            )

    def _ensure_csv(self) -> None:
        if self.csv_path.exists():
            with self.csv_path.open(encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            if first and "vplus" in first and "suggest_distance" in first and "lever" in first:
                return
            backup = self.csv_path.with_suffix(".csv.bak")
            self.csv_path.replace(backup)
            log.info("Rotated old shots.csv -> %s", backup.name)
        with self.csv_path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_FIELDS).writeheader()

    def _load_existing(self) -> tuple[int, int]:
        source = self.jsonl_path if self.jsonl_path.exists() else self.csv_path
        if not source.exists():
            return 0, 0
        loaded = 0
        skipped = 0
        dropped_paths = 0
        cutoff = _cutoff_ms(self.retain_hours)
        try:
            if source.suffix == ".jsonl":
                with source.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        row = json.loads(line)
                        event = _row_to_event(row)
                        if not event:
                            continue
                        if event["peak_ts"] < cutoff:
                            skipped += 1
                            continue
                        if row.get("path"):
                            dropped_paths += 1
                        self.events.append(event)
                        loaded += 1
            else:
                with source.open(encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        event = _row_to_event(row)
                        if not event:
                            continue
                        if event["peak_ts"] < cutoff:
                            skipped += 1
                            continue
                        self.events.append(event)
                        loaded += 1
            self.total = len(self.events)
            log.info("Loaded %s historic shots from %s (skipped %s older than %sh)", loaded, source.name, skipped, self.retain_hours)
        except Exception as exc:
            log.warning("Could not load historic shots: %s", exc)
        return skipped, dropped_paths

    def write(self, event: ShotEvent) -> dict[str, Any]:
        when = datetime.fromtimestamp(event.peak_ts / 1000, tz=timezone.utc)
        row = {
            "time": when.isoformat(),
            "symbol": event.symbol,
            "direction": event.direction,
            "percent": f"{event.percent:.4f}",
            "window_ms": event.window_ms,
            "start_price": event.start_price,
            "extreme_price": event.extreme_price,
            "last_price": event.last_price,
            "trades": event.trades,
            "quote_volume": event.quote_volume,
            "duration_ms": event.duration_ms,
            "btc_delta_pct": event.btc_delta_pct,
            "btc_calm": int(event.btc_calm),
            "pct_300": event.pct_300,
            "pct_1000": event.pct_1000,
            "pct_3000": event.pct_3000,
            "hold_ms": event.hold_ms,
            "exit_price": event.exit_price,
            "rollback_pct": event.rollback_pct,
            "vplus": int(event.vplus),
            "pnl_pct": event.pnl_pct,
            "suggest_distance": event.suggest_distance,
            "distance_report": json.dumps(event.distance_report, ensure_ascii=False),
            "lever": event.lever,
            "fill_ts": event.fill_ts,
            "fill_price": event.fill_price,
            "start_ts": event.start_ts,
        }
        stored = _row_to_event(row)
        if stored:
            self.events.append(stored)
        self._stats_memo = None
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore").writerow(row)
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        **{k: row[k] for k in CSV_FIELDS},
                        "btc_calm": bool(event.btc_calm),
                        "vplus": bool(event.vplus),
                        "fill_ts": event.fill_ts,
                        "fill_price": event.fill_price,
                        "start_ts": event.start_ts,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.total += 1
        log.info(
            "SHOT %s %s x%.0f %.2f%% dist=%.2f pnl=%.3f%% hold=%sms",
            event.direction,
            event.symbol,
            event.lever,
            event.percent,
            event.suggest_distance,
            event.pnl_pct,
            event.hold_ms,
        )
        return stored or row

    def recent(
        self,
        limit: int = 80,
        lookback_min: int = 0,
        direction: str = "",
        only_btc_calm: bool = False,
        symbol: str = "",
    ) -> list[dict[str, Any]]:
        items = self._filtered(lookback_min, direction, only_btc_calm)
        needle = str(symbol or "").strip().upper()
        if needle:
            items = [item for item in items if needle in str(item.get("symbol") or "").upper()]
        items = items[-limit:]
        items.reverse()
        return [_public_event(item, self.tz) for item in items]

    def stats(self, lookback_min: int = 0, direction: str = "", only_btc_calm: bool = False) -> dict[str, Any]:
        items = self._filtered(lookback_min, direction, only_btc_calm)
        memo_key = (
            lookback_min,
            direction,
            only_btc_calm,
            len(items),
            int(items[-1]["peak_ts"]) if items else 0,
            self.total,
        )
        if self._stats_memo and self._stats_memo[0] == memo_key:
            return dict(self._stats_memo[1])
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[item["symbol"]].append(item)

        rows = []
        for symbol, shots in grouped.items():
            percents = [float(x["percent"]) for x in shots]
            downs = [float(x["percent"]) for x in shots if x["direction"] == "DOWN"]
            ups = [float(x["percent"]) for x in shots if x["direction"] == "UP"]
            pnls = [float(x["pnl_pct"]) for x in shots]
            last = shots[-1]
            ordered = sorted(percents)
            down_shots = [x for x in shots if x["direction"] == "DOWN"]
            up_shots = [x for x in shots if x["direction"] == "UP"]
            buy_d, buy_plus, buy_minus, buy_win, buy_tp, buy_hint = _recommend_and_score(
                down_shots,
                self.hold_ms,
                self.suggest_inside_pct,
                self.distance_levels,
                inside_max=self.suggest_inside_max_pct,
                min_win_pct=self.min_win_pct,
                min_fills=self.min_fills,
            )
            sell_d, sell_plus, sell_minus, sell_win, sell_tp, sell_hint = _recommend_and_score(
                up_shots,
                self.hold_ms,
                self.suggest_inside_pct,
                self.distance_levels,
                inside_max=self.suggest_inside_max_pct,
                min_win_pct=self.min_win_pct,
                min_fills=self.min_fills,
            )
            # Сводная колонка — лучшая из сторон, без смешивания UP+DOWN в одну D.
            if buy_d > 0 and (sell_d <= 0 or (buy_plus, buy_win) >= (sell_plus, sell_win)):
                suggest, plus_n, minus_n, win_prob, suggest_tp, filter_hint = (
                    buy_d, buy_plus, buy_minus, buy_win, buy_tp, f"BUY {buy_hint}".strip()
                )
            else:
                suggest, plus_n, minus_n, win_prob, suggest_tp, filter_hint = (
                    sell_d, sell_plus, sell_minus, sell_win, sell_tp, (f"SHORT {sell_hint}".strip() if sell_d else "")
                )
            rows.append(
                {
                    "symbol": symbol,
                    "count": len(shots),
                    "count_down": len(downs),
                    "count_up": len(ups),
                    "avg": round(statistics.fmean(percents), 4),
                    "avg_down": round(statistics.fmean(downs), 4) if downs else 0.0,
                    "avg_up": round(statistics.fmean(ups), 4) if ups else 0.0,
                    "p50": round(_percentile(ordered, 50), 4),
                    "p70": round(_percentile(ordered, 70), 4),
                    "p80": round(_percentile(ordered, 80), 4),
                    "p90": round(_percentile(ordered, 90), 4),
                    "max": round(ordered[-1], 4),
                    "suggest_distance": suggest,
                    "suggest_tp_pct": suggest_tp,
                    "buy_pct": buy_d,
                    "buy_tp_pct": buy_tp,
                    "buy_win_prob": buy_win,
                    "buy_score_text": f"{buy_plus}/{buy_minus}" if buy_d else "",
                    "buy_filter_hint": buy_hint,
                    "sell_pct": sell_d,
                    "sell_tp_pct": sell_tp,
                    "sell_win_prob": sell_win,
                    "sell_score_text": f"{sell_plus}/{sell_minus}" if sell_d else "",
                    "sell_filter_hint": sell_hint,
                    "filter_hint": filter_hint,
                    "score_plus": plus_n,
                    "score_minus": minus_n,
                    "score_text": f"{plus_n}/{minus_n}",
                    "win_prob": win_prob,
                    "vplus": plus_n,
                    "vplus_rate": win_prob,
                    "avg_pnl": round(statistics.fmean(pnls), 4) if pnls else 0.0,
                    "last_percent": round(float(last["percent"]), 4),
                    "last_direction": last["direction"],
                    "last_vplus": bool(last["vplus"]),
                    "last_time": _fmt_local(last["peak_ts"], self.tz),
                    "last_ts": last["peak_ts"],
                    "lever": float(last.get("lever") or 0),
                }
            )
        rows.sort(key=lambda row: (row["count"], row["last_ts"]), reverse=True)
        all_pct = [float(x["percent"]) for x in items]
        vplus_n = sum(1 for x in items if x["vplus"])
        payload = {
            "lookback_min": lookback_min,
            "shots": len(items),
            "pairs": len(rows),
            "avg": round(statistics.fmean(all_pct), 4) if all_pct else 0.0,
            "vplus": vplus_n,
            "vplus_rate": round(100.0 * vplus_n / len(items), 1) if items else 0.0,
            "hold_ms": items[-1]["hold_ms"] if items else 300,
            "rows": rows,
        }
        self._stats_memo = (memo_key, payload)
        return dict(payload)

    def write_hints(
        self,
        lookback_min: int = 0,
        subscribed: set[str] | None = None,
        run_hours: float | None = None,
    ) -> None:
        payload = self.stats(lookback_min=lookback_min)
        with self.hints_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "symbol",
                    "count",
                    "suggest_distance",
                    "suggest_tp_pct",
                    "buy_pct",
                    "buy_tp_pct",
                    "sell_pct",
                    "sell_tp_pct",
                    "filter_hint",
                    "score_plus",
                    "score_minus",
                    "win_prob",
                    "vplus_rate",
                    "avg_pnl",
                    "avg",
                    "p50",
                    "p70",
                    "p90",
                    "max",
                ],
            )
            writer.writeheader()
            for row in payload["rows"]:
                writer.writerow({key: row[key] for key in writer.fieldnames})
        self.write_mt_plan(payload, subscribed=subscribed, run_hours=run_hours)

    def build_mt_plan(
        self,
        lookback_min: int = 0,
        subscribed: set[str] | None = None,
        run_hours: float | None = None,
        stats_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = stats_payload or self.stats(lookback_min=lookback_min)
        hours = max(float(run_hours if run_hours is not None else self.mt_run_hours), 0.1)
        hold_ms = int(payload.get("hold_ms") or self.hold_ms)
        now = datetime.now(tz=timezone.utc)
        active = {str(x) for x in (subscribed or [])}
        pairs = []
        for row in payload.get("rows") or []:
            buy_pct = round(float(row.get("buy_pct") or 0), 4)
            sell_pct = round(float(row.get("sell_pct") or 0), 4)
            recommend = round(float(row.get("suggest_distance") or 0), 4)
            if buy_pct <= 0 and sell_pct <= 0 and recommend <= 0:
                continue
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            tp = round(float(row.get("suggest_tp_pct") or 0), 4)
            pairs.append(
                {
                    "symbol": symbol,
                    "base": symbol.split("-")[0],
                    "recommend_pct": recommend,
                    "tp_pct": tp,
                    "buy_pct": buy_pct,
                    "buy_tp_pct": round(float(row.get("buy_tp_pct") or 0), 4),
                    "buy_win_prob": float(row.get("buy_win_prob") or 0),
                    "sell_pct": sell_pct,
                    "sell_tp_pct": round(float(row.get("sell_tp_pct") or 0), 4),
                    "sell_win_prob": float(row.get("sell_win_prob") or 0),
                    "hold_ms": hold_ms,
                    "run_hours": hours,
                    "lever": int(round(float(row.get("lever") or 0))),
                    "subscribed": symbol in active if active else False,
                    "count": int(row.get("count") or 0),
                    "score_plus": int(row.get("score_plus") or 0),
                    "score_minus": int(row.get("score_minus") or 0),
                    "filter_hint": row.get("filter_hint") or "",
                    "win_prob": float(row.get("win_prob") or 0),
                }
            )
        return {
            "schema": "shotcore.mt_plan.v2",
            "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_ts": int(now.timestamp() * 1000),
            "run_hours": hours,
            "hold_ms": hold_ms,
            "lookback_min": int(payload.get("lookback_min") or lookback_min or 0),
            "pairs": pairs,
        }

    def write_mt_plan(
        self,
        stats_payload: dict[str, Any] | None = None,
        subscribed: set[str] | None = None,
        run_hours: float | None = None,
        lookback_min: int = 0,
    ) -> dict[str, Any]:
        plan = self.build_mt_plan(
            lookback_min=lookback_min,
            subscribed=subscribed,
            run_hours=run_hours,
            stats_payload=stats_payload,
        )
        _atomic_write(self.mt_plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        fields = [
            "symbol",
            "base",
            "recommend_pct",
            "tp_pct",
            "buy_pct",
            "buy_tp_pct",
            "sell_pct",
            "sell_tp_pct",
            "hold_ms",
            "run_hours",
            "lever",
            "subscribed",
            "count",
            "win_prob",
            "updated_utc",
        ]
        stream = StringIO()
        csv_writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        csv_writer.writeheader()
        updated = plan["updated_utc"]
        for pair in plan["pairs"]:
            csv_writer.writerow(
                {
                    "symbol": pair["symbol"],
                    "base": pair["base"],
                    "recommend_pct": pair["recommend_pct"],
                    "tp_pct": pair["tp_pct"],
                    "buy_pct": pair.get("buy_pct") or 0,
                    "buy_tp_pct": pair.get("buy_tp_pct") or 0,
                    "sell_pct": pair.get("sell_pct") or 0,
                    "sell_tp_pct": pair.get("sell_tp_pct") or 0,
                    "hold_ms": pair["hold_ms"],
                    "run_hours": pair["run_hours"],
                    "lever": pair["lever"],
                    "subscribed": int(bool(pair["subscribed"])),
                    "count": pair["count"],
                    "win_prob": pair["win_prob"],
                    "updated_utc": updated,
                }
            )
        _atomic_write(self.mt_plan_csv_path, stream.getvalue())
        log.info("MT plan: %s pairs -> %s", len(plan["pairs"]), self.mt_plan_path.name)
        return plan

    def _filtered(self, lookback_min: int, direction: str = "", only_btc_calm: bool = False) -> list[dict[str, Any]]:
        cutoff = 0
        if lookback_min > 0:
            cutoff = int(datetime.now(tz=timezone.utc).timestamp() * 1000) - lookback_min * 60_000
        wanted = direction.upper()
        out = []
        for item in self.events:
            if item["peak_ts"] < cutoff:
                continue
            if wanted and item["direction"] != wanted:
                continue
            if only_btc_calm and not item["btc_calm"]:
                continue
            out.append(item)
        return out

    def prune(self) -> int:
        cutoff = _cutoff_ms(self.retain_hours)
        kept = [item for item in self.events if item["peak_ts"] >= cutoff]
        dropped = len(self.events) - len(kept)
        if dropped <= 0:
            return 0
        self.events = deque(kept, maxlen=50_000)
        self.total = len(self.events)
        self._stats_memo = None
        self._rewrite_files()
        log.info("Retention: removed %s shots older than %sh, kept %s", dropped, self.retain_hours, self.total)
        return dropped

    def purge_sidecar_files(self) -> int:
        removed = 0
        cutoff = time.time() - self.retain_hours * 3600
        suffixes = {".bak", ".png", ".jpg", ".jpeg", ".webp", ".tmp"}
        for path in self.directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def _rewrite_files(self) -> None:
        rows = [_event_to_row(item) for item in self.events]
        tmp_csv = self.csv_path.with_suffix(".csv.tmp")
        with tmp_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        tmp_csv.replace(self.csv_path)
        tmp_jsonl = self.jsonl_path.with_suffix(".jsonl.tmp")
        with tmp_jsonl.open("w", encoding="utf-8") as fh:
            for item in self.events:
                row = _event_to_row(item)
                fh.write(
                    json.dumps(
                        {
                            **row,
                            "btc_calm": bool(item.get("btc_calm")),
                            "vplus": bool(item.get("vplus")),
                            "fill_ts": int(item.get("fill_ts") or 0),
                            "fill_price": float(item.get("fill_price") or 0),
                            "start_ts": int(item.get("start_ts") or 0),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        tmp_jsonl.replace(self.jsonl_path)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _recommend_and_score(
    shots: list[dict[str, Any]],
    hold_ms: int,
    inside: float,
    levels: list[float] | None = None,
    inside_max: float | None = None,
    min_win_pct: float = 70.0,
    min_fills: int = 3,
) -> tuple[float, int, int, float, float, str]:
    """Все прострелы пары, затем D у края и фильтр шума.

    Ордер на 0.05–0.10% внутрь экстремума. Выход: TP за 0.3 с или по времени.
    Рекомендация только если доля плюса не ниже min_win_pct.
    """
    inside_hi = inside if inside_max is None else inside_max
    variants: list[tuple[str, list[dict[str, Any]]]] = [("все", shots)]
    calm = [row for row in shots if row.get("btc_calm")]
    if min_fills <= len(calm) < len(shots):
        variants.append(("спокойный BTC", calm))
    percents = sorted(float(row.get("percent") or 0) for row in shots if float(row.get("percent") or 0) > 0)
    if percents:
        floor = round(_percentile(percents, 50), 2)
        if floor >= 1.0:
            deep = [row for row in shots if float(row.get("percent") or 0) + 1e-9 >= floor]
            if min_fills <= len(deep) < len(shots):
                variants.append((f"глубина ≥ {floor:g}%", deep))
    best: tuple | None = None
    hint = ""
    for name, subset in variants:
        found = _search_dtp(
            subset,
            hold_ms,
            inside,
            inside_hi,
            levels or [],
            min_win_pct,
            min_fills,
        )
        if found is None:
            continue
        if _score_better(found, best):
            best = found
            hint = name
    if best is None:
        return 0.0, 0, 0, 0.0, 0.0, ""
    _total, plus, _hits, filled, distance, tp = best
    minus = int(filled) - plus
    prob = round(100.0 * plus / filled, 1) if filled else 0.0
    return round(distance, 2), plus, minus, prob, round(tp, 2), hint


def _search_dtp(
    shots: list[dict[str, Any]],
    hold_ms: int,
    inside_min: float,
    inside_max: float,
    levels: list[float],
    min_win_pct: float,
    min_fills: int,
) -> tuple | None:
    if len(shots) < min_fills:
        return None
    distances = _candidate_distances(shots, levels, inside_min, inside_max)
    if not distances:
        return None
    indexed = [(shot, _report_map(shot)) for shot in shots]
    tps = _candidate_tps()
    best: tuple | None = None
    for distance in distances:
        fills: list[tuple[float, float]] = []
        for shot, report in indexed:
            sim = _fill_mfe(shot, distance, hold_ms, report)
            if sim is None:
                continue
            fills.append(sim)
        if len(fills) < min_fills:
            continue
        max_mfe = max(mfe for _time_pnl, mfe in fills)
        tp_set = {tp for tp in tps if 0 < tp <= max_mfe + 1e-9}
        for _time_pnl, mfe in fills:
            hittable = math.floor(mfe * 100.0 + 1e-9) / 100.0
            if hittable >= 0.05:
                tp_set.add(round(hittable, 2))
                tp_set.add(round(max(0.05, hittable - 0.01), 2))
        options: list[float | None] = sorted(tp_set)
        options.append(None)
        for tp in options:
            total = 0.0
            plus = 0
            hits = 0
            for time_pnl, mfe in fills:
                if tp is not None and mfe + 1e-12 >= tp:
                    pnl = tp
                    hits += 1
                else:
                    pnl = time_pnl
                total += pnl
                if pnl > 0:
                    plus += 1
            win = 100.0 * plus / len(fills)
            if win + 1e-9 < min_win_pct or total <= 0:
                continue
            shown_tp = 0.0 if tp is None else tp
            key = (round(total, 6), plus, hits, len(fills), distance, shown_tp)
            if _score_better(key, best):
                best = key
    return best


def _score_better(key: tuple, best: tuple | None) -> bool:
    if best is None:
        return True
    if abs(key[0] - best[0]) > 1e-6:
        return key[0] > best[0]
    if key[1] != best[1]:
        return key[1] > best[1]
    if key[2] != best[2]:
        return key[2] > best[2]
    if key[3] != best[3]:
        return key[3] > best[3]
    return key[4] >= best[4]


def _inside_steps(inside_min: float, inside_max: float) -> list[float]:
    lo = max(0.05, round(float(inside_min or 0.05), 2))
    hi = max(lo, round(float(inside_max or lo), 2))
    out: list[float] = []
    cursor = lo
    while cursor <= hi + 1e-9:
        out.append(round(cursor, 2))
        cursor = round(cursor + 0.01, 2)
    return out


def _candidate_distances(
    shots: list[dict[str, Any]],
    levels: list[float],
    inside_min: float,
    inside_max: float | None = None,
) -> list[float]:
    """D у края прострела: экстремум минус 0.05…0.10%, плюс уровни MT."""
    dists: set[float] = {round(float(level), 2) for level in levels if float(level) > 0}
    insides = _inside_steps(inside_min, inside_min if inside_max is None else inside_max)
    for shot in shots:
        pct = float(shot.get("percent") or 0)
        if pct < 0.5:
            continue
        for inside in insides:
            dist = round(pct - inside, 2)
            if 0.5 <= dist <= 20.0:
                dists.add(dist)
    return sorted(d for d in dists if 0.5 <= d <= 20.0)


def _candidate_tps() -> list[float]:
    return [round(step * 0.05, 2) for step in range(2, 41)]


def _report_map(shot: dict[str, Any]) -> dict[float, dict[str, Any]]:
    out: dict[float, dict[str, Any]] = {}
    for row in shot.get("distance_report") or []:
        distance = round(float(row.get("distance") or 0), 2)
        if distance > 0:
            out[distance] = row
    return out


def _nearest_report(report: dict[float, dict[str, Any]], distance: float) -> dict[str, Any] | None:
    if not report:
        return None
    want = round(distance, 2)
    exact = report.get(want)
    if exact is not None:
        return exact
    nearest = min(report, key=lambda item: abs(item - want))
    if abs(nearest - want) <= 0.051:
        return report[nearest]
    return None


def _fill_mfe(
    shot: dict[str, Any],
    distance: float,
    hold_ms: int,
    report: dict[float, dict[str, Any]] | None = None,
) -> tuple[float, float] | None:
    """(pnl за 0.3с, MFE за 0.3с после входа). None = ордер не исполнился."""
    start = float(shot.get("start_price") or 0)
    pct = float(shot.get("percent") or 0)
    if start <= 0 or distance <= 0 or pct + 1e-9 < distance:
        return None
    row = _nearest_report(report if report is not None else _report_map(shot), distance)
    if row is not None:
        if not row.get("filled"):
            return None
        time_pnl = float(row.get("pnl_pct") or 0)
        mfe = float(row.get("mfe_pct") or 0)
        return time_pnl, max(mfe, time_pnl, 0.0)
    time_pnl = _estimate_hold_pnl(shot, distance, hold_ms)
    return time_pnl, max(time_pnl, 0.0)


def _estimate_hold_pnl(shot: dict[str, Any], distance: float, hold_ms: int) -> float:
    """Оценка выхода через 0.3 с, если по D нет тиковой симуляции (старые записи)."""
    start = float(shot.get("start_price") or 0)
    pct = float(shot.get("percent") or 0)
    direction = str(shot.get("direction") or "").upper()
    if start <= 0 or pct <= 0:
        return 0.0
    fill_px = start * (1.0 - distance / 100.0) if direction == "DOWN" else start * (1.0 + distance / 100.0)
    duration = max(int(shot.get("duration_ms") or 0), 1)
    hold = max(int(hold_ms or 300), 1)
    t_to_peak = duration * max(0.0, (pct - distance) / pct)
    if hold <= t_to_peak:
        extra = (pct - distance) * (hold / t_to_peak) if t_to_peak > 0 else 0.0
        moved = distance + extra
        hold_px = start * (1.0 - moved / 100.0) if direction == "DOWN" else start * (1.0 + moved / 100.0)
        return _pnl_from_fill(direction, fill_px, hold_px)
    extreme = float(shot.get("extreme_price") or 0)
    if extreme <= 0:
        extreme = start * (1.0 - pct / 100.0) if direction == "DOWN" else start * (1.0 + pct / 100.0)
    frac = min(1.0, (hold - t_to_peak) / float(hold))
    bounce = float(shot.get("rollback_pct") or 0) * frac
    if direction == "DOWN":
        hold_px = extreme + bounce / 100.0 * start
    else:
        hold_px = extreme - bounce / 100.0 * start
    return _pnl_from_fill(direction, fill_px, hold_px)


def _pnl_from_fill(direction: str, fill_px: float, px: float) -> float:
    if fill_px <= 0 or px <= 0:
        return 0.0
    if direction == "DOWN":
        return (px - fill_px) / fill_px * 100.0
    return (fill_px - px) / fill_px * 100.0


def _cutoff_ms(retain_hours: int) -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000) - retain_hours * 3_600_000


def _event_to_row(item: dict[str, Any]) -> dict[str, Any]:
    when = datetime.fromtimestamp(int(item["peak_ts"]) / 1000, tz=timezone.utc)
    report = item.get("distance_report") or []
    if not isinstance(report, str):
        report = json.dumps(report, ensure_ascii=False)
    return {
        "time": when.isoformat(),
        "symbol": item.get("symbol") or "",
        "direction": item.get("direction") or "",
        "percent": f"{float(item.get('percent') or 0):.4f}",
        "window_ms": int(item.get("window_ms") or 0),
        "start_price": item.get("start_price") or 0,
        "extreme_price": item.get("extreme_price") or 0,
        "last_price": item.get("last_price") or 0,
        "trades": item.get("trades") or 0,
        "quote_volume": item.get("quote_volume") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "btc_delta_pct": item.get("btc_delta_pct") or 0,
        "btc_calm": int(bool(item.get("btc_calm"))),
        "pct_300": item.get("pct_300") or 0,
        "pct_1000": item.get("pct_1000") or 0,
        "pct_3000": item.get("pct_3000") or 0,
        "hold_ms": item.get("hold_ms") or 300,
        "exit_price": item.get("exit_price") or 0,
        "rollback_pct": item.get("rollback_pct") or 0,
        "vplus": int(bool(item.get("vplus"))),
        "pnl_pct": item.get("pnl_pct") or 0,
        "suggest_distance": item.get("suggest_distance") or 0,
        "distance_report": report,
        "lever": item.get("lever") or 0,
    }


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _fmt_local(ts_ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%d.%m %H:%M:%S")


def _fill_meta(item: dict[str, Any]) -> tuple[int, float]:
    dist = float(item.get("suggest_distance") or 0)
    start = float(item.get("start_price") or 0)
    direction = str(item.get("direction") or "").upper()
    fill_price = float(item.get("fill_price") or 0)
    fill_ts = int(item.get("fill_ts") or 0)
    if fill_price <= 0 and start > 0 and dist > 0:
        fill_price = start * (1.0 - dist / 100.0) if direction == "DOWN" else start * (1.0 + dist / 100.0)
    if fill_ts <= 0 or fill_price <= 0:
        for row in item.get("distance_report") or []:
            if abs(float(row.get("distance") or 0) - dist) > 1e-6:
                continue
            if not fill_ts:
                fill_ts = int(row.get("fill_ts") or 0)
            if fill_price <= 0 and float(row.get("fill_price") or 0) > 0:
                fill_price = float(row["fill_price"])
            break
    return fill_ts, fill_price


def _public_event(item: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    fill_ts, fill_price = _fill_meta(item)
    return {
        "time": _fmt_local(item["peak_ts"], tz),
        "symbol": item["symbol"],
        "direction": item["direction"],
        "percent": item["percent"],
        "window_ms": item["window_ms"],
        "btc_calm": item["btc_calm"],
        "vplus": item["vplus"],
        "pnl_pct": item["pnl_pct"],
        "suggest_distance": item["suggest_distance"],
        "rollback_pct": item["rollback_pct"],
        "lever": item.get("lever") or 0,
        "peak_ts": item["peak_ts"],
        "start_ts": int(item.get("start_ts") or 0),
        "start_price": float(item.get("start_price") or 0),
        "extreme_price": float(item.get("extreme_price") or 0),
        "last_price": float(item.get("last_price") or 0),
        "exit_price": float(item.get("exit_price") or 0),
        "fill_ts": fill_ts,
        "fill_price": fill_price,
        "quote_volume": float(item.get("quote_volume") or 0),
        "duration_ms": int(item.get("duration_ms") or 0),
        "hold_ms": int(item.get("hold_ms") or 300),
    }


def _row_to_event(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        symbol = str(row.get("symbol") or "")
        percent = float(row.get("percent") or 0)
        if not symbol or percent <= 0:
            return None
        raw_time = str(row.get("time") or "")
        peak_ts = _parse_ts(raw_time)
        calm = _as_bool(row.get("btc_calm"))
        report = row.get("distance_report") or []
        if isinstance(report, str) and report:
            try:
                report = json.loads(report)
            except json.JSONDecodeError:
                report = []
        if not isinstance(report, list):
            report = []
        return {
            "peak_ts": peak_ts,
            "symbol": symbol,
            "direction": str(row.get("direction") or "").upper(),
            "percent": percent,
            "window_ms": int(float(row.get("window_ms") or 0)),
            "btc_calm": calm,
            "hold_ms": int(float(row.get("hold_ms") or 300)),
            "vplus": _as_bool(row.get("vplus")),
            "pnl_pct": float(row.get("pnl_pct") or 0),
            "suggest_distance": float(row.get("suggest_distance") or 0),
            "rollback_pct": float(row.get("rollback_pct") or 0),
            "distance_report": report,
            "lever": float(row.get("lever") or 0),
            "start_price": float(row.get("start_price") or 0),
            "extreme_price": float(row.get("extreme_price") or 0),
            "last_price": float(row.get("last_price") or 0),
            "quote_volume": float(row.get("quote_volume") or 0),
            "duration_ms": int(float(row.get("duration_ms") or 0)),
            "start_ts": int(float(row.get("start_ts") or 0)),
            "exit_price": float(row.get("exit_price") or 0),
            "fill_ts": int(float(row.get("fill_ts") or 0)),
            "fill_price": float(row.get("fill_price") or 0),
        }
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() in {"1", "true", "True"}
        return bool(value)


def _parse_ts(value: str) -> int:
    if not value:
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (pct / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    weight = pos - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight
