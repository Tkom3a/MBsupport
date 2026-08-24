from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class TraderConfig:
    host: str = "0.0.0.0"
    port: int = 4863
    timezone: str = "Europe/Moscow"
    shotcore_url: str = "http://127.0.0.1:4861"
    shotcore_token: str = ""
    web_token: str = ""
    lookback_min: int = 180
    poll_sec: int = 60
    run_hours: float = 3.0
    hold_ms: int = 300
    follow_delay_ms: int = 1000
    order_size_usdt: float = 10.0
    order_size_x20: float = 10.0
    order_size_x50: float = 10.0
    leverage: int = 50
    autostop_usd: float = 10.0
    emulate: bool = True
    live_trading: bool = False
    trade_long: bool = True
    trade_short: bool = True
    min_order_distance: float = 0.85
    min_v2_gap: float = 0.3
    v1_offset: float = 0.0
    tp_offset: float = 0.0
    v1_fail_bump: float = 0.15
    pair_lose_limit: int = 3
    pair_lose_window_hours: float = 3.0
    pair_ban_hours: float = 3.0
    min_fills: int = 2
    okx_rest: str = "https://www.okx.com"
    okx_ws: str = "wss://ws.okx.com:8443/ws/v5/public"
    okx_api_key: str = ""
    okx_secret: str = ""
    okx_passphrase: str = ""
    okx_simulated: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    data_dir: str = "data"


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _i(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(float(raw))


def load_trader_config(root: Path | None = None) -> TraderConfig:
    root = root or Path.cwd()
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    emulate = _b("EMULATE", True)
    live = _b("LIVE_TRADING", False)
    if live:
        emulate = False
    return TraderConfig(
        host=os.getenv("TRADER_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_i("TRADER_PORT", 4863),
        timezone=os.getenv("TZ", "Europe/Moscow").strip() or "Europe/Moscow",
        shotcore_url=(os.getenv("SHOTCORE_URL") or "http://127.0.0.1:4861").rstrip("/"),
        shotcore_token=(
            os.getenv("SHOTCORE_TOKEN")
            or os.getenv("WEB_TOKEN")
            or os.getenv("AUTH_API_TOKEN")
            or os.getenv("SESSION_SECRET")
            or ""
        ).strip(),
        web_token=(os.getenv("TRADER_TOKEN") or os.getenv("UI_TOKEN") or "").strip(),
        lookback_min=_i("LOOKBACK_MIN", 180),
        poll_sec=max(15, _i("TRADER_POLL_SEC", 60)),
        run_hours=max(0.1, _f("MT_RUN_HOURS", 3.0)),
        hold_ms=max(50, _i("HOLD_MS", 300)),
        follow_delay_ms=max(200, _i("FOLLOW_DELAY_MS", 1000)),
        order_size_usdt=max(1.0, _f("MARGIN_USDT", 10.0)),
        order_size_x20=max(1.0, _f("MARGIN_USDT_X20", _f("MARGIN_USDT", 10.0))),
        order_size_x50=max(1.0, _f("MARGIN_USDT_X50", _f("MARGIN_USDT", 10.0))),
        leverage=max(1, _i("DEFAULT_LEVERAGE", 50)),
        autostop_usd=max(0.5, _f("AUTOSTOP_USD", 10.0)),
        emulate=emulate,
        live_trading=live and not emulate,
        trade_long=_b("TRADE_LONG", True),
        trade_short=_b("TRADE_SHORT", True),
        min_order_distance=max(0.0, _f("MIN_ORDER_DISTANCE", 0.85)),
        min_v2_gap=max(0.05, _f("MIN_V2_GAP", 0.3)),
        v1_offset=max(-2.0, min(5.0, _f("V1_OFFSET", 0.0))),
        tp_offset=max(0.0, min(2.0, _f("TP_OFFSET", 0.0))),
        v1_fail_bump=max(0.0, min(2.0, _f("V1_FAIL_BUMP", 0.15))),
        pair_lose_limit=max(1, _i("PAIR_LOSE_LIMIT", 3)),
        pair_lose_window_hours=max(0.1, _f("PAIR_LOSE_WINDOW_HOURS", 3.0)),
        pair_ban_hours=max(0.1, _f("PAIR_BAN_HOURS", 3.0)),
        min_fills=max(1, _i("MIN_FILLS", 2)),
        okx_rest=(os.getenv("OKX_REST") or "https://www.okx.com").rstrip("/"),
        okx_ws=os.getenv("OKX_WS") or "wss://ws.okx.com:8443/ws/v5/public",
        okx_api_key=os.getenv("OKX_API_KEY", "").strip(),
        okx_secret=os.getenv("OKX_SECRET_KEY", "").strip() or os.getenv("OKX_API_SECRET", "").strip(),
        okx_passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
        okx_simulated=_b("OKX_SIMULATED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        data_dir=os.getenv("TRADER_DATA_DIR", "data").strip() or "data",
    )
