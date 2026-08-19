from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import AppConfig, FilterConfig, MarketConfig, norm_symbol


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
        if inst.lever and inst.lever < filters.min_leverage:
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
