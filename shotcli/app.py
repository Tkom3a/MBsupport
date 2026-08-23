from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .client import ShotClient

ALGO_KEYS = [
    "min_win_pct",
    "min_fills",
    "tp_min_pct",
    "hold_ms",
    "suggest_inside_pct",
    "suggest_inside_max_pct",
    "distance_levels",
    "min_percent",
    "min_trades",
    "min_quote_volume",
    "cooldown_ms",
    "windows_ms",
]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="shotcli",
        description="Терминальный клиент ShotCore + ShotTrader для Ubuntu Server",
    )
    parser.add_argument("--core", default="", help="URL ShotCore, по умолчанию SHOTCORE_URL или :4861")
    parser.add_argument("--trader", default="", help="URL ShotTrader, по умолчанию SHOTTRADER_URL или :4863")
    parser.add_argument("--core-token", default="", help="токен ShotCore (или SHOTCORE_TOKEN)")
    parser.add_argument("--trader-token", default="", help="токен ShotTrader (или TRADER_TOKEN)")
    parser.add_argument("--user", default="", help="логин, если AUTH_MODE=local/ldap")
    parser.add_argument("--password", default="", help="пароль к логину")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("menu", help="интерактивное меню (по умолчанию)")
    sub.add_parser("status", help="состояние терминала и ядра")
    sub.add_parser("orders", help="таблица ордеров один раз")
    sub.add_parser("watch", help="живой экран ордеров (Ctrl+C — выход)")
    sub.add_parser("deals", help="сделки за сутки")
    sub.add_parser("stats", help="сводка ShotTrader (час / день / 7 дней)")
    sub.add_parser("plan", help="план рекомендаций ShotCore")
    sub.add_parser("core", help="статистика разведки ShotCore")
    sub.add_parser("logs", help="последние строки лога терминала")
    sub.add_parser("panic", help="снять все ордера")
    sub.add_parser("resume", help="снять авто-стоп")
    sub.add_parser("algo", help="показать параметры алгоритма")

    p_set = sub.add_parser("set", help="правка настроек терминала")
    p_set.add_argument("key", help="long|short|size20|size50|autostop")
    p_set.add_argument("value", help="on/off или число")

    p_algo = sub.add_parser("algo-set", help="правка алгоритма ShotCore: ключ=значение ...")
    p_algo.add_argument("pairs", nargs="+", help="например min_win_pct=75 hold_ms=300")

    args = parser.parse_args(argv)
    cli = ShotClient(
        core_url=args.core or None,
        trader_url=args.trader or None,
        core_token=args.core_token or None,
        trader_token=args.trader_token or None,
        username=args.user,
        password=args.password,
    )
    cli.ensure_auth()
    cmd = args.cmd or "menu"
    try:
        if cmd == "menu":
            run_menu(cli)
        elif cmd == "status":
            print_status(cli)
        elif cmd == "orders":
            print_orders(cli)
        elif cmd == "watch":
            watch_orders(cli)
        elif cmd == "deals":
            print_deals(cli)
        elif cmd == "stats":
            print_stats(cli)
        elif cmd == "plan":
            print_plan(cli)
        elif cmd == "core":
            print_core(cli)
        elif cmd == "logs":
            print_logs(cli)
        elif cmd == "panic":
            cli.trader_post("/api/panic", {})
            print("panic: все ордера сняты")
        elif cmd == "resume":
            cli.trader_post("/api/resume", {})
            print("авто-стоп снят, жду план")
        elif cmd == "algo":
            print_algo(cli)
        elif cmd == "set":
            apply_set(cli, args.key, args.value)
        elif cmd == "algo-set":
            apply_algo_set(cli, args.pairs)
        else:
            parser.print_help()
    except KeyboardInterrupt:
        print()
    except Exception as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        sys.exit(1)


def run_menu(cli: ShotClient) -> None:
    actions = {
        "1": lambda: print_status(cli),
        "2": lambda: watch_orders(cli),
        "3": lambda: print_orders(cli),
        "4": lambda: print_deals(cli),
        "5": lambda: print_stats(cli),
        "6": lambda: print_plan(cli),
        "7": lambda: print_core(cli),
        "8": lambda: menu_directions(cli),
        "9": lambda: menu_sizes(cli),
        "a": lambda: menu_algo(cli),
        "l": lambda: print_logs(cli),
        "p": lambda: (cli.trader_post("/api/panic", {}), print("panic отправлен")),
        "r": lambda: (cli.trader_post("/api/resume", {}), print("авто-стоп снят")),
    }
    while True:
        ping = cli.ping()
        print()
        print("═ Shot CLI ════════════════════════════════")
        print(f"  ядро     {cli.core_url}  [{ping['core']}]")
        print(f"  терминал {cli.trader_url}  [{ping['trader']}]")
        print("───────────────────────────────────────────")
        print("  1  состояние")
        print("  2  ордера онлайн (watch)")
        print("  3  ордера один раз")
        print("  4  сделки за сутки")
        print("  5  статистика / отчёт 7 дней")
        print("  6  план ShotCore")
        print("  7  разведка ShotCore")
        print("  8  направление long/short")
        print("  9  размеры и авто-стоп")
        print("  a  алгоритм ShotCore")
        print("  l  лог терминала")
        print("  p  panic    r  снять авто-стоп")
        print("  0  выход")
        choice = input("команда: ").strip().lower()
        if choice in {"0", "q", "quit", "exit"}:
            return
        action = actions.get(choice)
        if not action:
            print("неизвестная команда")
            continue
        try:
            action()
        except KeyboardInterrupt:
            print()
        except Exception as exc:
            print(f"ошибка: {exc}")


def print_status(cli: ShotClient) -> None:
    ping = cli.ping()
    state = cli.trader_get("/api/state")
    print("\n".join(_status_lines(cli, state, ping)))


def print_orders(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    print("\n".join(_order_lines(state)))


def watch_orders(cli: ShotClient) -> None:
    """Цифры переписываются на месте, экран не мигает."""
    _cursor(False)
    ping = {"core": "…", "trader": "…"}
    tick = 0
    try:
        while True:
            if tick % 15 == 0:
                try:
                    ping = cli.ping()
                except Exception:
                    ping = {"core": "?", "trader": "?"}
            try:
                state = cli.trader_get("/api/state")
                lines = [
                    f"{time.strftime('%H:%M:%S')}  ордера онлайн  Ctrl+C выход",
                    *_status_lines(cli, state, ping),
                    "",
                    *_order_lines(state),
                ]
            except Exception as exc:
                lines = [
                    f"{time.strftime('%H:%M:%S')}  ордера онлайн  Ctrl+C выход",
                    f"ошибка: {exc}",
                ]
            _paint(lines)
            tick += 1
            time.sleep(1.0)
    finally:
        _cursor(True)
        print()


def _status_lines(cli: ShotClient, state: dict[str, Any], ping: dict[str, str]) -> list[str]:
    mode = "эмуляция" if state.get("emulate") else "LIVE"
    halt = f"  АВТО-СТОП: {state.get('halt_reason')}" if state.get("halted") else ""
    hour = state.get("hour") or {}
    today = state.get("today") or state.get("day") or {}
    lines = [
        f"ShotCore   {cli.core_url}  {ping.get('core', '')}",
        f"ShotTrader {cli.trader_url}  {ping.get('trader', '')}",
        f"режим {mode}{halt}",
        (
            f"направление  LONG={'on' if state.get('trade_long', True) else 'off'}  "
            f"SHORT={'on' if state.get('trade_short', True) else 'off'}"
        ),
        (
            f"size x20={state.get('order_size_x20')}  x50={state.get('order_size_x50')}  "
            f"autostop={state.get('autostop_usd')}$"
        ),
        (
            f"час    сделок {hour.get('trades', 0)}  "
            f"+{hour.get('plus', 0)}/−{hour.get('minus', 0)}  PnL {_money(hour.get('pnl_usd'))}"
        ),
        (
            f"сутки  сделок {today.get('trades', 0)}  "
            f"+{today.get('plus', 0)}/−{today.get('minus', 0)}  PnL {_money(today.get('pnl_usd'))}"
        ),
        (
            f"нереализ. {_money(state.get('unrealized'))}  "
            f"в сделках {float(state.get('in_trade') or 0):.2f}$  "
            f"заморожено {float(state.get('frozen_margin_usdt') or 0):.2f} USDT"
        ),
        f"клонов {len(state.get('markets') or [])}  план {len(state.get('plan') or [])} пар",
    ]
    if state.get("plan_error"):
        lines.append(f"план: {state['plan_error']}")
    return lines


def _order_lines(state: dict[str, Any]) -> list[str]:
    rows = state.get("markets") or []
    if not rows:
        return [state.get("plan_error") or "нет клонов"]
    lines = [
        (
            f"{'пара':<10} {'цена':>10} {'BUY':>10} {'D':>6} {'V2':>6} "
            f"{'SELL':>10} {'D':>6} {'V2':>6} {'сост':>8} {'V2':>6} {'PnL':>9}"
        )
    ]
    for row in rows:
        lines.append(
            f"{_base(row.get('symbol')):<10} "
            f"{_px(row.get('last')):>10} "
            f"{_px(row.get('buy')):>10} "
            f"{_d(row.get('buy_distance')):>6} "
            f"{_d(row.get('buy_v2_distance')):>6} "
            f"{_px(row.get('sell')):>10} "
            f"{_d(row.get('sell_distance')):>6} "
            f"{_d(row.get('sell_v2_distance')):>6} "
            f"{_st(row):>8} "
            f"{_st_v2(row):>6} "
            f"{_money(row.get('unrealized') if row.get('state') == 'pos' else 0):>9}"
        )
    lines.append(
        f"заморожено {float(state.get('frozen_margin_usdt') or 0):.2f} USDT  "
        f"LONG={'on' if state.get('trade_long', True) else 'off'}  "
        f"SHORT={'on' if state.get('trade_short', True) else 'off'}"
    )
    return lines


def print_deals(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    rows = list(state.get("journal") or [])
    if not rows:
        print("нет сделок за сутки")
        return
    pnl = sum(float(r.get("pnl_usd") or 0) for r in rows)
    plus = sum(1 for r in rows if float(r.get("pnl_usd") or 0) > 0)
    print(f"сделок {len(rows)}  +{plus}/−{len(rows) - plus}  {_money(pnl)}")
    print(f"{'время':<14} {'пара':<8} {'стор':<5} {'D':>6} {'слой':<4} {'ход%':>8} {'PnL':>9}")
    for row in reversed(rows[-80:]):
        ts = int(row.get("ts") or 0)
        stamp = time.strftime("%d.%m %H:%M:%S", time.localtime(ts / 1000)) if ts else "—"
        print(
            f"{stamp:<14} {_base(row.get('symbol')):<8} "
            f"{str(row.get('side') or '').upper():<5} "
            f"{_d(row.get('distance')):>6} "
            f"{str(row.get('layer') or 'v1'):<4} "
            f"{_signed(row.get('pnl_pct')):>8} "
            f"{_money(row.get('pnl_usd')):>9}"
        )


def print_stats(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    print_status(cli)
    print()
    print("отчёт 7 дней")
    days = (state.get("reports") or {}).get("days") or []
    for day in days:
        print(
            f"  {day.get('label') or day.get('date')}: "
            f"{day.get('trades')} сделок  +{day.get('plus')}/−{day.get('minus')}  "
            f"{_money(day.get('pnl_usd'))}"
        )


def print_plan(cli: ShotClient) -> None:
    try:
        plan = cli.core_get("/api/mt-plan")
    except Exception:
        state = cli.trader_get("/api/state")
        plan = {"pairs": state.get("plan") or [], "updated_utc": ""}
    pairs = plan.get("pairs") or []
    print(f"план {plan.get('updated_utc') or ''}  пар {len(pairs)}")
    print(f"{'пара':<12} {'BUY':>6} {'V2':>6} {'SHORT':>6} {'V2':>6} {'+':>5}")
    for row in pairs:
        print(
            f"{_base(row.get('symbol')):<12} "
            f"{_d(row.get('buy_pct')):>6} {_d(row.get('buy_v2_pct')):>6} "
            f"{_d(row.get('sell_pct')):>6} {_d(row.get('sell_v2_pct')):>6} "
            f"{_num(row.get('win_prob'), 0):>4}%"
        )


def print_core(cli: ShotClient) -> None:
    stats = cli.core_get("/api/stats")
    print(
        f"пар {stats.get('pairs')}  прострелов {stats.get('shots')}  "
        f"смотрим {stats.get('symbols_watched')}"
    )
    print(
        f"среднее LONG {_d(stats.get('avg_buy_pct'))}  ({stats.get('rec_buy_n') or 0} пар)  "
        f"SHORT {_d(stats.get('avg_sell_pct'))}  ({stats.get('rec_sell_n') or 0} пар)"
    )
    print(f"{'пара':<14} {'BUY':>6} {'V2':>6} {'SHORT':>6} {'V2':>6} {'вер':>5}")
    for row in (stats.get("rows") or [])[:40]:
        if float(row.get("buy_pct") or 0) <= 0 and float(row.get("sell_pct") or 0) <= 0:
            continue
        print(
            f"{_base(row.get('symbol')):<14} "
            f"{_d(row.get('buy_pct')):>6} {_d(row.get('buy_v2_pct')):>6} "
            f"{_d(row.get('sell_pct')):>6} {_d(row.get('sell_v2_pct')):>6} "
            f"{_num(row.get('win_prob'), 0):>4}%"
        )


def print_logs(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    lines = list(state.get("log") or [])
    if not lines:
        print("лог пуст")
        return
    for line in lines[-40:]:
        print(line)


def print_algo(cli: ShotClient) -> None:
    algo = cli.core_get("/api/algo")
    print("алгоритм ShotCore (горячие правки без рестарта)")
    for key in ALGO_KEYS:
        if key in algo:
            print(f"  {key:24} {algo[key]}")


def apply_set(cli: ShotClient, key: str, value: str) -> None:
    key = key.strip().lower()
    mapping = {
        "long": "trade_long",
        "short": "trade_short",
        "size20": "order_size_x20",
        "x20": "order_size_x20",
        "size50": "order_size_x50",
        "x50": "order_size_x50",
        "autostop": "autostop_usd",
        "stop": "autostop_usd",
        "core": "shotcore_url",
        "shotcore": "shotcore_url",
    }
    field = mapping.get(key)
    if not field:
        raise RuntimeError(f"неизвестный ключ {key}. доступны: long, short, size20, size50, autostop, core")
    if field == "shotcore_url":
        res = cli.trader_post("/api/shotcore", {"url": value})
        print(f"ok  ShotCore URL → {res.get('shotcore_url')}")
        return
    payload: dict[str, Any]
    if field in {"trade_long", "trade_short"}:
        payload = {field: _as_bool(value)}
    else:
        payload = {field: float(value)}
    res = cli.trader_post("/api/settings", payload)
    print(
        f"ok  long={'on' if res.get('trade_long', True) else 'off'}  "
        f"short={'on' if res.get('trade_short', True) else 'off'}  "
        f"x20={res.get('order_size_x20')}  x50={res.get('order_size_x50')}  "
        f"autostop={res.get('autostop_usd')}"
    )


def apply_algo_set(cli: ShotClient, pairs: list[str]) -> None:
    payload: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise RuntimeError(f"нужно ключ=значение, получено {item}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if key not in ALGO_KEYS:
            raise RuntimeError(f"неизвестный параметр {key}. доступны: {', '.join(ALGO_KEYS)}")
        payload[key] = _parse_algo_value(key, raw)
    res = cli.core_post("/api/algo", payload)
    print("алгоритм обновлён")
    for key in payload:
        print(f"  {key} = {res.get(key)}")


def menu_directions(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    print(
        f"сейчас LONG={'on' if state.get('trade_long', True) else 'off'}  "
        f"SHORT={'on' if state.get('trade_short', True) else 'off'}"
    )
    raw = input("вкл/выкл long? [on/off/enter=не менять]: ").strip()
    payload: dict[str, Any] = {}
    if raw:
        payload["trade_long"] = _as_bool(raw)
    raw = input("вкл/выкл short? [on/off/enter=не менять]: ").strip()
    if raw:
        payload["trade_short"] = _as_bool(raw)
    if not payload:
        print("без изменений")
        return
    res = cli.trader_post("/api/settings", payload)
    print(f"LONG={'on' if res.get('trade_long') else 'off'}  SHORT={'on' if res.get('trade_short') else 'off'}")


def menu_sizes(cli: ShotClient) -> None:
    state = cli.trader_get("/api/state")
    print(f"x20={state.get('order_size_x20')}  x50={state.get('order_size_x50')}  autostop={state.get('autostop_usd')}")
    payload: dict[str, Any] = {}
    raw = input("size x20 [enter=не менять]: ").strip()
    if raw:
        payload["order_size_x20"] = float(raw)
    raw = input("size x50 [enter=не менять]: ").strip()
    if raw:
        payload["order_size_x50"] = float(raw)
    raw = input("авто-стоп $ [enter=не менять]: ").strip()
    if raw:
        payload["autostop_usd"] = float(raw)
    if not payload:
        print("без изменений")
        return
    res = cli.trader_post("/api/settings", payload)
    print(f"x20={res.get('order_size_x20')}  x50={res.get('order_size_x50')}  autostop={res.get('autostop_usd')}")


def menu_algo(cli: ShotClient) -> None:
    print_algo(cli)
    print("правка: ключ=значение  (пусто — назад). несколько через пробел.")
    print("пример: min_win_pct=75 hold_ms=300")
    raw = input("algo-set: ").strip()
    if not raw:
        return
    apply_algo_set(cli, raw.split())


def _parse_algo_value(key: str, raw: str) -> Any:
    raw = raw.strip()
    if key in {"distance_levels", "windows_ms"}:
        return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if key in {"min_fills", "hold_ms", "min_trades", "cooldown_ms"}:
        return int(float(raw))
    return float(raw)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да", "вкл"}


def _base(symbol: Any) -> str:
    return str(symbol or "").split("-")[0] or "—"


def _px(value: Any) -> str:
    n = float(value or 0)
    if n <= 0:
        return "—"
    if n >= 100:
        return f"{n:.2f}"
    if n >= 1:
        return f"{n:.4f}"
    return f"{n:.6f}"


def _d(value: Any) -> str:
    n = float(value or 0)
    return f"{n:.2f}" if n > 0 else "—"


def _num(value: Any, digits: int = 2) -> str:
    return f"{float(value or 0):.{digits}f}"


def _signed(value: Any) -> str:
    n = float(value or 0)
    return f"{n:+.3f}"


def _money(value: Any) -> str:
    n = float(value or 0)
    return f"{n:+.2f}$"


def _st(row: dict[str, Any]) -> str:
    if row.get("state") == "pos":
        return str(row.get("side") or "pos")
    return str(row.get("state") or "hunt")


def _st_v2(row: dict[str, Any]) -> str:
    if row.get("v2_state") == "pos":
        return str(row.get("v2_side") or "pos")
    if float(row.get("buy_v2_distance") or 0) > 0 or float(row.get("sell_v2_distance") or 0) > 0:
        return str(row.get("v2_state") or "hunt")
    return "—"


def _cursor(visible: bool) -> None:
    sys.stdout.write("\033[?25h" if visible else "\033[?25l")
    sys.stdout.flush()


def _paint(lines: list[str]) -> None:
    """Перерисовать кадр без cls: курсор вверх, строки на месте, хвост стереть."""
    out = ["\033[H"]
    for line in lines:
        out.append(line.replace("\n", " ").replace("\r", ""))
        out.append("\033[K\n")
    out.append("\033[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()

