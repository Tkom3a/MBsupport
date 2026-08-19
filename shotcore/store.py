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
    "would_fill_1_11",
    "would_fill_1_32",
    "would_fill_1_63",
]


class ShotStore:
    def __init__(self, directory: Path, csv_name: str, jsonl_name: str, hints_name: str, tz_name: str = "UTC"):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.directory / csv_name
        self.jsonl_path = self.directory / jsonl_name
        self.hints_path = self.directory / hints_name
        self.tz = _zone(tz_name)
        self.events: deque[dict[str, Any]] = deque(maxlen=50_000)
        self.total = 0
        self._ensure_csv()
        self._load_existing()

    def _ensure_csv(self) -> None:
        if self.csv_path.exists():
            return
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
            "would_fill_1_11": int(event.percent >= 1.11),
            "would_fill_1_32": int(event.percent >= 1.32),
            "would_fill_1_63": int(event.percent >= 1.63),
        }
        stored = _row_to_event(row)
        if stored:
            self.events.append(stored)
        with self.csv_path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_FIELDS).writerow(row)
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "btc_calm": bool(event.btc_calm)}, ensure_ascii=False) + "\n")
        self.total += 1
        log.info(
            "SHOT %s %s %.2f%% window=%sms btc_delta=%.2f calm=%s",
            event.direction,
            event.symbol,
            event.percent,
            event.window_ms,
            event.btc_delta_pct,
            event.btc_calm,
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
            last = shots[-1]
            ordered = sorted(percents)
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
                    "suggest_distance": round(_percentile(ordered, 70), 2),
                    "last_percent": round(float(last["percent"]), 4),
                    "last_direction": last["direction"],
                    "last_time": _fmt_local(last["peak_ts"], self.tz),
                    "last_ts": last["peak_ts"],
                }
            )
        rows.sort(key=lambda row: (row["avg"], row["count"]), reverse=True)
        all_pct = [float(x["percent"]) for x in items]
        return {
            "lookback_min": lookback_min,
            "shots": len(items),
            "pairs": len(rows),
            "avg": round(statistics.fmean(all_pct), 4) if all_pct else 0.0,
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
                    "avg",
                    "avg_down",
                    "avg_up",
                    "p50",
                    "p70",
                    "p80",
                    "p90",
                    "max",
                    "suggest_distance",
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
        "suggest_distance": round(_percentile([item["percent"]], 70), 2),
    }


def _row_to_event(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        symbol = str(row.get("symbol") or "")
        percent = float(row.get("percent") or 0)
        if not symbol or percent <= 0:
            return None
        raw_time = str(row.get("time") or "")
        peak_ts = _parse_ts(raw_time)
        calm = row.get("btc_calm")
        if isinstance(calm, str):
            calm = calm.strip() in {"1", "true", "True"}
        return {
            "peak_ts": peak_ts,
            "symbol": symbol,
            "direction": str(row.get("direction") or "").upper(),
            "percent": percent,
            "window_ms": int(float(row.get("window_ms") or 0)),
            "btc_calm": bool(calm),
        }
    except (TypeError, ValueError):
        return None


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
