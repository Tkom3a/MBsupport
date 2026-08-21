# ShotCore + ShotTrader

Софт для стратегии **Shot** на фьючерсах OKX (USDT-SWAP):

1. **ShotCore** — разведка. Смотрит публичную ленту сделок, ловит «прострелы» и считает, на какой дистанции ставить лимит (отдельно для BUY и SHORT).
2. **ShotTrader** — терминал. Берёт рекомендации ShotCore и ставит ордера (сначала виртуально, живую торговлю можно включить отдельно).

MoonTrader **не нужен**. Ключи биржи для разведки **не нужны**.

| Что | Адрес |
|---|---|
| Разведка ShotCore | http://IP:4861/ |
| Терминал ShotTrader | http://IP:4863/ |

---

## Как это работает простыми словами

```
OKX (публичные сделки)
        ↓
   ShotCore :4861     ← смотрит рынок, пишет прострелы, считает D% и TP%
        ↓ план раз в минуту
   ShotTrader :4863   ← ставит BUY/SHORT, закрывает по TP (≥ 0.3%) или через 0.3 с
```

- Прострел **вниз (DOWN)** → лимит **BUY** на своей дистанции.
- Прострел **вверх (UP)** → лимит **SHORT** на своей дистанции.
- Рекомендация появляется, только если по симуляции плюсовых сделок **не меньше 70%**.
- TP ниже **0.3%** отбрасывается.
- По умолчанию ShotTrader в **эмуляции**: ордера на биржу не уходят.

---

## Что нужно установить

Выберите один способ.

### Способ A — Docker (проще)

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / macOS) или Docker Engine + Compose на Linux
- Интернет до `www.okx.com` и `wss://ws.okx.com:8443`

### Способ B — Python

- Python **3.10+**
- Интернет до OKX

Откройте порты **4861** и **4863**, если заходите с другого компьютера.

---

## Быстрый старт (Docker)

Всё из **корня** репозитория.

### 1. Скачать код

```bash
git clone https://github.com/<ваш-логин>/<репозиторий>.git
cd <репозиторий>
```

### 2. Создать файлы настроек

```bash
cp .env.example .env
cp shottrader/.env.example shottrader/.env
```

На Windows в PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item shottrader\.env.example shottrader\.env
```

Пока **ничего не меняйте** — так софт сразу запустится в безопасном режиме (эмуляция, без пароля).

### 3. Запустить

```bash
docker compose up -d --build
```

Подождать ~30–60 секунд, пока ShotCore подключится к OKX.

### 4. Открыть в браузере

- ShotCore: `http://127.0.0.1:4861/`
- ShotTrader: `http://127.0.0.1:4863/`  
  или вкладка **ShotTrader** на странице ShotCore

Если открываете с другого ПК — вместо `127.0.0.1` подставьте IP сервера.

### 5. Проверить, что живы

```bash
curl http://127.0.0.1:4861/health
curl http://127.0.0.1:4863/health
```

Должно ответить `ok`.

Полезные команды:

```bash
docker compose ps                  # статус
docker compose logs -f --tail=80   # логи
docker compose down                # остановка (данные в папках data/ и logs/ останутся)
```

---

## Запуск без Docker (Python)

```bash
git clone https://github.com/<ваш-логин>/<репозиторий>.git
cd <репозиторий>

cp .env.example .env
cp shottrader/.env.example shottrader/.env

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Два процесса (два окна терминала):

**Окно 1 — разведка**

```bash
python -m shotcore
```

**Окно 2 — терминал**

```bash
python -m shottrader
```

В `shottrader/.env` оставьте:

```env
SHOTCORE_URL=http://127.0.0.1:4861
```

Если ShotCore на другой машине:

```env
SHOTCORE_URL=http://IP-сервера-ShotCore:4861
```

---

## Минимальная настройка

Два файла:

| Файл | Для чего |
|---|---|
| `.env` | ShotCore (разведка, порт 4861) |
| `shottrader/.env` | ShotTrader (терминал, порт 4863) |

После правки `.env` перезапустите:

```bash
docker compose up -d --force-recreate
```

или перезапустите `python -m shotcore` / `python -m shottrader`.

### Что можно не трогать

Значения из примеров уже рабочие. Меняйте только то, что нужно вам.

### Часовой пояс

В обоих файлах:

```env
TZ=Europe/Moscow
```

От этого зависят лента, полночь (сброс статистики сделок) и отчёты за 7 дней.

### Размер ордера и авто-стоп

Это удобнее менять **в терминале ShotTrader** (слева: Order size, Плечо, Авто-стоп → **Применить**).

Стартовые значения в `shottrader/.env`:

```env
MARGIN_USDT=10          # размер ордера, USDT уже с плечом
DEFAULT_LEVERAGE=50
AUTOSTOP_USD=10         # если сделка ≤ −этой суммы — всё снимается
```

### Если ShotTrader не видит рекомендации

1. ShotCore должен быть открыт и уже накопить прострелы (обычно несколько минут).
2. В `shottrader/.env` правильный `SHOTCORE_URL` — **тот же IP**, с которого вы открываете ShotCore в браузере (не `127.0.0.1`, если ядро на другом сервере).
3. В Docker внутри сети по умолчанию уже стоит `http://shotcore:4861` — это нормально.

---

## Авторизация (по желанию)

По умолчанию страницы **открыты**. Чтобы закрыть логином:

### Простой пароль (без LDAP)

В **корневом** `.env` (ShotCore):

```env
AUTH_MODE=local
SESSION_SECRET=придумайте-длинную-строку
AUTH_USERS=admin:ваш_пароль
WEB_TOKEN=тот-же-секрет-для-API
```

В `shottrader/.env` (чтобы терминал мог читать план):

```env
AUTH_MODE=local
SESSION_SECRET=придумайте-длинную-строку
AUTH_USERS=admin:ваш_пароль
SHOTCORE_TOKEN=тот-же-секрет-для-API
```

`SHOTCORE_TOKEN` должен совпадать с `WEB_TOKEN` у ShotCore. Это не пароль страницы терминала, а ключ для запроса `/api/mt-plan`.

Вход: страница `/login`. Выход: ссылка **Выход**.

### LDAP / Active Directory

```env
AUTH_MODE=ldap
SESSION_SECRET=длинная-строка
LDAP_URL=ldap://dc.company.local:389
LDAP_BASE_DN=dc=company,dc=local
LDAP_BIND_DN=cn=svc,ou=...,dc=company,dc=local
LDAP_BIND_PASSWORD=...
LDAP_USER_FILTER=(sAMAccountName={username})
```

Либо без поиска, сразу UPN:

```env
LDAP_USER_DN_TEMPLATE={username}@company.local
```

Опционально пускать только членов группы: `LDAP_REQUIRE_GROUP=ShotTraders`.

### Только токен, без формы логина

ShotCore:

```env
AUTH_MODE=off
WEB_TOKEN=секрет
```

Открывать так: `http://IP:4861/?token=секрет`.

Страницу ShotTrader этот токен **не закрывает**. Чтобы закрыть терминал токеном, задайте отдельно `TRADER_TOKEN` в `shottrader/.env`.

---

## Живая торговля (LIVE) — осторожно

Сначала походите в **эмуляции** (так и есть из коробки).

Когда готовы к реальным ордерам, в `shottrader/.env`:

```env
EMULATE=false
LIVE_TRADING=true
OKX_API_KEY=
OKX_SECRET_KEY=
OKX_PASSPHRASE=
OKX_SIMULATED=false
```

`OKX_SIMULATED=true` — demo-счёт OKX, не боевой.

Ключи храните только в `.env`. Файл `.env` в git **не попадает**.

Перезапуск:

```bash
docker compose up -d --force-recreate shottrader
```

---

## Что видно в интерфейсе

### ShotCore (`:4861`)

- активные рынки;
- рекомендации **BUY D** и **SHORT D** (разные дистанции на разные стороны);
- TP не ниже 0.3%;
- лента прострелов.

### ShotTrader (`:4863`)

- таблица ордеров онлайн (цена, BUY/SHORT, дистанции, сумма в сделке, +/− за сегодня);
- слева: размер ордера, плечо, авто-стоп, Panic;
- справа: план ShotCore, отчёт за 7 дней, журнал сделок (время, сторона, D, PnL).

В полночь (по `TZ`) статистика «сегодня» обнуляется, предыдущие дни остаются в отчёте.

---

## Порты

| Сервис | Порт |
|---|---|
| ShotCore | **4861** |
| ShotTrader | **4863** |

Сменить снаружи можно через `WEB_PORT` / `TRADER_PORT` в `.env` (внутри контейнера порты остаются 4861 и 4863).

Фаервол Linux:

```bash
sudo ufw allow 4861/tcp
sudo ufw allow 4863/tcp
```

---

## Данные на диске

Не удаляются при `docker compose down`:

```
data/shots.csv                 прострелы ShotCore
data/mt_plan.json              актуальный план для терминала
data/trader_journal.jsonl      сделки ShotTrader
data/daily_reports.json        отчёты по дням (7 суток)
logs/                          логи
```

История при обновлении кода **сохраняется**.

---

## Обновление с GitHub

```bash
cd <репозиторий>
git pull
docker compose up -d --build --force-recreate
```

Без `--build` может остаться старый образ.

Python:

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
# остановить старые процессы и снова:
python -m shotcore
python -m shottrader
```

---

## Если что-то не работает

**Страница не открывается с другого компьютера**  
В `.env`: `WEB_HOST=0.0.0.0`. Откройте порты 4861 и 4863.

**ShotTrader пустой, нет клонов**  
Смотрите логи терминала. Частые причины:
- ShotCore ещё не запущен или недоступен по `SHOTCORE_URL`;
- `401 unauthorized` — задайте одинаковые `WEB_TOKEN` (ShotCore) и `SHOTCORE_TOKEN` (ShotTrader);
- план пуст — нет пар с рекомендацией (мало прострелов или win rate &lt; 70%).

**Контейнер постоянно Restarting**

```bash
docker compose logs --tail=100 shotcore
docker compose logs --tail=100 shottrader
```

Чаще всего нет `.env` или нет сети до OKX.

**Нет пар на дашборде**  
Слишком жёсткий объём `QAV_24H_MIN` или опечатка в `WHITELIST`. Формат только `BTC-USDT-SWAP`, не `btcusdt`.

**После git pull ничего не изменилось**  
Нужен `--build`. Обновите страницу в браузере через Ctrl+F5.

---

## Состав репозитория

```
.env.example              настройки ShotCore (скопировать в .env)
shottrader/.env.example   настройки ShotTrader
docker-compose.yml        оба сервиса одной командой
config.yaml               запасные значения, если ключа нет в .env
requirements.txt
shotcore/                 разведка + дашборд
shottrader/               терминал
mbauth/                   логин / LDAP
```

Файл `.env` и `shottrader/.env` в репозиторий не коммитить — там могут быть пароли и ключи биржи.

В корне лежит **`.gitignore`**: git сам не возьмёт секреты, ключи, логи и папку `data/`. В репозиторий попадают только шаблоны `.env.example`. Перед `git add -A` всё равно гляньте `git status` — в список не должны попасть `.env`, ключи OKX и журналы сделок.
