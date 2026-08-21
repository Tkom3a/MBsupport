# ShotTrader

Свой терминал в духе MoonTrader: тиковый график, размер ордера, плечо, клоны Shot Group.
Ордера и follow живут в этом приложении — **MTCore не нужен**.

## Что делает

1. Раз в минуту читает `GET ShotCore /api/mt-plan` (пары с рекомендацией D/TP и плюсом ≥ 70%).
2. На новую запись поднимает клон на **3 часа**.
3. Ставит лимиты BUY и SELL на дистанции D% от цены с запазданием **1 с** (follow).
4. После заполнения: **TP** или закрытие через **0.3 с**.
5. **Авто-стоп −10$**: закрытая или открытая сделка с минусом ≥ порога → panic, всё снимается.
6. Отчёты раз в час и раз в сутки (лог + Telegram, если задан).

По умолчанию **эмуляция**: сделки виртуальные, ключи OKX не нужны. Живая торговля — только осознанно.

## Запуск

Рядом с уже работающим ShotCore:

```bash
cd shottrader
cp .env.example .env
# SHOTCORE_URL=http://IP-ShotCore:4861
python -m shottrader
```

График убран: вместо него онлайн-таблица цен, BUY/SELL и расстояния до ордеров (обновление ~0.8 с).

UI: `http://IP:4863/` · с дашборда ShotCore вкладка **ShotTrader**.

## Авторизация

Те же переменные, что у ShotCore (`AUTH_MODE`, `AUTH_USERS` / LDAP_* , `SESSION_SECRET`).
При `AUTH_MODE=local` или `ldap` браузер открывает `/login`.
Сервисный доступ к API: `WEB_TOKEN` / `X-Shot-Token`.

Через Docker из корня репозитория (сервис `shottrader` в compose):

```bash
docker compose up -d --build shottrader
```

## LIVE (осторожно)

В `.env`:

```
EMULATE=false
LIVE_TRADING=true
OKX_API_KEY=...
OKX_SECRET_KEY=...
OKX_PASSPHRASE=...
```

Сначала проверьте эмуляцию. `OKX_SIMULATED=true` — demo API OKX.

## Управление в UI

| Элемент | Назначение |
|---|---|
| Order size | номинал USDT уже с плечом (как в Shot) |
| Плечо | для новых клонов / set-leverage в LIVE |
| Авто-стоп $ | порог минуса |
| Panic | снять все ордера и клоны |
| Снять авто-стоп | снова читать план |

## Файлы

```
shottrader/
  main.py          цикл: план / follow / WS / отчёты
  engine.py        клоны, fill, TP/0.3с, авто-стоп
  okx_broker.py    REST ордера (LIVE)
  web/index.html   терминал
data/trader_journal.jsonl
```
