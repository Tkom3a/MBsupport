from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("shottrader.okx")


class OkxBroker:
    def __init__(
        self,
        rest: str,
        session: aiohttp.ClientSession,
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        simulated: bool = False,
    ):
        self.rest = rest.rstrip("/")
        self.session = session
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.simulated = simulated
        self.specs: dict[str, dict[str, float]] = {}

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.secret and self.passphrase)

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = time.time()
        stamp = f"{ts:.3f}"
        prehash = f"{stamp}{method.upper()}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": stamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload) if payload else ""
        url = self.rest + path
        headers = self._headers(method, path, body)
        timeout = aiohttp.ClientTimeout(total=12)
        async with self.session.request(method, url, data=body or None, headers=headers, timeout=timeout) as resp:
            data = await resp.json(content_type=None)
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX {path}: {data.get('msg') or data}")
        return data.get("data") or []

    async def load_spec(self, inst_id: str) -> dict[str, float]:
        if inst_id in self.specs:
            return self.specs[inst_id]
        rows = await self._public("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})
        row = (rows or [{}])[0]
        spec = {
            "tick": float(row.get("tickSz") or 0.0001),
            "lot": float(row.get("lotSz") or 1),
            "min_sz": float(row.get("minSz") or 1),
            "ct_val": float(row.get("ctVal") or 1),
            "lever": float(row.get("lever") or 20),
        }
        self.specs[inst_id] = spec
        return spec

    async def _public(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = self.rest + path
        async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            payload = await resp.json(content_type=None)
        if str(payload.get("code")) != "0":
            return []
        return payload.get("data") or []

    def contracts_for(self, inst_id: str, usdt: float, price: float) -> str:
        spec = self.specs.get(inst_id) or {"tick": 0.0001, "lot": 1, "min_sz": 1, "ct_val": 1}
        if price <= 0 or spec["ct_val"] <= 0:
            return str(spec["min_sz"])
        raw = usdt / (price * spec["ct_val"])
        lot = spec["lot"] or 1
        sz = max(spec["min_sz"], round(raw / lot) * lot)
        if abs(sz - int(sz)) < 1e-9:
            return str(int(sz))
        return f"{sz:.8f}".rstrip("0").rstrip(".")

    def round_px(self, inst_id: str, price: float) -> str:
        tick = (self.specs.get(inst_id) or {}).get("tick") or 0.0001
        if tick <= 0:
            return f"{price:.8f}"
        stepped = round(round(price / tick) * tick, 10)
        text = f"{stepped:.10f}".rstrip("0").rstrip(".")
        return text or "0"

    async def set_leverage(self, inst_id: str, lever: int) -> None:
        await self._request(
            "POST",
            "/api/v5/account/set-leverage",
            {"instId": inst_id, "lever": str(int(lever)), "mgnMode": "isolated"},
        )

    async def place_limit(self, inst_id: str, side: str, px: str, sz: str) -> str:
        rows = await self._request(
            "POST",
            "/api/v5/trade/order",
            {
                "instId": inst_id,
                "tdMode": "isolated",
                "side": side,
                "ordType": "limit",
                "px": px,
                "sz": sz,
            },
        )
        return str((rows or [{}])[0].get("ordId") or "")

    async def amend(self, inst_id: str, ord_id: str, px: str) -> None:
        await self._request(
            "POST",
            "/api/v5/trade/amend-order",
            {"instId": inst_id, "ordId": ord_id, "newPx": px},
        )

    async def cancel(self, inst_id: str, ord_id: str) -> None:
        if not ord_id:
            return
        try:
            await self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                {"instId": inst_id, "ordId": ord_id},
            )
        except Exception as exc:
            log.debug("cancel %s: %s", ord_id, exc)

    async def close_market(self, inst_id: str, side: str, sz: str) -> None:
        await self._request(
            "POST",
            "/api/v5/trade/order",
            {
                "instId": inst_id,
                "tdMode": "isolated",
                "side": side,
                "ordType": "market",
                "sz": sz,
                "reduceOnly": True,
            },
        )
