# ShotCore → MoonTrader

Скрипт на **сервере с MTCore** забирает план с дашборда ShotCore по HTTP и поднимает Shot Group в папке **MBsupport**.

Для каждой пары:

| Поле MT | Откуда |
|---|---|
| White list | торговая пара из плана (`sol-usdt-swap`) |
| Distance | `recommend_pct` |
| Take Profit | `tp_pct` |
| Order size | **10 USDT** уже с плечом (как в справке Shot: «полный размер с учётом leverage») |
| Max leverage filter | макс. плечо пары (x20 / x50) |
| Папка | `MBsupport` (создаётся, если нет) |
| Время работы | клон алгоритма на `run_hours` (по умолчанию 3 часа) |

Логика запуска:

1. создаётся **мастер** (не стартует);
2. с него поднимается **клон** с lifetime = 3 часа;
3. ядро MoonTrader само останавливает и удаляет клон, когда время вышло.

Python **не спит** 3 часа.

Алгоритмы копируются с вашего рабочего Shot Group, поэтому фильтры QAV, шаг цены, mark, BTC-дельта и источник Trades остаются как в шаблоне. Меняются пара, дистанция, TP, размер ордера и фильтр плеча.

MoonTrader не даёт публичного API «создать алгоритм». Скрипт пишет `algorithms.config` профиля ядра. После записи **перезапустите MTCore** или обновите список алгоритмов в клиенте.

---

## 1. Что нужно

- Python 3.10+ на машине, где крутится **MTCore**
- сеть до ShotCore: `http://IP:4861/api/mt-plan`
- хотя бы один уже существующий **Shot Group** в профиле (шаблон)
- путь к `algorithms.config`

Типичные пути:

```
Linux:   ~/.config/moontrader-data/data/<профиль>/algorithms.config
Windows: %APPDATA%\moontrader-data\data\<профиль>\algorithms.config
```

У вас профиль ядра — `okxoma`.

---

## 2. Установка

На сервере MoonTrader (не обязательно в Docker ShotCore):

```bash
# скопируйте папку mt_launcher из репозитория
cd mt_launcher
cp .env.example .env
nano .env
```

Зависимостей pip нет — только стандартная библиотека Python.

---

## 3. Настройка `.env`

Обязательно:

```
SHOTCORE_URL=http://IP-сервера-ShotCore:4861
SHOTCORE_TOKEN=                    # если на дашборде задан WEB_TOKEN
MT_PROFILE=okxoma
MT_FOLDER=MBsupport
MARGIN_USDT=10
```

Если файл конфига лежит нестандартно:

```
MT_ALGOS_PATH=/root/.config/moontrader-data/data/okxoma/algorithms.config
```

Шаблон (имя как в клиенте, кусок строки достаточно):

```
MT_TEMPLATE=OKXBuy1Test
```

Пустой `MT_TEMPLATE` — берётся первый Shot Group в файле.

`MARGIN_USDT=10` уходит в **Order size как есть**. Не умножается на x20/x50: в Shot это уже номинал с плечом, биржа открывает $10.

`SUBSCRIBED_ONLY=true` — только пары, которые сейчас в активных рынках ShotCore (фильтр 1ч Δ).

---

## 4. Проверка, не трогая MT

```bash
python3 launcher.py inspect     # видит ли скрипт algorithms.config и Shot Group
python3 launcher.py plan        # что пришло с /api/mt-plan
python3 launcher.py apply --dry-run
```

`inspect` должен показать ваши папки и алгоритмы (`algorithms key: configs`). Если «не найден список алгоритмов» — пришлите первые строки `algorithms.config` (без секретов) и поправим разбор.

---

## 5. Боевой запуск на 3 часа

1. Остановите ядро **или** будьте готовы сразу его перезапустить — иначе MT может держать старый список в памяти.
2. Запуск:

```bash
python3 launcher.py run
```

Скрипт:

1. качает `GET /api/mt-plan`
2. делает backup `algorithms.config.bak-...`
3. создаёт папку **MBsupport**, если её нет
4. удаляет прошлые алгоритмы с префиксом `SC `
5. для каждой пары пишет мастер `SC SOL D1.42 TP0.35 x50` и клон на `RUN_HOURS` (3 ч = 10800 с)
6. сразу выходит — дальше время считает MTCore

То же самое, явно без ожидания:

```bash
python3 launcher.py apply
```

Снять раньше времени (мастера и клоны `SC *`):

```bash
python3 launcher.py stop
```

После `apply` / `run` / `stop` **перезапустите `./MTCore`**, затем в клиенте откройте вкладку «Алгоритмы» → папка **MBsupport**. Клон должен быть запущен; мастер — выключен.

---

## 6. Команды

| Команда | Что делает |
|---|---|
| `inspect` | разобрать локальный `algorithms.config` |
| `plan` | показать план с ShotCore |
| `apply --dry-run` | что будет создано, файл не менять |
| `apply` | записать мастера + клоны на 3 ч |
| `run` | то же, что `apply` (MT сам гасит клоны) |
| `stop` | удалить алгоритмы `SC *` |

---

## 7. Безопасность

- Перед каждой записью создаётся `.bak-YYYYMMDD-HHMMSS` рядом с `algorithms.config`.
- Скрипт удаляет только алгоритмы с префиксом `SC ` (задаётся `ALGO_PREFIX`).
- Не ходит на торговый API биржи и не читает ключи MT.
- Файл `.env` и `state.json` в git не коммитятся.

Эмуляция: в клоне выставляется `isEmulated=false`. Для проверки сначала поставьте в шаблоне эмуляцию или сделайте `--dry-run`.

---

## 8. Типичные проблемы

**Нет связи с ShotCore**  
С сервера MT: `curl http://IP:4861/health` и `curl http://IP:4861/api/mt-plan`. Фаервол 4861/tcp. Если задан `WEB_TOKEN` — заголовок `X-Shot-Token` или `?token=`.

**План пустой / 0 selected**  
На дашборде ещё нет рекомендаций, или `SUBSCRIBED_ONLY=true`, а пары не в топ-25 по 1ч Δ.

**Алгоритмы в файле есть, в клиенте нет**  
Ядро не перечитало конфиг. Перезапуск `./MTCore`.

**Папка MBsupport не появилась**  
Формат групп в вашей сборке MT может отличаться. `python3 launcher.py inspect` — если `groups key: None`, создайте папку вручную в клиенте один раз, повторите `inspect`, пришлите имена ключей.

**Клон не погас через 3 часа**  
Lifetime пишется в `isClone` / `cloneLifeTime` (секунды) на копии алгоритма. Если ваша сборка хранит срок только в Triggers & Actions, пришлите `inspect` и кусок клона из `algorithms.config` после ручного «запустить клон на период» — подставим точные поля.
