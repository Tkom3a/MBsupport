from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class ExchangeConfig:
    rest: str = "https://www.okx.com"
    ws_public: str = "wss://ws.okx.com:8443/ws/v5/public"
    inst_type: str = "SWAP"
    quote: str = "USDT"


@dataclass
class MarketConfig:
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    refresh_sec: int = 30
    ws_batch_size: int = 80
    symbols_per_connection: int = 180


@dataclass
class FilterConfig:
    qav_24h_min: float = 5_000_000
    qav_24h_max: float = 2_000_000_000
    tick_size_pct_max: float = 0.25
    mark_dev_pct_max: float = 2.0
    min_leverage: float = 20


@dataclass
class ShotConfig:
    windows_ms: list[int] = field(default_factory=lambda: [300, 1000, 3000])
    min_percent: float = 0.80
    min_trades: int = 2
    min_quote_volume: float = 200
    cooldown_ms: int = 2500
    recover_ratio: float = 0.35
    hold_ms: int = 300
    distance_levels: list[float] = field(default_factory=lambda: [1.11, 1.32, 1.42, 1.63, 1.78])
    vplus_min_pnl: float = 0.0


@dataclass
class BtcFilterConfig:
    symbol: str = "BTC-USDT-SWAP"
    window_sec: int = 60
    range_pct: float = 0.6


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 4861
    token: str = ""
    timezone: str = "Europe/Moscow"
    stats_lookback_min: int = 1440


@dataclass
class NotifyConfig:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_min_percent: float = 1.30


@dataclass
class OutputConfig:
    dir: str = "data"
    csv_name: str = "shots.csv"
    jsonl_name: str = "shots.jsonl"
    hints_name: str = "distance_hints.csv"


@dataclass
class AppConfig:
    exchange: ExchangeConfig
    market: MarketConfig
    filters: FilterConfig
    shot: ShotConfig
    btc_filter: BtcFilterConfig
    web: WebConfig
    notify: NotifyConfig
    output: OutputConfig
    raw: dict[str, Any]


def _load_section(cls, data: dict[str, Any]):
    allowed = {k for k in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in allowed})


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _env_filled(name: str) -> bool:
    return bool(_env(name))


def _as_float(name: str, default: float) -> float:
    raw = _env(name)
    return default if raw == "" else float(raw)


def _as_int(name: str, default: int) -> int:
    raw = _env(name)
    return default if raw == "" else int(float(raw))


def _as_csv(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _as_ints(name: str, default: list[int]) -> list[int]:
    parts = _as_csv(name)
    if not parts:
        return default
    return [int(float(part)) for part in parts]


def _as_floats(name: str, default: list[float]) -> list[float]:
    parts = _as_csv(name)
    if not parts:
        return default
    return [float(part) for part in parts]


def load_dotenv_files(root: Path) -> None:
    for candidate in (root / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=True)
            return
    load_dotenv(override=False)


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path).resolve() if path else Path("config.yaml").resolve()
    root = config_path.parent if config_path.exists() else Path.cwd()
    load_dotenv_files(root)

    raw: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    exchange = _load_section(ExchangeConfig, raw.get("exchange") or {})
    market = _load_section(MarketConfig, raw.get("market") or {})
    filters = _load_section(FilterConfig, raw.get("filters") or {})
    shot = _load_section(ShotConfig, raw.get("shot") or {})
    btc_filter = _load_section(BtcFilterConfig, raw.get("btc_filter") or {})
    output = _load_section(OutputConfig, raw.get("output") or {})
    web = _load_section(WebConfig, raw.get("web") or {})
    if not web.host and (raw.get("output") or {}).get("status_host"):
        web.host = str(raw["output"]["status_host"])
    if web.port == 4861 and (raw.get("output") or {}).get("status_port"):
        web.port = int(raw["output"]["status_port"])
    notify = _load_section(NotifyConfig, raw.get("notify") or {})

    if _env_filled("OKX_REST"):
        exchange.rest = _env("OKX_REST")
    if _env_filled("OKX_WS"):
        exchange.ws_public = _env("OKX_WS")
    if _env_filled("INST_TYPE"):
        exchange.inst_type = _env("INST_TYPE").upper()
    if _env_filled("QUOTE"):
        exchange.quote = _env("QUOTE").upper()

    if _env_filled("WHITELIST"):
        market.whitelist = _as_csv("WHITELIST")
    if _env_filled("BLACKLIST"):
        market.blacklist = _as_csv("BLACKLIST")
    if _env_filled("MARKET_REFRESH_SEC"):
        market.refresh_sec = _as_int("MARKET_REFRESH_SEC", market.refresh_sec)

    if _env_filled("QAV_24H_MIN"):
        filters.qav_24h_min = _as_float("QAV_24H_MIN", filters.qav_24h_min)
    if _env_filled("QAV_24H_MAX"):
        filters.qav_24h_max = _as_float("QAV_24H_MAX", filters.qav_24h_max)
    if _env_filled("TICK_SIZE_PCT_MAX"):
        filters.tick_size_pct_max = _as_float("TICK_SIZE_PCT_MAX", filters.tick_size_pct_max)
    if _env_filled("MARK_DEV_PCT_MAX"):
        filters.mark_dev_pct_max = _as_float("MARK_DEV_PCT_MAX", filters.mark_dev_pct_max)
    if _env_filled("MIN_LEVERAGE"):
        filters.min_leverage = _as_float("MIN_LEVERAGE", filters.min_leverage)

    if _env_filled("SHOT_WINDOWS_MS"):
        shot.windows_ms = _as_ints("SHOT_WINDOWS_MS", shot.windows_ms)
    if _env_filled("SHOT_MIN_PERCENT"):
        shot.min_percent = _as_float("SHOT_MIN_PERCENT", shot.min_percent)
    if _env_filled("SHOT_MIN_TRADES"):
        shot.min_trades = _as_int("SHOT_MIN_TRADES", shot.min_trades)
    if _env_filled("SHOT_MIN_QUOTE_VOLUME"):
        shot.min_quote_volume = _as_float("SHOT_MIN_QUOTE_VOLUME", shot.min_quote_volume)
    if _env_filled("SHOT_COOLDOWN_MS"):
        shot.cooldown_ms = _as_int("SHOT_COOLDOWN_MS", shot.cooldown_ms)
    if _env_filled("SHOT_RECOVER_RATIO"):
        shot.recover_ratio = _as_float("SHOT_RECOVER_RATIO", shot.recover_ratio)
    if _env_filled("HOLD_MS"):
        shot.hold_ms = _as_int("HOLD_MS", shot.hold_ms)
    if _env_filled("DISTANCE_LEVELS"):
        shot.distance_levels = _as_floats("DISTANCE_LEVELS", shot.distance_levels)
    if _env_filled("VPLUS_MIN_PNL"):
        shot.vplus_min_pnl = _as_float("VPLUS_MIN_PNL", shot.vplus_min_pnl)

    if _env_filled("BTC_SYMBOL"):
        btc_filter.symbol = norm_symbol(_env("BTC_SYMBOL"))
    if _env_filled("BTC_WINDOW_SEC"):
        btc_filter.window_sec = _as_int("BTC_WINDOW_SEC", btc_filter.window_sec)
    if _env_filled("BTC_RANGE_PCT"):
        btc_filter.range_pct = _as_float("BTC_RANGE_PCT", btc_filter.range_pct)

    if _env_filled("WEB_HOST"):
        web.host = _env("WEB_HOST")
    if _env_filled("WEB_PORT"):
        web.port = _as_int("WEB_PORT", web.port)
    if _env_filled("WEB_TOKEN"):
        web.token = _env("WEB_TOKEN")
    if _env_filled("TZ"):
        web.timezone = _env("TZ")
    if _env_filled("STATS_LOOKBACK_MIN"):
        web.stats_lookback_min = _as_int("STATS_LOOKBACK_MIN", web.stats_lookback_min)

    if _env_filled("TELEGRAM_BOT_TOKEN"):
        notify.telegram_bot_token = _env("TELEGRAM_BOT_TOKEN")
    if _env_filled("TELEGRAM_CHAT_ID"):
        notify.telegram_chat_id = _env("TELEGRAM_CHAT_ID")
    if _env_filled("TELEGRAM_MIN_PERCENT"):
        notify.telegram_min_percent = _as_float("TELEGRAM_MIN_PERCENT", notify.telegram_min_percent)

    return AppConfig(
        exchange=exchange,
        market=market,
        filters=filters,
        shot=shot,
        btc_filter=btc_filter,
        web=web,
        notify=notify,
        output=output,
        raw=raw,
    )


def public_filters(cfg: AppConfig) -> dict[str, Any]:
    return {
        "inst_type": cfg.exchange.inst_type,
        "quote": cfg.exchange.quote,
        "qav_24h_min": cfg.filters.qav_24h_min,
        "qav_24h_max": cfg.filters.qav_24h_max,
        "tick_size_pct_max": cfg.filters.tick_size_pct_max,
        "mark_dev_pct_max": cfg.filters.mark_dev_pct_max,
        "min_leverage": cfg.filters.min_leverage,
        "whitelist": cfg.market.whitelist,
        "blacklist": cfg.market.blacklist,
        "shot_windows_ms": cfg.shot.windows_ms,
        "shot_min_percent": cfg.shot.min_percent,
        "hold_ms": cfg.shot.hold_ms,
        "distance_levels": cfg.shot.distance_levels,
        "vplus_min_pnl": cfg.shot.vplus_min_pnl,
        "btc_window_sec": cfg.btc_filter.window_sec,
        "btc_range_pct": cfg.btc_filter.range_pct,
        "timezone": cfg.web.timezone,
        "stats_lookback_min": cfg.web.stats_lookback_min,
    }


def norm_symbol(value: str) -> str:
    return (value or "").strip().upper().replace("_", "-")
