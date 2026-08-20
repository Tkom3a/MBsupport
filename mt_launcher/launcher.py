#!/usr/bin/env python3
"""Забирает план ShotCore по API и поднимает Shot Group в MoonTrader.

Читает /api/mt-plan, создаёт папку MBsupport в algorithms.config (если её нет),
копирует ваш рабочий Shot Group: для каждой пары — мастер (не запущен) и клон
на run_hours (по умолчанию 3 ч). В Order size пишется 10 USDT уже с учётом
плеча (как в справке Shot). Время жизни клона считает само ядро MT.

Запуск на сервере, где лежит MTCore / algorithms.config.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PREFIX_DEFAULT = "SC"
FOLDER_DEFAULT = "MBsupport"
STATE_PATH = ROOT / "state.json"
_ID_SEQ = 0

CLONE_LIFETIME_KEYS = (
    "cloneLifeTime",
    "cloneLifetime",
    "lifeTime",
    "lifetime",
    "cloneDuration",
    "workTime",
    "cloneWorkTime",
)
CLONE_PARENT_KEYS = (
    "parentId",
    "parentID",
    "cloneParentId",
    "cloneParentID",
    "sourceId",
    "sourceID",
    "originalId",
    "clonedFromId",
    "clonedFrom",
)


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = env(name)
    return default if raw == "" else float(raw)


def env_int(name: str, default: int) -> int:
    raw = env(name)
    return default if raw == "" else int(float(raw))


def log(msg: str) -> None:
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def new_id() -> int:
    global _ID_SEQ
    _ID_SEQ += 1
    return int(time.time() * 1000) + _ID_SEQ


def default_algos_path() -> Path:
    explicit = env("MT_ALGOS_PATH")
    if explicit:
        return Path(explicit).expanduser()
    profile = env("MT_PROFILE", "okxoma")
    linux = Path.home() / ".config" / "moontrader-data" / "data" / profile / "algorithms.config"
    win = Path.home() / "AppData" / "Roaming" / "moontrader-data" / "data" / profile / "algorithms.config"
    if linux.is_file():
        return linux
    if win.is_file():
        return win
    return linux


def fetch_plan(url: str, token: str) -> dict[str, Any]:
    base = url.rstrip("/")
    req_url = base + "/api/mt-plan"
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Shot-Token"] = token
        req_url += ("&" if "?" in req_url else "?") + "token=" + token
    req = Request(req_url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise SystemExit(f"ShotCore API {exc.code}: {exc.reason}. Проверьте SHOTCORE_URL и SHOTCORE_TOKEN.") from exc
    except URLError as exc:
        raise SystemExit(f"Нет связи с ShotCore {base}: {exc.reason}") from exc
    if not isinstance(payload, dict) or "pairs" not in payload:
        raise SystemExit("Ответ /api/mt-plan не похож на план ShotCore.")
    return payload


def mt_symbol(symbol: str) -> str:
    return str(symbol or "").strip().lower().replace("_", "-")


def select_pairs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    subscribed_only = env_bool("SUBSCRIBED_ONLY", True)
    skip_btc = env_bool("SKIP_BTC", True)
    limit = max(1, env_int("MT_MAX_PAIRS", 25))
    out: list[dict[str, Any]] = []
    for pair in plan.get("pairs") or []:
        symbol = str(pair.get("symbol") or "")
        if not symbol or float(pair.get("recommend_pct") or 0) <= 0:
            continue
        if skip_btc and symbol.upper().startswith("BTC-"):
            continue
        if subscribed_only and not pair.get("subscribed"):
            continue
        out.append(pair)
        if len(out) >= limit:
            break
    return out


def load_config(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    data = json.loads(text)
    return data, encoding


def save_config(path: Path, data: Any, encoding: str) -> None:
    backup = path.with_name(path.name + datetime.now().strftime(".bak-%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    log(f"backup {backup.name}")
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding=encoding)
    tmp.replace(path)


def as_list_container(data: Any) -> tuple[list[Any], str | None, Any]:
    """Возвращает (список алгоритмов, ключ в dict или None, корень)."""
    if isinstance(data, list):
        return data, None, data
    if not isinstance(data, dict):
        raise SystemExit("algorithms.config: неизвестный формат (не JSON object/array).")
    for key in ("configs", "algorithms", "Algorithms", "items", "Items", "algos", "Algos"):
        if isinstance(data.get(key), list):
            return data[key], key, data
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict) and (
            "args" in value[0] or "params" in value[0] or "signature" in value[0] or "groupID" in value[0]
        ):
            return value, key, data
    raise SystemExit(
        "Не нашёл список алгоритмов в algorithms.config. Запустите: python launcher.py inspect"
    )


def groups_of(root: Any) -> tuple[list[Any] | None, str | None]:
    if not isinstance(root, dict):
        return None, None
    for key in ("groups", "Groups", "folders", "Folders", "algorithmGroups"):
        if isinstance(root.get(key), list):
            return root[key], key
    return None, None


def group_name(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("name", "Name", "info", "Info", "title", "Title"):
        if item.get(key):
            return str(item[key])
    return ""


def group_id(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    for key in ("id", "ID", "Id", "groupID", "GroupID"):
        if item.get(key) not in (None, ""):
            try:
                return int(item[key])
            except (TypeError, ValueError):
                continue
    return 0


def ensure_folder(root: Any, folder: str) -> int:
    groups, _key = groups_of(root)
    if groups is None:
        return 8800000000001
    for item in groups:
        if group_name(item) == folder:
            gid = group_id(item)
            log(f"папка «{folder}» уже есть, id={gid}")
            return gid or new_id()
    gid = new_id()
    groups.append({"id": gid, "name": folder, "groupType": 1, "actionType": 13})
    log(f"создана папка алгоритмов «{folder}», id={gid}")
    return gid


def is_wrapped_arg(slot: Any) -> bool:
    return isinstance(slot, dict) and "value" in slot and ("name" in slot or "argumentType" in slot)


def arguments(algo: dict[str, Any]) -> dict[str, Any]:
    args = algo.get("args")
    if isinstance(args, dict) and isinstance(args.get("Arguments"), dict):
        return args["Arguments"]
    params = algo.get("params")
    if isinstance(params, dict):
        return params
    if isinstance(args, dict) and args and not any(k in args for k in ("Arguments", "arguments")):
        return args
    algo.setdefault("params", {})
    if not isinstance(algo["params"], dict):
        algo["params"] = {}
    return algo["params"]


def arg_key(args: dict[str, Any], name: str) -> str | None:
    if name in args:
        return name
    low = name.lower()
    for key in args:
        if str(key).lower() == low:
            return str(key)
    return None


def unwrap_arg(slot: Any) -> Any:
    if is_wrapped_arg(slot):
        return slot.get("value")
    return slot


def get_arg(algo: dict[str, Any], name: str) -> Any:
    args = arguments(algo)
    key = arg_key(args, name)
    if key is None:
        return None
    return unwrap_arg(args[key])


def set_arg(algo: dict[str, Any], name: str, value: Any, merge: bool = False) -> bool:
    args = arguments(algo)
    key = arg_key(args, name)
    if key is None:
        return False
    slot = args[key]
    if is_wrapped_arg(slot):
        current = slot.get("value")
        if merge and isinstance(value, dict) and isinstance(current, dict):
            current.update(value)
        else:
            slot["value"] = value
        return True
    if merge and isinstance(value, dict) and isinstance(slot, dict):
        slot.update(value)
        return True
    args[key] = value
    return True


def algo_title(algo: dict[str, Any]) -> str:
    for src in (get_arg(algo, "info"), get_arg(algo, "namingRule"), algo.get("info")):
        if src:
            return str(src)
    return ""


def find_template(algos: list[Any], name: str) -> dict[str, Any]:
    wanted = name.strip().lower()
    prefix = env("ALGO_PREFIX", PREFIX_DEFAULT)
    shots = [
        a
        for a in algos
        if isinstance(a, dict)
        and str(a.get("signature") or "").upper() == "SG"
        and not a.get("isClone")
        and not our_algo(a, prefix)
    ]
    pool = shots or [
        a
        for a in algos
        if isinstance(a, dict) and not a.get("isClone") and not our_algo(a, prefix)
    ]
    if wanted:
        for algo in pool:
            info = algo_title(algo).lower()
            rule = str(get_arg(algo, "namingRule") or "").lower()
            if wanted in info or wanted in rule:
                return algo
        raise SystemExit(f"Шаблон «{name}» не найден среди Shot Group.")
    if pool:
        return pool[0]
    raise SystemExit("В algorithms.config нет Shot Group, который можно клонировать. Создайте один Shot Group вручную.")


def walk_set_numeric(obj: Any, names: set[str], value: Any) -> bool:
    """Пишет value в слот с таким именем, не ломая args.Arguments обёртку."""
    found = False
    if isinstance(obj, dict):
        slot_name = str(obj.get("name") or "").lower()
        if is_wrapped_arg(obj) and slot_name in names:
            current = obj.get("value")
            if isinstance(current, dict):
                for inner in ("percent", "percentage", "value", "orderSize", "size"):
                    if inner in current and isinstance(current[inner], (int, float)):
                        current[inner] = value
                        found = True
            elif isinstance(current, (int, float, str)) or current is None:
                obj["value"] = value
                found = True
            return found
        for key, val in obj.items():
            if str(key).lower() in names:
                if is_wrapped_arg(val):
                    if walk_set_numeric(val, names, value):
                        found = True
                elif isinstance(val, dict) and any(k in val for k in ("percent", "percentage", "min", "max")):
                    for inner in ("percent", "percentage", "value"):
                        if inner in val and isinstance(val[inner], (int, float)):
                            val[inner] = value
                            found = True
                    if not found:
                        obj[key] = value
                        found = True
                elif isinstance(val, (int, float, str)) or val is None:
                    obj[key] = value
                    found = True
                elif walk_set_numeric(val, names, value):
                    found = True
            elif walk_set_numeric(val, names, value):
                found = True
    elif isinstance(obj, list):
        for item in obj:
            if walk_set_numeric(item, names, value):
                found = True
    return found


def set_minmax(obj: Any, names: set[str], low: float, high: float) -> bool:
    found = False
    if isinstance(obj, dict):
        if is_wrapped_arg(obj) and str(obj.get("name") or "").lower() in names:
            val = obj.get("value")
            if isinstance(val, dict) and ("min" in val or "max" in val or "toggle" in val):
                val["toggle"] = True
                if "min" in val:
                    val["min"] = low
                if "max" in val:
                    val["max"] = high
                return True
        for key, val in obj.items():
            target = unwrap_arg(val) if is_wrapped_arg(val) else val
            if str(key).lower() in names and isinstance(target, dict) and ("min" in target or "max" in target or "toggle" in target):
                target["toggle"] = True
                if "min" in target:
                    target["min"] = low
                if "max" in target:
                    target["max"] = high
                found = True
            elif set_minmax(val, names, low, high):
                found = True
    elif isinstance(obj, list):
        for item in obj:
            if set_minmax(item, names, low, high):
                found = True
    return found


def set_whitelist(algo: dict[str, Any], symbol: str) -> None:
    current = get_arg(algo, "whiteList")
    if isinstance(current, dict) and ("all" in current or "sample" in current or "count" in current):
        payload = dict(current)
        payload["all"] = [symbol]
        payload["sample"] = [symbol]
        payload["count"] = 1
        set_arg(algo, "whiteList", payload)
        return
    if isinstance(current, list):
        set_arg(algo, "whiteList", [symbol])
        return
    if isinstance(current, str) or current is None:
        if not set_arg(algo, "whiteList", symbol):
            arguments(algo)["whiteList"] = symbol
        return
    set_arg(algo, "whiteList", {"count": 1, "sample": [symbol], "all": [symbol]})


def our_algo(algo: dict[str, Any], prefix: str) -> bool:
    tag = prefix + " "
    naming = str(get_arg(algo, "namingRule") or "")
    return algo_title(algo).startswith(tag) or naming.startswith(tag)


def order_size() -> float:
    """Число в поле Order size Shot: уже номинал с плечом. 10 = ордер на $10."""
    raw = env("ORDER_SIZE")
    if raw:
        return round(float(raw), 4)
    return round(env_float("MARGIN_USDT", 10), 4)


def hours_to_seconds(hours: float) -> int:
    return max(1, int(round(hours * 3600)))


def as_timespan(seconds: int) -> str:
    hours, rem = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def stamp_clone(algo: dict[str, Any], master_id: int, seconds: int) -> None:
    """Помечает копию как клон с lifetime — MT сам остановит и удалит."""
    algo["isClone"] = True
    timespan = as_timespan(seconds)
    existing_lifetime = [k for k in CLONE_LIFETIME_KEYS if k in algo]
    if existing_lifetime:
        for key in existing_lifetime:
            current = algo[key]
            if isinstance(current, str) and ":" in current:
                algo[key] = timespan
            else:
                algo[key] = seconds
    else:
        algo["cloneLifeTime"] = seconds
        algo["lifeTime"] = seconds
    existing_parent = [k for k in CLONE_PARENT_KEYS if k in algo]
    if existing_parent:
        for key in existing_parent:
            algo[key] = master_id
    else:
        algo["parentId"] = master_id
        algo["cloneParentId"] = master_id


def disable_active_markets(algo: dict[str, Any]) -> None:
    current = get_arg(algo, "activeMarketsFilter")
    if isinstance(current, dict):
        current["isEnabled"] = False
        if "maxActiveMarkets" in current:
            current["maxActiveMarkets"] = 1
        set_arg(algo, "activeMarketsFilter", current)


def build_algo(
    template: dict[str, Any],
    pair: dict[str, Any],
    folder_id: int,
    prefix: str,
    *,
    auto_start: bool,
) -> dict[str, Any]:
    algo = copy.deepcopy(template)
    symbol = mt_symbol(str(pair.get("symbol") or ""))
    base = str(pair.get("base") or symbol.split("-")[0]).upper()
    distance = round(float(pair.get("recommend_pct") or 0), 4)
    tp = round(float(pair.get("tp_pct") or 0), 4)
    lever = float(pair.get("lever") or 0) or env_float("DEFAULT_LEVERAGE", 20)
    hold_ms = int(pair.get("hold_ms") or 300)
    size = order_size()
    name = f"{prefix} {base} D{distance:g} TP{tp:g} x{lever:g}"
    algo["id"] = new_id()
    algo["groupID"] = folder_id
    algo["groupType"] = algo.get("groupType") or 1
    algo["isTradingAlgo"] = True
    algo["isClone"] = False
    algo["name"] = template.get("name") or "Shots Group"
    algo["signature"] = template.get("signature") or "SG"
    if "info" in algo:
        algo["info"] = name
    set_arg(algo, "info", name)
    set_arg(algo, "namingRule", name)
    set_arg(algo, "isEmulated", False)
    set_arg(algo, "autoStart", auto_start)
    set_arg(algo, "autoRestart", False if auto_start else bool(get_arg(algo, "autoRestart")))
    set_arg(algo, "distance", distance)
    set_whitelist(algo, symbol)
    if get_arg(algo, "blackList") is None or isinstance(get_arg(algo, "blackList"), str):
        set_arg(algo, "blackList", "")
    walk_set_numeric(algo, {"distance"}, distance)
    if tp > 0:
        walk_set_numeric(
            algo,
            {
                "takeprofit",
                "takeprofitpercent",
                "takeprofitpercentage",
                "tppercent",
            },
            tp,
        )
        set_arg(algo, "takeProfitPercentage", tp)
        set_arg(algo, "takeProfitPercent", tp)
    walk_set_numeric(algo, {"ordersize", "ordervolume", "volumeusdt", "sizeusdt"}, size)
    set_arg(algo, "orderSize", size)
    set_minmax(algo, {"togglemaxleveragefilter", "toggleleveragefilter"}, lever, lever)
    walk_set_numeric(algo, {"shotrestartdelay"}, max(0.3, hold_ms / 1000.0))
    disable_active_markets(algo)
    return algo


def build_clone(master: dict[str, Any], seconds: int) -> dict[str, Any]:
    clone = copy.deepcopy(master)
    master_id = int(master.get("id") or 0)
    clone["id"] = new_id()
    stamp_clone(clone, master_id, seconds)
    set_arg(clone, "autoStart", True)
    set_arg(clone, "autoRestart", False)
    title = algo_title(master)
    hours = max(1, int(round(seconds / 3600)))
    clone_name = f"{title} {hours}h"
    if "info" in clone:
        clone["info"] = clone_name
    set_arg(clone, "info", clone_name)
    set_arg(clone, "namingRule", clone_name)
    return clone


def prune_ours(algos: list[Any], prefix: str, folder_id: int = 0) -> int:
    kept: list[Any] = []
    removed = 0
    for algo in algos:
        if isinstance(algo, dict) and our_algo(algo, prefix):
            if folder_id and int(algo.get("groupID") or 0) not in {0, folder_id}:
                kept.append(algo)
                continue
            removed += 1
            continue
        kept.append(algo)
    algos[:] = kept
    return removed


def write_state(payload: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_plan(plan: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    path = default_algos_path()
    if not path.is_file():
        raise SystemExit(f"Не найден {path}. Задайте MT_ALGOS_PATH или MT_PROFILE.")
    folder = env("MT_FOLDER", FOLDER_DEFAULT)
    prefix = env("ALGO_PREFIX", PREFIX_DEFAULT)
    pairs = select_pairs(plan)
    if not pairs:
        raise SystemExit("В плане нет пар для запуска (проверьте SUBSCRIBED_ONLY и что ShotCore уже насчитал рекомендации).")
    data, encoding = load_config(path)
    algos, key, root = as_list_container(data)
    template = find_template(algos, env("MT_TEMPLATE"))
    if isinstance(root, dict):
        folder_id = ensure_folder(root, folder)
    else:
        folder_id = new_id()
        log(f"в файле нет списка папок — groupID={folder_id}. Если «{folder}» не появится, создайте папку в клиенте.")
    removed = prune_ours(algos, prefix)
    hours = float(plan.get("run_hours") or env_float("RUN_HOURS", 3))
    seconds = hours_to_seconds(hours)
    size = order_size()
    created = []
    for pair in pairs:
        master = build_algo(template, pair, folder_id, prefix, auto_start=False)
        clone = build_clone(master, seconds)
        algos.append(master)
        algos.append(clone)
        created.append(
            {
                "id": master["id"],
                "clone_id": clone["id"],
                "name": algo_title(master),
                "clone_name": algo_title(clone),
                "symbol": pair.get("symbol"),
                "distance": pair.get("recommend_pct"),
                "tp": pair.get("tp_pct"),
                "lever": pair.get("lever"),
                "order_size": size,
                "lifetime_sec": seconds,
            }
        )
        log(
            f"{pair.get('symbol')}  D={pair.get('recommend_pct')}%  TP={pair.get('tp_pct')}%  "
            f"x{pair.get('lever') or env_float('DEFAULT_LEVERAGE', 20):g}  "
            f"orderSize={size:g}  clone={seconds}s"
        )
    state = {
        "folder": folder,
        "folder_id": folder_id,
        "prefix": prefix,
        "hours": hours,
        "lifetime_sec": seconds,
        "started_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stop_utc": (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "algos": created,
        "removed_old": removed,
        "algos_path": str(path),
        "configs_key": key,
    }
    if dry_run:
        log(
            f"DRY RUN: снял бы {removed} старых, создал бы {len(created)} мастеров "
            f"+ {len(created)} клонов на {hours:g} ч в «{folder}». Файл не изменён."
        )
        return state
    save_config(path, data, encoding)
    write_state(state)
    log(f"записано {len(created)} мастеров и {len(created)} клонов в {path} (папка «{folder}»).")
    log("Чтобы ядро подхватило файл: перезапустите MTCore или обновите список алгоритмов в клиенте.")
    log(f"Клоны живут {hours:g} ч ({seconds} с, до {state['stop_utc']} UTC) — MT сам снимет их. Python не ждёт.")
    return state


def stop_algos(dry_run: bool) -> None:
    path = default_algos_path()
    if not path.is_file():
        raise SystemExit(f"Не найден {path}")
    prefix = env("ALGO_PREFIX", PREFIX_DEFAULT)
    folder = env("MT_FOLDER", FOLDER_DEFAULT)
    data, encoding = load_config(path)
    algos, _key, root = as_list_container(data)
    removed = prune_ours(algos, prefix)
    log(f"к снятию: {removed} алгоритмов {prefix}* в «{folder}»")
    if dry_run:
        return
    save_config(path, data, encoding)
    if STATE_PATH.is_file():
        STATE_PATH.unlink()
    log("алгоритмы ShotCore сняты из algorithms.config. Обновите ядро/клиент.")


def cmd_inspect() -> None:
    path = default_algos_path()
    print("file:", path)
    print("exists:", path.is_file())
    if not path.is_file():
        return
    data, encoding = load_config(path)
    print("encoding:", encoding)
    print("root:", type(data).__name__, list(data)[:20] if isinstance(data, dict) else f"len={len(data)}")
    algos, key, root = as_list_container(data)
    print("algorithms key:", key, "count:", len(algos))
    groups, gkey = groups_of(root if key else data)
    print("groups key:", gkey, "count:", 0 if groups is None else len(groups))
    if groups:
        for item in groups[:15]:
            print(
                "  folder:",
                group_id(item),
                group_name(item),
                {k: item.get(k) for k in ("id", "name", "groupType", "actionType", "type", "action") if k in item},
            )
    shots = [a for a in algos if isinstance(a, dict) and str(a.get("signature") or "").upper() == "SG"]
    print("Shot Group count:", len(shots), "clones:", sum(1 for a in shots if a.get("isClone")))
    for algo in (shots or algos)[:8]:
        args = arguments(algo)
        names = sorted(str(k) for k in args)
        print(
            f"  id={algo.get('id')} group={algo.get('groupID')} sig={algo.get('signature')} "
            f"isClone={algo.get('isClone')} info={algo_title(algo)!r} "
            f"distance={get_arg(algo, 'distance')} orderSize={get_arg(algo, 'orderSize')} "
            f"autoStart={get_arg(algo, 'autoStart')} args={names[:16]}"
        )


def cmd_plan() -> None:
    plan = fetch_plan(env("SHOTCORE_URL", "http://127.0.0.1:4861"), env("SHOTCORE_TOKEN"))
    pairs = select_pairs(plan)
    print("updated:", plan.get("updated_utc"), "run_hours:", plan.get("run_hours"), "pairs in plan:", len(plan.get("pairs") or []))
    print("selected:", len(pairs), "orderSize:", order_size())
    for pair in pairs:
        print(
            f"  {pair.get('symbol'):<22} rec={pair.get('recommend_pct')}%  "
            f"tp={pair.get('tp_pct')}%  x{pair.get('lever') or 0:g}  sub={pair.get('subscribed')}"
        )


def cmd_apply(dry: bool) -> dict[str, Any]:
    plan = fetch_plan(env("SHOTCORE_URL", "http://127.0.0.1:4861"), env("SHOTCORE_TOKEN"))
    return apply_plan(plan, dry_run=dry)


def cmd_run(dry: bool) -> None:
    cmd_apply(dry)
    if dry:
        log("DRY RUN: MT сам снял бы клоны по lifetime. Python не спит.")
        return
    log("скрипт завершён. Клоны гасит MoonTrader по времени жизни.")


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="ShotCore → MoonTrader Shot Group")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "apply", "stop", "plan", "inspect"])
    parser.add_argument("--dry-run", action="store_true", help="не писать algorithms.config")
    args = parser.parse_args()
    dry = args.dry_run or env_bool("DRY_RUN", False)
    if args.command == "inspect":
        cmd_inspect()
    elif args.command == "plan":
        cmd_plan()
    elif args.command == "apply":
        cmd_apply(dry)
    elif args.command == "stop":
        stop_algos(dry)
    else:
        cmd_run(dry)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"ошибка: {exc}")
        sys.exit(1)
