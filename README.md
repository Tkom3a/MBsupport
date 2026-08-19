# ShotCore

Разведка под стратегию Shot: заранее стоящий лимит на максимальном плече, вход на простреле, закрытие **через 0.3 с в противоположную сторону**. Главный показатель — **В+** (сделка закрылась в плюс).

Ядро не ставит ордера и не ходит в торговый API. Оно смотрит публичную ленту OKX и отвечает на два вопроса:

1. по каким парам вообще были прострелы;
2. на каком Distance ставить лимит, чтобы после удержания 0.3 с чаще выходить в плюс.

- Дашборд: `http://IP:8787/`
- История: `data/shots.csv`
- Сводка: `data/distance_hints.csv` (`suggest_distance`, `vplus_rate`)

---

## Стратегия, которую считает ядро

1. Берётся опорная цена до импульса.
2. Прострел DOWN заполняет заранее стоящий **BUY** на дистанции D%. Прострел UP — **SELL**.
3. Через `HOLD_MS` (по умолчанию 300 мс) позиция закрывается противоположной стороной.
4. **В+**, если PnL > `VPLUS_MIN_PNL`.

По каждой паре перебираются дистанции из `DISTANCE_LEVELS` (как в вашем MT: 1.11 / 1.32 / 1.42 / 1.63 / 1.78). В колонке «Дистанция ордера» — уровень с лучшим В+.

---

## Что умеет

1. Подписывается на ленту сделок OKX (`wss://ws.okx.com:8443/ws/v5/public`).
2. Отбирает пары фильтрами как в MT Shot: QAV 24h, шаг цены, mark price, плечо, whitelist/blacklist.
3. Ловит прострел ≥ `SHOT_MIN_PERCENT`, меряет глубину и откат.
4. Симулирует вход на каждой дистанции из `.env` и выход через 0.3 с.
5. Показывает пары, дистанцию ордера, долю В+ и ленту событий.

---

## Требования

**Вариант A — Docker (рекомендуется)**

- Docker 24+
- Docker Compose v2 (`docker compose`)

**Вариант B — Python**

- Python 3.10+
- доступ в интернет до `www.okx.com` и `ws.okx.com:8443`

Открой входящий TCP-порт дашборда (по умолчанию **8787**) в фаерволе ВМ, если заходишь с другого компьютера.

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

Страница: **http://IP-сервера:8787/**

Проверка, что ядро живо:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/api/status
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
curl -s http://127.0.0.1:8787/api/status
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
| `WEB_HOST` / `WEB_PORT` | Адрес страницы. Снаружи удобно `0.0.0.0` и `8787` |
| `WEB_TOKEN` | Если задать — вход только с `http://IP:8787/?token=СЕКРЕТ` |
| `TZ` | Часовой пояс ленты, например `Europe/Moscow` |
| `STATS_LOOKBACK_MIN` | Окно статистики на странице, минуты. `0` = вся история |
| `SHOT_WINDOWS_MS` | Окна детекции, мс, через запятую |
| `SHOT_MIN_PERCENT` | Минимальный прострел, который пишем |
| `HOLD_MS` | Удержание до закрытия в противоположную сторону, мс (у вас 300) |
| `DISTANCE_LEVELS` | Дистанции ордеров для перебора, как Distance в MT |
| `VPLUS_MIN_PNL` | PnL выше этого = В+ (обычно 0) |
| `QAV_24H_MIN` / `QAV_24H_MAX` | Объём 24h в USDT |
| `TICK_SIZE_PCT_MAX` | Отсечь «квадратные» монеты по шагу цены |
| `MARK_DEV_PCT_MAX` | Допустимое отклонение mark price, % |
| `MIN_LEVERAGE` | Минимальное плечо контракта |
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
| `/` | Пары с прострелами, дистанция ордера, В+, лента |
| `/api/status` | Сколько пар в подписке, фильтры |
| `/api/stats` | JSON статистики |
| `/health` | `ok` для healthcheck |

**Дистанция ордера** — уровень из `DISTANCE_LEVELS`, на котором симуляция «лимит → выход через 0.3 с» даёт лучший В+. Это значение ставьте в Distance алгоритма Shot. **Глубина p50/p90** — насколько реально улетал прострел. **В+** — доля закрытий в плюс.

---

## 6. Файлы на диске

```
data/shots.csv              все события
data/shots.jsonl            то же, по строке на событие
data/distance_hints.csv     сводка: suggest_distance, vplus_rate, avg_pnl
logs/shotcore.log           лог ядра
```

В Docker эти каталоги примонтированы с хоста (`./data`, `./logs`).

---

## 7. Типичные проблемы

**Страница не открывается с другого ПК**  
В `.env` должно быть `WEB_HOST=0.0.0.0`. Проверь фаервол: `sudo ufw allow 8787/tcp`.

**Нет пар / 0 symbols**  
Слишком жёсткий QAV или whitelist с опечаткой. Формат только `BTC-USDT-SWAP`, не `btcusdt`.

**Контейнер Restarting**  
`docker compose logs shotcore` — чаще всего нет `.env` (`cp .env.example .env`) или нет сети до OKX.

**После `git pull` старое поведение**  
Не забыли `--build`: без него Compose может поднять старый образ.

**401 Need WEB_TOKEN**  
Задан `WEB_TOKEN`. Открой `http://IP:8787/?token=значение_из_env`.

---

## 8. Структура репозитория

```
Dockerfile
docker-compose.yml
.env.example
config.yaml              запасные значения, если ключа нет в .env
requirements.txt
shotcore.service         unit для Linux без Docker
shotcore/                код ядра и web/
```

Файл `.env` в git не коммитится.
