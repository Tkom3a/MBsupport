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
2. с него поднимается **клон** (`isClone`, `autoStart`) — как «запустить клон» в клиенте;
3. контейнер `watch` каждые 15 с сверяет таблицу ShotCore: D, TP, счёт.

Если изменились расстояние или TP — старый клон снимается, пишется новый. Если в счёте появился минус — клон гасится, ждём новую рекомендацию (ShotCore сам пересчитает D/TP). Через `RUN_HOURS` (3 ч) клон тоже снимается.

Плечо в фильтре пишется **целым** (`50`, не `50.0`) — иначе MT не стартует: `Input string '50.0' is not a valid integer`.

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

## 2. Запуск в Docker (на сервере MoonTrader)

```bash
cd ~/MBsupport/mt_launcher
cp .env.example .env
nano .env   # SHOTCORE_URL=http://IP-ShotCore:4861
# в docker-compose уже: MT_DATA_DIR=/root/.config/moontrader-data/data/okxoma
docker compose up -d --build
docker compose logs -f mt-launcher
```

Контейнер монтирует профиль ядра и каждые 15 секунд делает `watch`.

Без Docker:

```bash
cd mt_launcher
cp .env.example .env
nano .env
python3 launcher.py inspect
python3 launcher.py plan
python3 launcher.py watch
```

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

`MARGIN_USDT=10` уходит в **Order size как есть**. Не умножается на x20/x50.

`LOOKBACK_MIN=1440` — то же окно, что селект «24 часа» на веб-морде. Если на странице стоит 6 часов — поставьте `360`, иначе Distance/TP в ордере не совпадут с таблицей.

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

## 5. Боевой запуск

```bash
docker compose up -d --build
# или: python3 launcher.py watch
```

Каждые 15 с:

1. качает `GET /api/mt-plan?lookback=1440` (те же Рекомендация / TP / Счет, что на морде)
2. если пары ещё нет — мастер + клон `SC TRUMP D1.91 TP0.49 x50`, клон с `autoStart`
3. если D или TP изменились — старый клон снимается, поднимается новый
4. если в счёте вырос минус — клон гасится, пока таблица не даст другую D/TP
5. через 3 часа клон снимается

`apply` — один проход без цикла. `stop` — снять все `SC *`.

После первой записи, если ядро не подхватило файл, перезапустите `./MTCore` один раз.

---

## 6. Команды

| Команда | Что делает |
|---|---|
| `inspect` | разобрать локальный `algorithms.config` |
| `plan` | показать план с ShotCore (D/TP/счёт) |
| `apply --dry-run` | что будет создано, файл не менять |
| `apply` | один проход: мастера + клоны |
| `watch` / `run` | цикл каждые 15 с |
| `stop` | удалить алгоритмы `SC *` |

---

## 7. Безопасность

- Перед каждой записью копируется `algorithms.config.bak-launcher`.
- Скрипт удаляет только алгоритмы с префиксом `SC ` (задаётся `ALGO_PREFIX`).
- Не ходит на торговый API биржи и не читает ключи MT.
- Файл `.env` и `state.json` в git не коммитятся.

Эмуляция: в клоне выставляется `isEmulated=false`. Для проверки сначала поставьте в шаблоне эмуляцию или сделайте `--dry-run`.

---

## 8. Типичные проблемы

**Нет связи с ShotCore / 404**  
В `.env` нужен IP **сервера ShotCore**, не `127.0.0.1` (если MT на другой машине):

```
SHOTCORE_URL=http://IP-ShotCore:4861
```

С сервера MT: `curl -sS http://IP-ShotCore:4861/health` (должно быть `ok`) и `curl -sS http://IP-ShotCore:4861/api/mt-plan`. Фаервол 4861/tcp. Если задан `WEB_TOKEN` — `SHOTCORE_TOKEN` тот же. Если `/health` = ok, а `/api/mt-plan` = 404 — на ShotCore устаревший контейнер: `docker compose up -d --build --force-recreate`.

**План пустой / 0 selected**  
На дашборде ещё нет рекомендаций, или `SUBSCRIBED_ONLY=true`, а пары не в топ-25 по 1ч Δ.

**Алгоритмы в файле есть, в клиенте нет**  
Ядро не перечитало конфиг. Перезапуск `./MTCore`.

**Папка MBsupport не появилась**  
Формат групп в вашей сборке MT может отличаться. `python3 launcher.py inspect` — если `groups key: None`, создайте папку вручную в клиенте один раз, повторите `inspect`, пришлите имена ключей.

**Клон не погас через 3 часа**  
Lifetime пишется в `isClone` / `cloneLifeTime` (секунды) на копии алгоритма. Если ваша сборка хранит срок только в Triggers & Actions, пришлите `inspect` и кусок клона из `algorithms.config` после ручного «запустить клон на период» — подставим точные поля.
