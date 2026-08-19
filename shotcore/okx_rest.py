from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import AppConfig, FilterConfig, MarketConfig, norm_symbol

log = logging.getLogger("shotcore.okx")


@dataclass
class Instrument:
    inst_id: str
    tick_sz: float
    lever: float
    last: float = 0.0
    mark: float = 0.0
    qav_24h: float = 0.0


class OkxRest:
    def __init__(self, cfg: AppConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        url = self.cfg.exchange.rest.rstrip("/") + path
        async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX {path} error: {payload}")
        return payload.get("data") or []

    async def fetch_universe(self) -> list[Instrument]:
        inst_type = self.cfg.exchange.inst_type
        quote = self.cfg.exchange.quote.upper()
        instruments = await self._get("/api/v5/public/instruments", {"instType": inst_type})
        tickers = await self._get("/api/v5/market/tickers", {"instType": inst_type})
        ticker_by_id = {row["instId"]: row for row in tickers}

        out: list[Instrument] = []
        for row in instruments:
            if row.get("state") != "live":
                continue
            inst_id = row.get("instId") or ""
            if not inst_id.endswith(f"-{quote}-SWAP") and not (
                inst_type == "SPOT" and inst_id.endswith(f"-{quote}")
            ):
                continue
            ticker = ticker_by_id.get(inst_id, {})
            last = _f(ticker.get("last") or row.get("last"))
            mark = _f(ticker.get("markPx"))
            qav = _f(ticker.get("volCcy24h") if inst_type == "SPOT" else ticker.get("volCcyQuote24h"))
            if qav <= 0:
                qav = _f(ticker.get("volCcy24h")) * last if last else 0.0
            out.append(
                Instrument(
                    inst_id=inst_id,
                    tick_sz=_f(row.get("tickSz")),
                    lever=_f(row.get("lever")),
                    last=last,
                    mark=mark,
                    qav_24h=qav,
                )
            )
        return out

    async def fetch_candles(
        self,
        inst_id: str,
        bar: str = "15m",
        limit: int = 4,
        after: int | None = None,
        before: int | None = None,
        history: bool = False,
    ) -> list[Any]:
        path = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        url = self.cfg.exchange.rest.rstrip("/") + path
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after:
            params["after"] = str(int(after))
        if before:
            params["before"] = str(int(before))
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(0.35 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    payload = await resp.json()
                if str(payload.get("code")) != "0":
                    log.debug("OKX candles %s %s: %s", inst_id, bar, payload.get("msg") or payload)
                    return []
                return payload.get("data") or []
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.2 * (attempt + 1))
        if last_error:
            log.debug("candles %s failed: %s", inst_id, last_error)
        return []

    async def fetch_candles_around(self, inst_id: str, ts_ms: int) -> tuple[str, list[dict[str, Any]]]:
        """One cheap window around a shot. Prefer 1s; fall back to 1m. No background work."""
        now = int(time.time() * 1000)
        age = now - ts_ms
        specs = (
            ("1s", 100, 80_000, 25_000),
            ("1m", 80, 50 * 60_000, 15 * 60_000),
        )
        for bar, limit, before_ms, after_ms in specs:
            history = age > before_ms + after_ms + 5_000
            after = ts_ms + after_ms
            if age < 90_000 and bar == "1s":
                rows = await self.fetch_candles(inst_id, bar=bar, limit=limit, history=False)
            else:
                rows = await self.fetch_candles(inst_id, bar=bar, limit=limit, after=after, history=history)
                if not rows and history:
                    rows = await self.fetch_candles(inst_id, bar=bar, limit=limit, after=after, history=False)
            parsed = parse_okx_candles(rows)
            lo, hi = ts_ms - before_ms, ts_ms + after_ms
            windowed = [row for row in parsed if lo <= row["ts"] <= hi]
            chosen = windowed or parsed
            if len(chosen) >= 3:
                return bar, chosen
        return "1s", []


def parse_okx_candles(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            item = {
                "ts": int(float(row[0])),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "vol": float(row[5]) if len(row) > 5 else 0.0,
                "vol_ccy": float(row[6]) if len(row) > 6 else 0.0,
                "vol_quote": float(row[7]) if len(row) > 7 else 0.0,
            }
        except (TypeError, ValueError, IndexError):
            continue
        if item["h"] < item["l"] or item["o"] <= 0:
            continue
        if item["vol_quote"] <= 0 and item["vol_ccy"] > 0:
            item["vol_quote"] = item["vol_ccy"] * item["c"]
        out.append(item)
    out.sort(key=lambda row: row["ts"])
    return out


def apply_market_filters(
    instruments: list[Instrument],
    market: MarketConfig,
    filters: FilterConfig,
) -> list[Instrument]:
    whitelist = {norm_symbol(x) for x in market.whitelist if x}
    blacklist = {norm_symbol(x) for x in market.blacklist if x}
    selected: list[Instrument] = []
    for inst in instruments:
        key = norm_symbol(inst.inst_id)
        if whitelist and key not in whitelist:
            continue
        if key in blacklist:
            continue
        if not (filters.qav_24h_min <= inst.qav_24h <= filters.qav_24h_max):
            continue
        if inst.lever < filters.min_leverage or inst.lever > filters.max_leverage:
            continue
        if inst.last > 0 and inst.tick_sz > 0:
            tick_pct = inst.tick_sz / inst.last * 100.0
            if tick_pct > filters.tick_size_pct_max:
                continue
        if inst.last > 0 and inst.mark > 0:
            mark_dev = abs(inst.mark - inst.last) / inst.last * 100.0
            if mark_dev > filters.mark_dev_pct_max:
                continue
        selected.append(inst)
    selected.sort(key=lambda x: x.qav_24h, reverse=True)
    return selected


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
