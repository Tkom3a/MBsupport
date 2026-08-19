from __future__ import annotations

import csv
import json
import logging
import math
import statistics
import time
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
        retain_hours: int = 48,
        tp_min_pct: float = 0.3,
        hold_ms: int = 300,
        suggest_inside_pct: float = 0.05,
    ):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.directory / csv_name
        self.jsonl_path = self.directory / jsonl_name
        self.hints_path = self.directory / hints_name
        self.tz = _zone(tz_name)
        self.distance_levels = distance_levels or [1.11, 1.32, 1.42, 1.63, 1.78]
        self.retain_hours = max(1, int(retain_hours))
        self.tp_min_pct = max(float(tp_min_pct), 0.0)
        self.hold_ms = max(int(hold_ms), 50)
        self.suggest_inside_pct = max(float(suggest_inside_pct), 0.0)
        self.events: deque[dict[str, Any]] = deque(maxlen=50_000)
        self.total = 0
        self._ensure_csv()
        skipped = self._load_existing()
        if skipped:
            self._rewrite_files()
            log.info("Dropped %s shots older than %sh on load", skipped, self.retain_hours)

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

    def _load_existing(self) -> int:
        source = self.jsonl_path if self.jsonl_path.exists() else self.csv_path
        if not source.exists():
            return 0
        loaded = 0
        skipped = 0
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
        return skipped

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
            "path": event.path,
        }
        stored = _row_to_event(row)
        if stored:
            self.events.append(stored)
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
                        "path": event.path,
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

    def recent(self, limit: int = 80, lookback_min: int = 0, direction: str = "", only_btc_calm: bool = False) -> list[dict[str, Any]]:
        items = self._filtered(lookback_min, direction, only_btc_calm)
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
            last = shots[-1]
            ordered = sorted(percents)
            suggest, plus_n, minus_n, win_prob = _recommend_and_score(
                shots, self.tp_min_pct, self.hold_ms, self.suggest_inside_pct
            )
            _, level_stats = _best_distance(shots, self.distance_levels)
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
                    "score_plus": plus_n,
                    "score_minus": minus_n,
                    "score_text": f"{plus_n}/{minus_n}",
                    "win_prob": win_prob,
                    "vplus": plus_n,
                    "vplus_rate": win_prob,
                    "avg_pnl": round(statistics.fmean(pnls), 4) if pnls else 0.0,
                    "levels": level_stats,
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

    def find_shot(self, symbol: str, peak_ts: int) -> dict[str, Any] | None:
        needle = (symbol or "").upper()
        best: dict[str, Any] | None = None
        best_dt = 15_000
        for item in self.events:
            if str(item.get("symbol") or "").upper() != needle:
                continue
            delta = abs(int(item["peak_ts"]) - int(peak_ts))
            if delta < best_dt:
                best_dt = delta
                best = item
        return best

    def as_public(self, item: dict[str, Any]) -> dict[str, Any]:
        return _public_event(item, self.tz)

    def prune(self) -> int:
        cutoff = _cutoff_ms(self.retain_hours)
        kept = [item for item in self.events if item["peak_ts"] >= cutoff]
        dropped = len(self.events) - len(kept)
        if dropped <= 0:
            return 0
        self.events = deque(kept, maxlen=50_000)
        self.total = len(self.events)
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
                            "path": item.get("path") or [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        tmp_jsonl.replace(self.jsonl_path)


def _recommend_and_score(
    shots: list[dict[str, Any]],
    tp_min: float,
    hold_ms: int,
    inside: float,
) -> tuple[float, int, int, float]:
    percents = sorted(float(x["percent"]) for x in shots if float(x.get("percent") or 0) > 0)
    if not percents:
        return 0.0, 0, 0, 0.0
    typical = _percentile(percents, 50)
    suggest = round(max(0.5, typical - inside), 2)
    plus = 0
    minus = 0
    for shot in shots:
        outcome = _outcome_at(shot, suggest, tp_min, hold_ms)
        if outcome == "plus":
            plus += 1
        elif outcome == "minus":
            minus += 1
    total = plus + minus
    prob = round(100.0 * plus / total, 1) if total else 0.0
    return suggest, plus, minus, prob


def _outcome_at(shot: dict[str, Any], distance: float, tp_min: float, hold_ms: int) -> str:
    start = float(shot.get("start_price") or 0)
    pct = float(shot.get("percent") or 0)
    direction = str(shot.get("direction") or "").upper()
    if start <= 0 or distance <= 0 or pct + 1e-9 < distance:
        return "skip"
    fill_px = start * (1.0 - distance / 100.0) if direction == "DOWN" else start * (1.0 + distance / 100.0)
    path = shot.get("path") or []
    fill_ts = 0
    exit_px = fill_px
    for pt in path:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        ts = float(pt[0])
        px = float(pt[1])
        if fill_ts <= 0:
            if direction == "DOWN" and px <= fill_px:
                fill_ts = int(ts)
            elif direction == "UP" and px >= fill_px:
                fill_ts = int(ts)
            continue
        if ts <= fill_ts + hold_ms:
            exit_px = px
        else:
            break
    if fill_ts <= 0:
        if shot.get("vplus"):
            return "plus"
        return "minus"
    if direction == "DOWN":
        pnl = (exit_px - fill_px) / fill_px * 100.0 if fill_px else 0.0
    else:
        pnl = (fill_px - exit_px) / fill_px * 100.0 if fill_px else 0.0
    return "plus" if pnl + 1e-12 >= tp_min else "minus"


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
            "path": _as_path(row.get("path")),
        }
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() in {"1", "true", "True"}
    return bool(value)


def _as_path(value: Any) -> list[list[float]]:
    if isinstance(value, str) and value:
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    out: list[list[float]] = []
    for pt in value:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        try:
            ts, px = float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            continue
        if ts > 0 and px > 0:
            row = [ts, px]
            if len(pt) >= 3:
                try:
                    row.append(float(pt[2]))
                except (TypeError, ValueError):
                    row.append(0.0)
            if len(pt) >= 4:
                try:
                    row.append(float(pt[3]))
                except (TypeError, ValueError):
                    row.append(0.0)
            out.append(row)
    return out


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
