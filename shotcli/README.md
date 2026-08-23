# Shot CLI — терминальный клиент

Для Ubuntu Server без браузера: те же функции, что на страницах ShotCore (`:4861`) и ShotTrader (`:4863`).

CLI **не поднимает** ядро и терминал. Он ходит в уже запущенные сервисы по HTTP.

```
ShotCore :4861  ← разведка, средние D, алгоритм
ShotTrader :4863 ← ордера, лонг/шорт, размеры, panic
        ↑
   python3 -m shotcli
```

На Ubuntu пакет называется `python3`. Команды `python` там нет — это не ошибка установки.

---

## Запуск на сервере

Из **корня репозитория**, рядом с уже работающими ShotCore и ShotTrader (или Docker).

```bash
cd /home/bcore/MBsupport    # ваш путь
python3 -m shotcli
```

или:

```bash
chmod +x shotcli.sh
./shotcli.sh
```

Откроется интерактивное меню. `pip` тут не нужен.

Если сервисы в Docker:

```bash
docker compose exec shottrader python3 -m shotcli \
  --core http://shotcore:4861 \
  --trader http://127.0.0.1:4863
```

С хоста к проброшенным портам:

```bash
export SHOTCORE_URL=http://127.0.0.1:4861
export SHOTTRADER_URL=http://127.0.0.1:4863
# если на ядре включён WEB_TOKEN / AUTH:
export SHOTCORE_TOKEN=тот-же-секрет
python3 -m shotcli
```

---

## Интерактивное меню

| Клавиша | Что делает |
|---|---|
| `1` | состояние: LIVE/эмуляция, long/short, PnL, клоны |
| `2` | живой экран ордеров (V1 + V2), обновление раз в секунду. `Ctrl+C` — назад |
| `3` | таблица ордеров один раз |
| `4` | сделки за сутки |
| `5` | час / сегодня / отчёт 7 дней |
| `6` | план ShotCore (BUY / V2 / SHORT / V2) |
| `7` | разведка: среднее LONG/SHORT и таблица пар |
| `8` | галочки направления: ставить лонги / шорты |
| `9` | order size x20 / x50, авто-стоп |
| `a` | правка алгоритма ShotCore без рестарта |
| `l` | лог терминала |
| `p` | Panic — снять все ордера |
| `r` | снять авто-стоп |
| `0` | выход |

---

## Команды без меню

Удобно из скриптов и `tmux`.

```bash
python3 -m shotcli status          # режим, PnL, направления
python3 -m shotcli watch           # ордера онлайн
python3 -m shotcli orders
python3 -m shotcli deals
python3 -m shotcli stats
python3 -m shotcli plan
python3 -m shotcli core            # средние рекомендации + таблица
python3 -m shotcli logs
python3 -m shotcli panic
python3 -m shotcli resume
python3 -m shotcli algo            # текущие пороги разведки
```

### Настройки терминала

```bash
python3 -m shotcli set long on
python3 -m shotcli set long off
python3 -m shotcli set short on
python3 -m shotcli set short off
python3 -m shotcli set size20 15
python3 -m shotcli set size50 10
python3 -m shotcli set autostop 8
python3 -m shotcli set mindist 0.85
python3 -m shotcli set v2gap 0.3
```

`off` на long или short сразу перестаёт ставить ордера в эту сторону (как галочка на веб-странице).

### Правка алгоритма ShotCore

Горячо, без рестарта ядра. Пишется в `data/algo_runtime.json` и переживает перезапуск.

```bash
python3 -m shotcli algo-set min_win_pct=75
python3 -m shotcli algo-set hold_ms=300 tp_min_pct=0.3
python3 -m shotcli algo-set suggest_inside_pct=0.05 suggest_inside_max_pct=0.10
python3 -m shotcli algo-set min_percent=0.80 min_fills=3
python3 -m shotcli algo-set windows_ms=500,700,1200
python3 -m shotcli algo-set distance_levels=1.11,1.32,1.42,1.63,1.78
```

| Ключ | Смысл |
|---|---|
| `min_win_pct` | минимум плюсовых сделок для рекомендации (обычно 70) |
| `min_fills` | минимум исполнений в симуляции |
| `tp_min_pct` | пол TP, не ниже 0.3 |
| `hold_ms` | выход по времени после входа |
| `suggest_inside_pct` / `suggest_inside_max_pct` | отступ от края прострела |
| `distance_levels` | запасные уровни D |
| `min_percent` | минимальный прострел, который ловит детектор |
| `min_trades` | минимум сделок в окне |
| `min_quote_volume` | минимум объёма прострела |
| `cooldown_ms` | пауза после прострела |
| `windows_ms` | окна детектора, мс |

---

## URL и токены

| Переменная / флаг | Зачем |
|---|---|
| `SHOTCORE_URL` / `--core` | адрес ядра |
| `SHOTTRADER_URL` / `--trader` | адрес терминала |
| `SHOTCORE_TOKEN` / `--core-token` | если на ShotCore включён `WEB_TOKEN` или AUTH |
| `TRADER_TOKEN` / `--trader-token` | если страница ShotTrader закрыта токеном |
| `AUTH_USERS` / `--user` `--password` | если `AUTH_MODE=local` — CLI сам логинится |

CLI сам читает `.env` и `shottrader/.env` из корня репозитория. `/health` всегда открыт, API — нет: без логина будет `401`.

```bash
python3 -m shotcli --user admin --password 'ваш_пароль'
```

Пример с другой машины в LAN:

```bash
python3 -m shotcli --core http://192.168.1.10:4861 --trader http://192.168.1.10:4863
```

---

## Что смотреть в watch

Колонки `D` — основная рекомендация. `V2` — страховочный ордер глубже: среднее прострелов, которые ушли дальше основной D. Если больших прострелов мало, V2 = `—` (ордер не ставится).

`сост` / `V2` = `hunt` (ждёт цену) или `buy`/`sell` (в позиции).
