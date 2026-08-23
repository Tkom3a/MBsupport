# ShotTrader

Терминал стратегии Shot. MoonTrader не нужен.

Полная инструкция по установке, портам, авторизации и LIVE — в [корневом README](../README.md).

## Коротко

1. Раз в минуту читает план ShotCore (`GET /api/mt-plan`).
2. На новую рекомендацию поднимает клон на **3 часа**.
3. Ставит лимиты BUY и SHORT на дистанциях D% (стороны считаются отдельно) с запазданием **1 с**, плюс страховочные **V2** глубже основной D — если по паре есть более глубокие прострелы.
4. После заполнения: **TP ≥ 0.3%** или закрытие через **0.3 с**.
5. Авто-стоп: минус ≥ порога → panic, всё снимается.

По умолчанию **эмуляция**. Ключи OKX не нужны, пока не включите LIVE.

## Запуск

Сначала должен работать ShotCore (`:4861`).

```bash
cd shottrader
cp .env.example .env
# SHOTCORE_URL=http://IP-ShotCore:4861
python -m shottrader
```

UI: `http://IP:4863/` · вкладка **ShotTrader** на дашборде ядра.

Docker из корня репозитория:

```bash
docker compose up -d --build
```

## Важно про токены

| Переменная | Зачем |
|---|---|
| `SHOTCORE_TOKEN` | Чтобы терминал мог читать план у ShotCore, если там включён логин/`WEB_TOKEN`. Должен совпадать с `WEB_TOKEN` ядра. |
| `TRADER_TOKEN` / `AUTH_MODE` | Закрывают **страницу** ShotTrader. `WEB_TOKEN` страницу терминала не запирает. |

## LIVE (осторожно)

Сначала эмуляция. Потом в `.env`:

```env
EMULATE=false
LIVE_TRADING=true
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
```

`OKX_SIMULATED=true` — demo API OKX.

## Управление в UI

| Элемент | Назначение |
|---|---|
| Order size x20 / x50 | номинал USDT с плечом для пар x20 и x50 |
| Ставить лонги / шорты | галочки направления: сняли — ордера в эту сторону не ставятся |
| Авто-стоп $ | порог минуса |
| Применить | записать размеры / авто-стоп |
| Panic | снять все ордера и клоны |
| Снять авто-стоп | снова читать план |

Без браузера те же действия: `python3 -m shotcli` (см. [../shotcli/README.md](../shotcli/README.md)).

## Файлы

```
shottrader/
  main.py          цикл: план / follow / WS / отчёты
  engine.py        клоны, fill, TP/0.3с, авто-стоп
  okx_broker.py    REST ордера (LIVE)
  web/index.html   терминал
data/trader_journal.jsonl
data/daily_reports.json
```
