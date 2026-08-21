# ShotCore

Разведка под стратегию Shot: заранее стоящий лимит на максимальном плече (x20–x100), вход на простреле, закрытие **через 0.3 с в противоположную сторону**. Один импульс пишется **один раз**. Подписка только на активные рынки, отобранные так же, как фильтр MoonTrader (1ч Δ + 15м оΔ, max 25, ignore first 2, sort 15 s).

Ядро не ставит ордера и не ходит в торговый API. Оно смотрит публичную ленту OKX и отвечает на два вопроса:

1. по каким парам вообще были прострелы;
2. на каком Distance ставить лимит, чтобы после удержания 0.3 с чаще выходить в плюс.

- Дашборд: `http://IP:4861/`
- История: `data/shots.csv`
- Сводка: `data/distance_hints.csv` (`suggest_distance`, `vplus_rate`)

---

## Стратегия, которую считает ядро

1. Берётся опорная цена до импульса.
2. Прострел DOWN заполняет заранее стоящий **BUY** на дистанции D%. Прострел UP — **SELL**.
3. Через `HOLD_MS` (по умолчанию 300 мс) позиция закрывается противоположной стороной.
4. **В плюс**, если PnL > `VPLUS_MIN_PNL` (считается внутри, на странице не показывается).

По каждой паре перебираются дистанции из `DISTANCE_LEVELS` (как в вашем MT: 1.11 / 1.32 / 1.42 / 1.63 / 1.78). В колонке «Дистанция ордера» — уровень, на котором симуляция чаще закрывалась в плюс.

---

## Что умеет

1. Подписывается на ленту сделок OKX (`wss://ws.okx.com:8443/ws/v5/public`).
2. Отбирает пары фильтрами как в MT Shot: QAV 24h, шаг цены, mark price, плечо, whitelist/blacklist.
3. Ловит прострел ≥ `SHOT_MIN_PERCENT` **один раз на импульс**, меряет глубину и откат.
4. Симулирует вход на каждой дистанции из `.env` и выход через 0.3 с.
5. Показывает активные рынки (как в MT), плечо пары, дистанцию ордера и ленту событий.

---

## Требования

**Вариант A — Docker (рекомендуется)**

- Docker 24+
- Docker Compose v2 (`docker compose`)

**Вариант B — Python**

- Python 3.10+
- доступ в интернет до `www.okx.com` и `ws.okx.com:8443`

Открой входящий TCP-порт дашборда (по умолчанию **4861**) в фаерволе ВМ, если заходишь с другого компьютера.

---

## 1. Первый запуск (Docker)

```bash
git clone git@gitlab.com:<группа-или-логин>/MBsupport.git
cd MBsupport

cp .env.example .env
nano .env          # или vim / notepad

docker compose up -d --build
docker compose ps
docker compose logs -f --tail=50
```

Страница: **http://IP-сервера:4861/**

Проверка, что ядро живо:

```bash
curl http://127.0.0.1:4861/health
curl http://127.0.0.1:4861/api/status
```

Остановка:

```bash
docker compose down
```

Данные в `./data` и `./logs` контейнер не удаляет.

---

## 2. Запуск без Docker

```bash
git clone git@gitlab.com:<группа-или-логин>/MBsupport.git
cd MBsupport

cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m shotcore
```

Ядро читает `.env` и `config.yaml`. Значения из `.env` важнее yaml.

Фоном через systemd (Linux):

```bash
sudo mkdir -p /opt/shotcore
sudo rsync -a --exclude .venv --exclude .git ./ /opt/shotcore/
cd /opt/shotcore
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo cp shotcore.service /etc/systemd/system/shotcore.service
sudo systemctl daemon-reload
sudo systemctl enable --now shotcore
sudo systemctl status shotcore
```

---

## 3. Обновление (git pull + апдейт)

### Docker

```bash
cd MBsupport
git pull origin main

# если меняли только код/зависимости:
docker compose up -d --build

# если меняли .env — пересоздать контейнер:
docker compose up -d --build --force-recreate
```

Проверка после апдейта:

```bash
docker compose ps
docker compose logs --tail=80 shotcore
curl -s http://127.0.0.1:4861/api/status
```

Откат на предыдущий коммит, если что-то сломалось:

```bash
git log --oneline -5
git checkout <hash>
docker compose up -d --build --force-recreate
```

Потом вернитесь на `main`: `git checkout main && git pull && docker compose up -d --build`.

### Python / systemd

```bash
cd MBsupport          # или /opt/shotcore
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
# локальный запуск: Ctrl+C и снова python -m shotcore
sudo systemctl restart shotcore    # если стоит unit
```

История прострелов в `data/` при обновлении **не затирается**.

---

## 4. Настройка через `.env`

Скопируй `.env.example` → `.env` и правь. После сохранения:

- Docker: `docker compose up -d --force-recreate`
- Python: перезапусти процесс / `systemctl restart shotcore`

| Переменная | Зачем |
|---|---|
| `WEB_HOST` / `WEB_PORT` | Адрес страницы. Снаружи удобно `0.0.0.0` и `4861` |
| `WEB_TOKEN` | Если задать — вход только с `http://IP:4861/?token=СЕКРЕТ` |
| `TZ` | Часовой пояс ленты, например `Europe/Moscow` |
| `STATS_LOOKBACK_MIN` | Окно статистики на странице, минуты. `0` = вся история |
| `SHOT_WINDOWS_MS` | Окна детекции, мс, через запятую |
| `SHOT_MIN_PERCENT` | Минимальный прострел, который пишем |
| `HOLD_MS` | Удержание до закрытия в противоположную сторону, мс (у вас 300) |
| `DISTANCE_LEVELS` | Дистанции ордеров для перебора, как Distance в MT |
| `QAV_24H_MIN` / `QAV_24H_MAX` | Объём 24h в USDT |
| `TICK_SIZE_PCT_MAX` | Отсечь «квадратные» монеты по шагу цены |
| `MARK_DEV_PCT_MAX` | Допустимое отклонение mark price, % |
| `MIN_LEVERAGE` / `MAX_LEVERAGE` | Коридор макс. плеча контракта, x20–x100 |
| `ACTIVE_MAX_MARKETS` | Как «Макс. кол. рынков» в MT, по умолчанию 25 |
| `ACTIVE_IGNORE_FIRST` | Как «Игнорировать первые», по умолчанию 2 |
| `ACTIVE_SORT_SEC` | Как «Частота сортировки», по умолчанию 15 |
| `SHOT_REFRACTORY_MS` | Пауза после фиксации прострела, чтобы не резать один импульс пачками |
| `WHITELIST` | Пары OKX через запятую, пусто = все прошедшие фильтры |
| `BLACKLIST` | Исключить пары |
| `BTC_WINDOW_SEC` / `BTC_RANGE_PCT` | Фильтр «спокойный BTC», как delta в MT |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Опционально, алерты от `TELEGRAM_MIN_PERCENT` |

Формат пар: `SOL-USDT-SWAP,DOGE-USDT-SWAP`.

На странице можно дополнительно сузить окно (15 мин / 1 ч / 6 ч / 24 ч), сторону DOWN/UP и «только спокойный BTC» — это не требует перезапуска.

---

## 5. Дашборд

| URL | Что |
|---|---|
| `/` | Активные рынки как в MT, пары с прострелами, дистанция, плечо, лента |
| `/api/status` | Сколько пар в подписке, фильтры |
| `/api/stats` | JSON статистики |
| `/api/mt-plan` | JSON для ядра MT: пара, рекомендация, TP, run_hours |
| `/health` | `ok` для healthcheck |

**Дистанция ордера** — уровень из `DISTANCE_LEVELS`, на котором симуляция «лимит → выход через 0.3 с» чаще в плюсе. Это значение ставьте в Distance алгоритма Shot. **Глубина p50/p90** — насколько реально улетал прострел. **Плечо** — максимальное доступное на паре (x20…x100).

Фильтр активных рынков на странице совпадает с MoonTrader: **1ч Δ** (диапазон high−low за час) и **15м оΔ** ((last−open)/open за 15 минут). Список сортируется каждые 15 с по убыванию 1ч Δ, первые 2 отбрасываются, берутся следующие 25. Подписка на сделки — только эти 25 плюс BTC.

---

## 6. Файлы на диске

```
data/shots.csv              события за последние 24 часа
data/shots.jsonl            то же, без тиковых графиков
data/distance_hints.csv     сводка: suggest_distance, vplus_rate, avg_pnl
data/mt_plan.json           снимок для MT: пара, рекомендация, TP
data/mt_plan.csv            то же построчно
logs/shotcore.log           лог ядра (не старше суток)
```

В Docker эти каталоги примонтированы с хоста (`./data`, `./logs`).

---

## 7. Свой терминал ShotTrader (вместо MT)

Если MoonTrader не интегрируется — запускайте **ShotTrader**: UI в духе MT, тиковый график, размер/плечо, клоны на 3 часа по плану ShotCore, follow 1 с, TP или 0.3 с, авто-стоп −10$, отчёты час/сутки.

```bash
cd shottrader
cp .env.example .env
# SHOTCORE_URL=http://IP-ShotCore:4861
python -m shottrader
# UI: http://IP:4863/
```

Или из корня: `docker compose up -d --build shottrader`. По умолчанию **эмуляция**. LIVE — только с ключами OKX (`LIVE_TRADING=true`). Подробнее: [`shottrader/README.md`](shottrader/README.md).

Старый путь через `mt_launcher` → `algorithms.config` MoonTrader по-прежнему в [`mt_launcher/README.md`](mt_launcher/README.md).

---

## 8. Типичные проблемы

**Страница не открывается с другого ПК**  
В `.env` должно быть `WEB_HOST=0.0.0.0`. Проверь фаервол: `sudo ufw allow 4861/tcp`.

**Нет пар / 0 symbols**  
Слишком жёсткий QAV или whitelist с опечаткой. Формат только `BTC-USDT-SWAP`, не `btcusdt`.

**Контейнер Restarting**  
`docker compose logs shotcore` — чаще всего нет `.env` (`cp .env.example .env`) или нет сети до OKX.

**После `git pull` старое поведение**  
Не забыли `--build`: без него Compose может поднять старый образ.

**401 Need WEB_TOKEN**  
Задан `WEB_TOKEN`. Открой `http://IP:4861/?token=значение_из_env`.

---

## 9. Структура репозитория

```
Dockerfile
docker-compose.yml
.env.example
config.yaml              запасные значения, если ключа нет в .env
requirements.txt
shotcore.service         unit для Linux без Docker
shotcore/                код ядра и web/
mt_launcher/             скрипт: план ShotCore → алгоритмы MoonTrader
```

Файл `.env` в git не коммитится.
