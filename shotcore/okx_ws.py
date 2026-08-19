from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

import websockets

log = logging.getLogger("shotcore.ws")

TradeHandler = Callable[[str, int, float, float, str], None]


class OkxPublicFeed:
    def __init__(
        self,
        url: str,
        on_trade: TradeHandler,
        batch_size: int = 80,
        symbols_per_connection: int = 180,
    ):
        self.url = url
        self.on_trade = on_trade
        self.batch_size = batch_size
        self.symbols_per_connection = symbols_per_connection
        self._desired: set[str] = set()
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.connected = 0

    async def set_symbols(self, symbols: Iterable[str]) -> None:
        async with self._lock:
            self._desired = {s.upper() for s in symbols}
        await self.restart()

    async def restart(self) -> None:
        await self.stop()
        symbols = sorted(self._desired)
        chunks = [
            symbols[i : i + self.symbols_per_connection]
            for i in range(0, len(symbols), self.symbols_per_connection)
        ] or [[]]
        self._tasks = [
            asyncio.create_task(self._run_connection(idx, chunk), name=f"okx-ws-{idx}")
            for idx, chunk in enumerate(chunks)
            if chunk
        ]

    async def stop(self) -> None:
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_connection(self, idx: int, symbols: list[str]) -> None:
        delay = 1.0
        while True:
            try:
                await self._session(idx, symbols)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("WS#%s disconnected: %s", idx, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def _session(self, idx: int, symbols: list[str]) -> None:
        log.info("WS#%s connecting, symbols=%s", idx, len(symbols))
        async with websockets.connect(
            self.url,
            ping_interval=15,
            ping_timeout=20,
            max_size=2**23,
            compression=None,
        ) as ws:
            self.connected += 1
            try:
                for offset in range(0, len(symbols), self.batch_size):
                    batch = symbols[offset : offset + self.batch_size]
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "args": [{"channel": "trades", "instId": inst} for inst in batch],
                            }
                        )
                    )
                    await asyncio.sleep(0.15)
                while True:
                    raw = await ws.recv()
                    if raw == "pong":
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    self._handle_message(raw)
            finally:
                self.connected = max(0, self.connected - 1)

    def _handle_message(self, raw: str) -> None:
        try:
            msg: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return
        event = msg.get("event")
        if event in {"subscribe", "unsubscribe", "error"}:
            if event == "error":
                log.warning("OKX WS error: %s", msg)
            return
        arg = msg.get("arg") or {}
        if arg.get("channel") != "trades":
            return
        for row in msg.get("data") or []:
            inst = row.get("instId") or arg.get("instId")
            if not inst:
                continue
            try:
                ts = int(row.get("ts") or 0)
                px = float(row["px"])
                sz = float(row.get("sz") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            side = str(row.get("side") or "")
            self.on_trade(inst, ts, px, sz, side)
