# Устройства, ANPR-приём, оффлайн-режим, медиа (фаза 4)

Код: `modules/devices` (камеры, ключи, шлагбаумы, драйверы, здоровье), `modules/anpr` (приём, оффлайн-буфер, replay,
DLQ), `modules/media` (ключи объектов, presigned URL), `api/v1/camera.py`, `api/v1/devices.py`, `api/v1/admin.py`.
Gate — `docs/backend/gate-4.out`. Допущения фазы — A-26…A-31 в `ASSUMPTIONS.md`.

## 1. Camera API

`POST /api/v1/camera/events` (JSON ≤ 8 КиБ — `BodyLimitMiddleware`, 413), `POST /api/v1/camera/heartbeat`,
`POST /api/v1/camera/media/presign`, `POST /api/v1/camera/vendor/hikvision` (multipart XML + jpg),
`POST /api/v1/camera/vendor/dahua` (JSON с base64-кадром). Все — `X-Camera-Key`, в OpenAPI помечены
`x-public` + `x-camera-auth`. Tenant, площадка и камера определяются только по ключу; `camera_code` и `direction`
из тела сверяются с камерой ключа и **не расширяют права**.

### 1.1 Аутентификация (`devices/application/camera_auth.py`)

Ключ `<16 символов префикса><43 символа секрета>` (256 бит). Путь: префикс → кандидаты
(`app.lookup_camera_keys`, SECURITY DEFINER, UoW без tenant) → `HMAC-SHA256(pepper, ключ)` → `hmac.compare_digest`
с каждым кандидатом → `expires_at`/tenant active. Argon2/bcrypt запрещены: 200 RPS × 250 мс = мёртвый API, а
растягивать системный ключ с 256 битами энтропии не от чего.

Кеш кандидатов в Redis (`camauth:{prefix}`, 60 с, **включая отрицательный результат**) плюс суточная копия
`camauth:stale:{prefix}` — используется только когда PostgreSQL недоступен (оффлайн-режим, A-27). Неверный и
несуществующий ключ идут одним путём (кеш → один HMAC → compare_digest с кандидатом или фиктивным дайджестом)
и получают один ответ `401 UNAUTHENTICATED "invalid camera key"`; замер: медианы 0.317 vs 0.284 мс, отношение 1.12
(`tests/devices/test_camera_auth.py`). `last_used_at` — `HSET camkey:lastused`, флаш в PostgreSQL батчем ролью
app_bulk из джоба здоровья; неудачи считаются на IP (`camauth:fail:{ip}`, окно 60 с), на пороге 20 —
`camera_auth_abuse_total` + лог `camera_auth_abuse` уровня error (алерт фазы 7).

Ротация (A-17): `POST /api/v1/camera-keys/{id}/rotate` выпускает новый ключ, старому ставит `expires_at = now +
grace` (24 ч по умолчанию); отзыв — немедленно, с событием `devices.camera_key.revoked.v1` (обработчик сбрасывает кеш
на всех инстансах) и `after_commit`-инвалидацией на своём.

### 1.2 Бюджет (gate)

`scripts/bench_camera_auth.py` (in-process, генератор и API делят одно ядро стенда 2 vCPU):

| нагрузка | p50 запроса | p95 | p99 | `camera_auth_seconds` |
|---|---|---|---|---|
| 100 RPS × 10 с | 4.36 мс | 6.60 мс | 84 мс | 93 % < 2 мс, mean 2.0 мс |
| 200 RPS × 10 с | 4.05 мс | 58 мс | 259 мс | 89 % < 2 мс, 94 % < 5 мс, mean 6.6 мс |

Сама аутентификация — 0.67 мс последовательно (два Redis round-trip + HMAC); полный запрос — 3.5 мс CPU (FastAPI,
middleware, structlog). На 2 vCPU 200 RPS упирается в процессор, а не в аутентификацию: внешний uvicorn с двумя
воркерами на той же машине вместе с генератором выдал 72 RPS. Бюджет аутентификации (< 1 мс, без PostgreSQL на
горячем пути) выполнен; для 200 RPS в проде нужны ≥ 4 vCPU на API (C4 §1.2), что и заложено в sizing фазы 9.

### 1.3 Порядок валидации (от дешёвого к дорогому)

1. размер тела (middleware, 413) → 2. Pydantic (`confidence` 0–100, `event_type` ENTRY|EXIT) → 3. rate limit →
4. аутентификация → 5. камера активна (неактивные не попадают в кандидаты), направление = направлению камеры,
`camera_code` = коду камеры → 6. клоковый разбег → 7. replay → 8. номер (`PlateNumber.parse`) →
9. `image_object_key` по шаблону и принадлежности → 10. уверенность → 11. идемпотентность → 12. решение доступа →
13. одна транзакция.

**Rate limit 120/мин на ключ** (`RedisRateLimiter`, скользящее окно на ZSET, 429 + `Retry-After`): самый загруженный
въезд — машина в 3 с ≈ 20 событий/мин, с повторами прошивки и дублями ×2 — 40; 120 оставляет трёхкратный запас и
режет только шторм (петля ретраев, украденный ключ). Сброс буфера камеры после обрыва растягивается на минуты — такие
события и так `late`. Redis недоступен → лимит не применяется (приём важнее).

**Клоковый разбег** не отклоняет: `|event_time − now| > camera.max_clock_skew_seconds` (120) → событие принимается,
`clock_skew_seconds` пишется в `anpr_event`, гистограмма `camera_clock_skew_seconds{lot}`, лог `camera_clock_skew`
(алерт на систематическое превышение — фаза 7). Gate: +10 мин → `ENTRY_CONFIRMED`, `clock_skew_seconds = 599`.

**Replay-окно N = `lot.late_event_window_hours`** (24 ч, A-13): старше — `422 REPLAY`. Связь с идемпотентностью:
`anpr_idempotency` хранится 7 суток > 24 ч + разбег, поэтому дубль внутри окна всегда ловит реестр, а вне окна событие
отклоняется здесь — двойной обработки нет ни в одном интервале. Между 10 мин (`camera_late_after_seconds`) и окном —
`late`: сессия задним числом, шлагбаум не трогаем (`LATE_ACCEPTED`), потому что машина давно проехала.

### 1.4 Политика уверенности

`>= confidence_min` (80) — обычная обработка; `[confidence_review_min, confidence_min)` (60–79) — сессия
`MANUAL_REVIEW`, шлагбаум не открывается; `< confidence_review_min` — `422 LOW_CONFIDENCE`, событие пишется в
`anpr_event` как `REJECTED` с `image_object_key` (изображение сохранено для разбора).

### 1.5 Идемпотентность и транзакция

`idempotency_key = SHA256(tenant_id ‖ camera_id ‖ event_type ‖ plate_hash ‖ floor(event_time, с))`. Источник истины —
PK `anpr_idempotency (event_second, idempotency_key)`; Redis `anpr:idem:{key}` (24 ч) — только ускоритель повторов.
Одна транзакция (`IngestService._commit`): `SELECT` реестра без блокировки → `pg_advisory_xact_lock(hash(key))` →
повторный `SELECT` → `parking.process_entry_in` / `start_exit_in` → `INSERT anpr_event` → `INSERT anpr_idempotency`
со снимком ответа. Параллельные дубли ждут на advisory-lock, а не работают; решение доступа считается **до** транзакции
(вложенное соединение из пула внутри транзакции при 100 параллельных запросах заклинивает пул — первая версия
поймала это тестом). Повтор → тот же ответ, `duplicate: true`, **200**. Gate: 100 параллельных → 1 сессия,
1 оригинал + 99 дублей, 1 строка `anpr_event`.

Ответ `{status, session_id, barrier_command_id, saga_id, event_id, correlation_id, duplicate, queued}` отдаётся после
коммита, не дожидаясь шлагбаума: команда исполняется воркером по `devices.barrier.command_requested.v1`.

## 2. Heartbeat и здоровье камер

`POST /api/v1/camera/heartbeat` → только `HSET camera:hb:{id}` + TTL 1 ч (204; Redis недоступен → 503).
`CameraHealthJob.run_once()` (Celery beat `parking.maintenance.camera_health` раз в 30 с или
`main_camera_health`): читает активные камеры ролью app_maintenance, heartbeat/последние события из Redis,
считает `compute_status`: ONLINE < 2×интервал, DEGRADED < 5×, OFFLINE иначе; **SILENT** — heartbeat свежий, но
событий нет дольше 3× базовой линии часа суток (EWMA межсобытийного интервала `camera:baseline:{id}:{hour}`,
обновляется на каждом принятом событии) — «жива, но не распознаёт». Флаш `last_heartbeat_at`/`last_event_at`/
`camera_api_key.last_used_at` — один `executemany` на tenant ролью app_bulk (GRANT на колонки); смена статуса —
редкая, пишется app_critical вместе с `devices.camera.status_changed.v1` в outbox; OFFLINE логируется как error
(алерт). Gate: UNKNOWN→ONLINE→OFFLINE→SILENT в `tests/devices/test_health.py`.

## 3. Шлагбаумы

Порт `BarrierDriver.open/close(barrier, command_id)`; реализации `HttpRestDriver` (`POST {endpoint}/open|/close`,
`Authorization: Bearer <секрет по auth_secret_ref>`, `X-Command-Id`, 2xx = успех, редирект = отказ) и
`ModbusTcpDriver` (Modbus TCP FC05 Write Single Coil, импульс `pulse_ms` ON→OFF; endpoint
`host[:port][?unit=&open_coil=&close_coil=&pulse_ms=]`). Секреты — `SecretProvider`: dev/staging из
`PARKING_SECRETS_JSON`, prod — Vault (фаза 9; `HardenedSettings` запрещает JSON). Fake — только в tests/.
Третий драйвер — `HikvisionIsapiDriver` (`HIKVISION_ISAPI`): реле самой ANPR-камеры Hikvision, см. §3.4.

### 3.1 Надёжность

Таймаут 3 с; **OPEN ретраится** до 3 раз (tenacity, 0.5/1/2 с + джиттер): открыть открытое — no-op.
**CLOSE не ретраится**: повтор может опустить стрелу на машину; неудача — FAILED, оператор решает. Circuit breaker
на шлагбаум (`platform/breaker.py`, A-26): 5 отказов → OPEN 60 с → HALF_OPEN одна проба; при открытом breaker команда
завершается FAILED без обращения к контроллеру (сага → MANUAL_REVIEW). Лог из gate:
`barrier_attempt_failed ×5 → barrier_breaker_state closed→open → barrier_circuit_open → … open→half_open →
half_open→closed`. Каждая команда — `barrier_command` (PENDING до отправки, результат после, разными транзакциями,
I-9) и аудит для MANUAL.

### 3.2 SSRF (`devices/domain/endpoint.py` + `infrastructure/endpoint_resolver.py`)

Аллоклист подсетей — на площадке (`parking_lot.barrier_cidr_allowlist`), не на tenant. При сохранении endpoint
(`POST/PATCH /api/v1/barriers`) и при каждом вызове драйвера: разбор → резолв DNS → проверка всех адресов:
блок-лист всегда (127/8, 169.254/16 — метаданные облака, 100.64/10, 0/8, multicast, ::1, fe80::/10, fd00::/8,
ff00::/8); приватные (10/8, 172.16/12, 192.168/16, fc00::/7) — только внутри аллоклиста; публичные — можно.
Подключение — к проверенному IP (`Host`/SNI = исходное имя), не к имени: DNS rebinding между проверкой и подключением
исключён. Редиректы выключены, HTTP-клиент — собственный `make_async_client` (не тот, что к платёжному провайдеру).
Gate: `169.254.169.254` → 422, приватный вне аллоклиста → 422, домен → приватный IP → 422.

### 3.3 Безопасность

I-8: blacklist → `ENTRY_DENIED`, сессия `MANUAL_REVIEW`, `barrier_command` не создаётся (проверка в коде parking, не
в конфигурации). I-15: `POST /api/v1/barriers/{id}/open` без `reason` (≥ 3 символов) → 422; с reason — команда
MANUAL с `issued_by`, аудит `BARRIER_OPEN_MANUAL` в той же транзакции, 202 с `command_id`.

### 3.4 Камера Hikvision DS-TCG406-E без отдельного реле (`HIKVISION_ISAPI`)

У камеры въезда DS-TCG406-E есть свои **2 релейных выхода для стрелы** («2-ch relay output, supporting opening and
closing of barrier gates»), 3 входа (петля, датчик положения стрелы), RS-485, Wiegand и API **ISAPI** с
Digest-авторизацией; режим управления стрелой «By Camera / Platform / Mixed» (паспорт Hikvision). Поэтому реле Modbus
не нужно: сервер решает, открывать ли (оплата, абонемент, чёрный список), и командует камере, камера замыкает свой
выход, стрела APOLLO поднимается.

**Как работает в системе**
- События: камера сама шлёт распознанные номера на сервер (HTTP-уведомления, multipart: `anpr.xml` + кадр).
  Камера не умеет добавлять свой заголовок, поэтому ключ камеры — последним сегментом адреса:
  `POST /api/v1/camera/vendor/hikvision/<ключ камеры>`. В журнал запросов пишется шаблон роута, в трассы и Sentry —
  маска; в журналах ingress/прокси путь нужно маскировать так же (они видят сырой URL) — nginx локальной установки (`frontend/nginx.conf`) пишет `…/hikvision/***` и доводит событие до API, даже если камера оборвала соединение. Служебные события
  (`heartBeat` и др.) и проезды без номера получают `200 IGNORED`, чтобы камера не повторяла отправку. Тип события —
  по назначению камеры в системе (въезд/выезд): камера каждой полосы видит подъезжающие машины как `forward`.
- Стрела: шлагбаум с драйвером «Камера Hikvision», адрес `http://<IP камеры>[:порт]`, логин и пароль камеры.
  Открыть — `PUT /ISAPI/Parking/channels/1/barrierGate` с `<BarrierGate><ctrlMode>open</ctrlMode></BarrierGate>`;
  закрыть — `ctrlMode=close`; «Проверить связь» — `GET /ISAPI/System/deviceInfo` (модель, прошивка, серийный номер;
  стрела не двигается, заодно проверяется пароль). Для камер без раздела Parking — `?mode=io&output=1&pulse_ms=500`:
  импульс выхода тревоги `PUT /ISAPI/System/IO/outputs/1/trigger`. Параметры адреса: `channel`, `mode`, `output`,
  `pulse_ms`.
- Логин/пароль камеры хранятся в `barrier.credentials_encrypted` — AES-256-GCM ключом клиента (как секреты вебхуков,
  `platform/secret_box.py`), назначение `barrier:<id>` входит в AAD (шифротекст нельзя переставить на другой
  шлагбаум); API их не возвращает (`has_credentials`), меняются только парой. Остальное — как у других драйверов:
  SSRF-политика с подсетями площадки, подключение к проверенному IP, без редиректов, таймаут 3 с, повторы OPEN,
  circuit breaker.

**Подключение на площадке (монтажнику)**
1. Провода (220 В шлагбаума перед работой отключить). Колодка камеры **RELAY OUT** под задней крышкой: `2B 2A 1B 1A`
   — сухие контакты реле, полярность не важна. **Два провода 0,5 мм²**:
   - `1A` → вход «открыть» платы, `1B` → общий провод входов платы: у одних плат это `GND`, у других `+12V` —
     проверять по плате (ниже) или мультиметром: если на входе «открыть» относительно `GND` около 0 В, вход
     срабатывает от `+12V`;
   - **реле 2 (`2A`/`2B`) к входу «закрыть» НЕ подключать.** Камера, увидев машину, сама замыкает реле 2 по своим
     правилам — плата получает «открыть» от сервера и сразу «закрыть» от камеры, стрела остаётся на месте (MediaPark,
     2026-10-12: ручное «Открыть для проверки» поднимало стрелу, а при въезде — нет; без провода 关 заработало).
     Сервер шлёт только «открыть»; закрывает плата сама — по таймеру автозакрытия или по петле/фотоэлементу;
   - плата «DC Brushless Motor Barrier controller V3.0» (нижний ряд): `OP` — открыть, `CL` — закрыть, `GND` — между
     `ST` и `VD`; левая колодка `12V GND COM NC NO` — не трогать (`COM/NC/NO` — выход реле самой платы);
   - плата «digital system», 24V10A, два табло (MediaPark, проверено 2026-10-10): правая нижняя колодка из 8 клемм,
     сверху вниз: `12V`, 开 (открыть), 关 (закрыть), 停 (стоп), `GND`, 红外 (фотоэлемент), 地感 (петля), `12V`.
     **Входы 开/关 срабатывают от `+12V`, не от `GND`** (замыкание 开 с `GND` стрелу не двигает): `1A` → 开,
     `1B` → верхняя клемма `12V`; 关 свободна (см. выше). Кнопки под табло: 确认 — ОК, 关/+ — закрыть/плюс, 菜单 — меню,
     开/- — открыть/минус (меню и 确认 без нужды не трогать). Верхняя колодка 关到位/开到位 — выходы «закрыто/открыто
     до конца», для камеры не нужны;
   - проверка платы до камеры: на секунду соединить «открыть» с общим проводом этой платы (у «digital system» — 开 и
     верхняя `12V`; у V3.0 — `OP` и `GND`) — стрела откроется. `12V` никогда не соединять с `GND` — это короткое
     замыкание. Если стрела после проезда не опускается сама — включить автозакрытие по таймеру в меню платы (или
     поставить петлю/фотоэлемент); реле 2 камеры для этого не использовать (закрыло бы и над машиной).
2. Питание камеры — 12–24 В DC или PoE 802.3at (по паспорту, потребление до 10 Вт); инжектор/коммутатор — с поддержкой 802.3at.
3. В веб-интерфейсе камеры:
   - режим управления стрелой — **Platform** (стрелу открывает сервер). В режимах Camera/Mixed камера откроет стрелу
     сама по своему списку разрешённых, мимо оплаты и чёрного списка;
   - распознавание — только **Forward**, чтобы машина, сдающая назад от стрелы, не давала событие;
   - **страна номеров — Узбекистан**: «Настройки → Захват → Параметры съемки → Настройка распознавания →
     Страна/Регион» = «Все» + «Узбекистан». С заводским «Европа» узбекские номера не читаются: камера шлёт проезд без
     номера, сервер его пропускает (`200 IGNORED`), в «Live» пусто (MediaPark, 2026-10-10);
   - отправка событий — адрес сервера, порт и URL из памятки «Устройства → камера → ключ»
     (`/api/v1/camera/vendor/hikvision/<ключ>`), протокол HTTP, кадр номера, аутентификация — нет. Пункт меню зависит
     от прошивки: на V5.4.0 — «Сеть → Подключение данных → Проверка ISAPI → ISAPI1», на других — «Сеть → Расширенные →
     HTTP-прослушивание» или «События → Сервер тревог»;
   - **«Проверка ответа платформы» — включить**: без неё камера (cpp-httplib/0.9) закрывает соединение через ~30 мс,
     не дожидаясь ответа, и не узнаёт, принято ли событие. Сервер такое событие всё равно обрабатывает
     (`api/middleware/eager_body.py`: uvicorn после обрыва отдаёт `http.disconnect` вместо уже полученного тела, поэтому
     тело запросов `/api/v1/camera/` читается сразу, до проверки ключа);
   - **время**: «Система → Настройки системы → Настройки времени» — NTP (`pool.ntp.org`, интервал 60 мин), часовой пояс
     GMT+05:00, летнее время выключить. Новая камера стартует с 1970-01-01, а событие старше окна площадки (24 ч)
     отклоняется как повтор;
   - отдельный пользователь камеры с правами оператора (не admin) — его логин и пароль вводятся в карточке шлагбаума.
4. В системе: камера (производитель Hikvision) → ключ; шлагбаум «Камера Hikvision» → IP камеры, логин, пароль, «Какая
   камера открывает» → эта камера; подсеть камер (например `192.168.1.0/24`) — в «Подсетях контроллеров стрел»
   площадки; «Проверить связь» должна показать `DS-TCG406-E · <прошивка> · <серийный номер>`.

**Сеть.** Сервер должен достучаться до камеры (команда «открыть»), а камера — до сервера (события). При локальном
запуске (`START.bat` на компьютере площадки) это одна сеть. Если сервер в облаке — площадка подключается по VPN
(подсеть площадки в аллоклисте), пробрасывать порт камеры в интернет нельзя.

**Без железа.** `backend/scripts/fake_hikvision_camera.py` — имитатор камеры: ISAPI с Digest, стрела, состояние на
`/state`; `… push <URL с ключом> <номер>` шлёт событие точно в формате камеры. Тесты: `tests/devices/test_hikvision_isapi.py`
(драйвер против имитатора: Digest, открыть/закрыть, режим io, неверный пароль, отказ, SSRF, подмена шифротекста),
`tests/api/test_hikvision_camera.py` (секреты не возвращаются, ключ в пути, heartBeat, камера выезда),
`tests/api/test_camera_disconnect.py` (настоящий uvicorn, клиент закрывает соединение сразу после отправки, как камера:
контрольный сигнал и событие не теряются).

**Проверено на настоящей камере** (DS-TCG406-E, прошивка V5.4.0 build 250425, 2026-10-09): доставка на сервер с ключом в
пути; контрольный сигнал — multipart с частью `heartBeat.xml` (`EventNotificationAlert`, пространство имён
`http://www.isapi.org/ver20/XMLSchema`) раз в «Интервал контрольных сигналов», камера по нему становится ONLINE (джоб
`camera_health`; на стенде `scripts/e2e_stack.py` запускает его воркером `health`); тело команды стрелы совпадает с
кнопкой «Открыть» веб-интерфейса камеры (`entranceExit.js` прошивки).

**Ещё не проверено:** формат настоящего события распознавания (части multipart, имя кадра), «Проверить связь» и
«Открыть для проверки» через драйвер. Если камера ответит на команду `notSupport` — включить `?mode=io` (выход тревоги).

## 4. Оффлайн-режим (PostgreSQL недоступен, API жив)

Переключение — по circuit breaker пула БД (`db_breaker`: 5 ошибок соединения → OPEN 10 с → проба), не по таймауту
каждого запроса; классификатор `is_connectivity_error` отделяет ошибки соединения от ошибок данных. Путь:
событие проходит все проверки → `XADD anpr:offline` полного конверта (`event_id`, `correlation_id`, `received_at`,
`retry_count`, `tenant_id`, `lot_id`, `camera_id`, `idempotency_key` — **вычислен на приёме**, тело, `skew_seconds`,
`late`, `confidence_class`, `barrier_opened_offline`) → **202** `{status: QUEUED, queued: true}`. `XLEN ≥
PARKING_OFFLINE_STREAM_MAXLEN` (1 000 000) или XADD не удался → **503** (приём остановлен честно, не молча);
≥ 80 % — лог error `offline_buffer_near_capacity`; `MAXLEN ~ 1.2×` как жёсткий потолок. Аутентификация в оффлайне —
по суточной копии кандидатов (A-27). Шлагбаум (A-10): ENTRY и бесплатные EXIT (кеш решения: SUBSCRIPTION/VIP/WHITELIST
100 %) открываются напрямую драйвером по кешу шлагбаума камеры (`barrier:cache:{lot}:{camera}`, заполняет
исполнитель), blacklist из кеша — не открывать; факт уходит в конверт и при replay — в аудит `BARRIER_OPEN_OFFLINE`.

`OfflineReplayer` (`main_offline_replayer`): consumer group `offline`, батч ≤ 1000: XAUTOCLAIM зависших (idle ≥ 60 с)
→ собственные незавершённые (`0`) → новые (`>`), батч **сортируется по `event_time`**, а не по порядку в стриме:
EXIT может приехать раньше ENTRY — parking допускает сессии задним числом (EXIT без сессии → `entry_inferred`,
поздний ENTRY с `event_time` раньше `exit_time` присоединяется, A-10). Replay идёт с `late=True`
(`queued_offline = true`, шлагбаум не трогаем — он уже открыт на месте). Ошибка соединения → без ACK, экспоненциальная
пауза 2…64 с; ошибка данных → `retry_count+1` в хвост, после 3 — `anpr:dlq` (`dlq_messages_total{stream}`).
ACK удаляет запись (XDEL): XLEN отражает невоспроизведённый хвост. Дедупликация — тем же `anpr_idempotency`:
gate `pg_ctl stop` → 10 событий + 1 дубль → 11×202 → `pg_ctl start` → processed=11, duplicates=1, +10 `anpr_event`,
+10 сессий, 10 аудитов оффлайн-открытий. Consumer убит → XAUTOCLAIM подобрал 5/5.

DLQ-админ: `GET /api/v1/admin/dlq?stream=anpr|events`, `POST /api/v1/admin/dlq/replay {stream, ids?, dry_run,
reason}` — роль `dlq:replay` (оператор платформы), номера маскируются, аудит `DLQ_REPLAY[_DRY_RUN]`; replay
возвращает запись в исходный стрим с `retry_count = 0` и удаляет из DLQ.

Метрики: `offline_mode_active`, `offline_queue_length`, `offline_replayed_total{result}`, `redis_stream_lag{stream}`,
`dlq_messages_total{stream}`, `camera_event_total{result}`, `camera_event_rejected_total{reason}`,
`camera_clock_skew_seconds{lot}`, `camera_auth_failed_total`, `camera_auth_abuse_total`, `camera_auth_seconds`,
`camera_status_total{status}`, `barrier_command_total{command,result}`.

## 5. Media

Ключ объекта только `tenant/{t}/lot/{l}/camera/{c}/YYYY/MM/DD/{uuid}.jpg` (`media/domain/object_key.py`, CHECK в
`media_object`), принадлежность — против ключа камеры; иначе `422 INVALID_IMAGE_KEY` (path traversal и чужие
изображения). Загрузка — **presigned POST прямо в MinIO** (`POST /api/v1/camera/media/presign`: ключ навязан
сервером, `Content-Type: image/jpeg` зафиксирован политикой, `content-length-range 1..8 MiB`, TTL 300 с);
gate против реального MinIO: 204 / oversize 400 EntityTooLarge / исполняемый тип 403 / подмена ключа 403.
Просмотр — `GET /api/v1/sessions/{id}/images` → presigned GET (TTL 300 с), tenant и скоуп площадок проверяются по
сессии. Vendor-адаптеры кладут кадр сами (`put_object`) — камеры Hikvision/Dahua не умеют presigned POST.
Lifecycle бакета `anpr-media` применён (`scripts/minio_dev.py setup`, `mc ilm rule ls` в gate): 90 дней, старые
версии 7 дней.

## 6. Матрица отказов фазы

| Отказ | Поведение |
|---|---|
| PostgreSQL недоступен | breaker → 202 + `anpr:offline`; шлагбаум по кешу; replay после восстановления без дублей |
| Redis недоступен | rate limit и кеши пропускаются (лог warning), приём через PostgreSQL продолжается; heartbeat → 503; PostgreSQL + Redis недоступны → 503 |
| Контроллер шлагбаума молчит | 3 попытки OPEN → FAILED → сага MANUAL_REVIEW; breaker 60 с; оператор — вручную с reason |
| Камера с битыми часами | принимается, помечается, метрика/алерт; ничего не останавливается |
| MinIO недоступен | presign не выдаётся (503 от aioboto3 → ExternalServiceError не оборачивается: событие без изображения принимается) |
| Стрим переполнен | 503 на приём, алерт с 80 % |
