from __future__ import annotations

import csv
import json
import logging
import math
import statistics
from collections import defaultdict, deque
from datetime import datetime, timezone
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
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.directory / csv_name
        self.jsonl_path = self.directory / jsonl_name
        self.hints_path = self.directory / hints_name
        self.tz = _zone(tz_name)
        self.distance_levels = distance_levels or [1.11, 1.32, 1.42, 1.63, 1.78]
        self.events: deque[dict[str, Any]] = deque(maxlen=50_000)
        self.total = 0
        self._ensure_csv()
        self._load_existing()

    def _ensure_csv(self) -> None:
        if self.csv_path.exists():
            with self.csv_path.open(encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            if first and "vplus" in first and "suggest_distance" in first:
                return
            backup = self.csv_path.with_suffix(".csv.bak")
            self.csv_path.replace(backup)
            log.info("Rotated old shots.csv -> %s", backup.name)
        with self.csv_path.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_FIELDS).writeheader()

    def _load_existing(self) -> None:
        source = self.jsonl_path if self.jsonl_path.exists() else self.csv_path
        if not source.exists():
            return
        loaded = 0
        try:
            if source.suffix == ".jsonl":
                with source.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        row = json.loads(line)
                        event = _row_to_event(row)
                        if event:
                            self.events.append(event)
                            loaded += 1
            else:
                with source.open(encoding="utf-8", newline="") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        event = _row_to_event(row)
                        if event:
                            self.events.append(event)
                            loaded += 1
            self.total = len(self.events)
            log.info("Loaded %s historic shots from %s", loaded, source.name)
        except Exception as exc:
            log.warning("Could not load historic shots: %s", exc)

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
        }
        stored = _row_to_event(row)
        if stored:
            self.events.append(stored)
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_FIELDS).writerow(row)
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "btc_calm": bool(event.btc_calm), "vplus": bool(event.vplus)}, ensure_ascii=False) + "\n")
        self.total += 1
        mark = "В+" if event.vplus else "В−"
        log.info(
            "SHOT %s %s %.2f%% dist=%.2f %s pnl=%.3f%% hold=%sms",
            event.direction,
            event.symbol,
            event.percent,
            event.suggest_distance,
            mark,
            event.pnl_pct,
            event.hold_ms,
        )
        return stored or row

    def recent(self, limit: int = 80, lookback_min: int = 0, direction: str = "") -> list[dict[str, Any]]:
        items = self._filtered(lookback_min, direction)
        items = items[-limit:]
        items.reverse()
        return [_public_event(item, self.tz) for item in items]

    def stats(self, lookback_min: int = 0, direction: str = "", only_btc_calm: bool = False) -> dict[str, Any]:
        items = self._filtered(lookback_min, direction, only_btc_calm)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[item["symbol"]].append(item)

        rows = []
        for symbol, shots in grouped.items():
            percents = [float(x["percent"]) for x in shots]
            downs = [float(x["percent"]) for x in shots if x["direction"] == "DOWN"]
            ups = [float(x["percent"]) for x in shots if x["direction"] == "UP"]
            pnls = [float(x["pnl_pct"]) for x in shots]
            vplus_n = sum(1 for x in shots if x["vplus"])
            last = shots[-1]
            ordered = sorted(percents)
            suggest, level_stats = _best_distance(shots, self.distance_levels)
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
                    "vplus": vplus_n,
                    "vplus_rate": round(100.0 * vplus_n / len(shots), 1),
                    "avg_pnl": round(statistics.fmean(pnls), 4) if pnls else 0.0,
                    "levels": level_stats,
                    "last_percent": round(float(last["percent"]), 4),
                    "last_direction": last["direction"],
                    "last_vplus": bool(last["vplus"]),
                    "last_time": _fmt_local(last["peak_ts"], self.tz),
                    "last_ts": last["peak_ts"],
                }
            )
        rows.sort(key=lambda row: (row["vplus_rate"], row["count"], row["avg"]), reverse=True)
        all_pct = [float(x["percent"]) for x in items]
        vplus_n = sum(1 for x in items if x["vplus"])
        return {
            "lookback_min": lookback_min,
            "shots": len(items),
            "pairs": len(rows),
            "avg": round(statistics.fmean(all_pct), 4) if all_pct else 0.0,
            "vplus": vplus_n,
            "vplus_rate": round(100.0 * vplus_n / len(items), 1) if items else 0.0,
            "hold_ms": items[-1]["hold_ms"] if items else 300,
            "rows": rows,
        }

    def write_hints(self, lookback_min: int = 0) -> None:
        payload = self.stats(lookback_min=lookback_min)
        with self.hints_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "symbol",
                    "count",
                    "suggest_distance",
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


def _best_distance(shots: list[dict[str, Any]], levels: list[float]) -> tuple[float, list[dict[str, Any]]]:
    stats: list[dict[str, Any]] = []
    for distance in levels:
        filled = 0
        vplus = 0
        pnls: list[float] = []
        for shot in shots:
            for row in shot.get("distance_report") or []:
                if abs(float(row.get("distance") or 0) - distance) > 1e-6:
                    continue
                if row.get("filled"):
                    filled += 1
                    pnls.append(float(row.get("pnl_pct") or 0))
                    if row.get("vplus"):
                        vplus += 1
                break
        rate = 100.0 * vplus / filled if filled else 0.0
        stats.append(
            {
                "distance": distance,
                "filled": filled,
                "vplus": vplus,
                "vplus_rate": round(rate, 1),
                "avg_pnl": round(statistics.fmean(pnls), 4) if pnls else 0.0,
            }
        )
    ranked = [row for row in stats if row["filled"] > 0]
    if ranked:
        best = max(ranked, key=lambda row: (row["vplus_rate"], row["avg_pnl"], row["distance"]))
        return best["distance"], stats
    percents = sorted(float(x["percent"]) for x in shots)
    return round(_percentile(percents, 50) * 0.85, 2), stats


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _fmt_local(ts_ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%d.%m %H:%M:%S")


def _public_event(item: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
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
