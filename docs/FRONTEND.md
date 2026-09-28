# Frontend и realtime (фаза 6)

Код: `frontend/` (React 18, TS 5, Vite 7, TanStack Query, Zustand, React Router v6, Tailwind v4), backend
`api/ws.py` (WebSocket), `platform/realtime.py` + `workers/realtime.py` (fan-out integration events → UI).
Gate — `docs/backend/gate-6.out`, отчёт Lighthouse — `docs/frontend/lighthouse-live.html`.

## 1. Принципы → реализация

| Принцип (§1) | Где |
|---|---|
| Типы из OpenAPI, не руками | `npm run gen:api` (`openapi-typescript` → `src/api/schema.d.ts`), `openapi-fetch` типизирован путями; `npm run check:api` в CI — расхождение с `docs/openapi.json` ломает сборку (демонстрация в gate: переименование поля → `tsc` падает) |
| Видимость — не граница безопасности | `hasPermission()` только прячет кнопки (`SessionPanel`, меню); E2E `rbac.spec.ts`: viewer — кнопки нет **и** API 403 |
| TanStack Query для серверного, Zustand для клиентского | `src/api/hooks.ts` — все серверные данные; Zustand — токен в памяти, выбранная площадка, язык |
| Оператор в стрессе | Live Parking: крупные цели (кнопки ≥ 44 px), лента, занятость цифрами, состояния цветом **и** текстом, подтверждение только у ручного открытия шлагбаума; коррекция номера — поле уже в фокусе, исходное распознавание рядом (I-16), reason обязателен |

## 2. Экраны

MVP: Login, Live Parking (лента, поиск по части номера, занятость, здоровье камер/шлагбаумов, вкладка «требуют
внимания» со счётчиком, карточка сессии: фото, коррекция, ручное открытие, наличные), Sessions (фильтр по статусу,
таймзона площадки), Payments (смены кассира — открыть/закрыть с расхождением, поиск сессии, приём наличных: сумма
приходит с бэкенда `amount.formatted`, оператор её не вводит). Плюс Dashboard, Cameras, Barriers, Access, Users
(списки). Остальные экраны каталога — после MVP (§2). Ошибки — по `error.code` с человеческим текстом (ru/uz) и
`correlation_id` с кнопкой «Копировать» (`ErrorBox`). Скелетоны на загрузке, пустые состояния, оптимистичных
обновлений нет (платежи и шлагбаум — только по ответу сервера). Локализация ru/uz (`src/i18n`, ~1 КБ, без i18next),
даты — `Intl.DateTimeFormat` в `lot.timezone`.

## 3. WebSocket `/ws/v1/lots/{lot_id}`

Сервер (`api/ws.py`, `LotSocketSession`): `accept` → первым сообщением `{"type":"auth","token"}` (5 с; токен не в
query string — не попадёт в логи прокси) → `container.auth.authenticate` + `lots.get(user, lot)` (чужой tenant / вне
скоупа → 4404) → подписка только на Redis-канал `ws:{tenant}:{lot}` → `ready`. Heartbeat: сервер `ping` каждые
20 с, ждёт любой ответ 40 с; клиентский `ping` → `pong`. Переавторизация: за 60 с до `exp` — `auth_expiring`,
клиент шлёт новый `auth` (ответ `auth_ok`), без разрыва; истёк + 30 с без нового — 4401. Невалидный повторный
`auth` → `error`, соединение живо; другой пользователь → 4403.

События: `ENTRY`, `EXIT`, `SESSION_CREATED`, `SESSION_UPDATED`, `SESSION_CLOSED`, `PAYMENT`, `BARRIER`, `OCCUPANCY`,
`CAMERA_STATUS`, `ALERT` — стрим-консьюмер (`workers/realtime.py`, потребитель `ws-fanout`) публикует их в Redis
Pub/Sub из integration events после доменного обработчика.

**Бэкпрешер (политика §4, `platform/realtime.py`):** очередь на соединение 256; `OCCUPANCY` и `CAMERA_STATUS`
схлопываются до последнего значения по ключу (площадка/камера) — промежуточные терять безопасно; `ENTRY`/`EXIT`/
`PAYMENT`/`BARRIER` при переполнении дропаются с уведомлением `GAP {dropped}` — клиент перезапрашивает ленту;
`ALERT` и `SESSION_*` **не дропаются никогда** — при переполнении соединение закрывается 1013, клиент переподключается
и перезапрашивает состояние: потеря не бывает молчаливой. Тест: `tests/api/test_ws.py::test_backpressure_policy`.

Клиент (`src/realtime/socket.ts`): auth первым сообщением, `pong` на `ping`, `auth` на `auth_expiring`,
переподключение с экспоненциальным бэкоффом и джиттером (1 с → 30 с), индикатор состояния (`ConnectionIndicator`:
онлайн / переподключение / «данные могут быть устаревшими»), после `ready` при переподключении — `onResync()`:
инвалидация занятости, сессий, очереди review, камер (перезапрос, не «возобновление потока»). Тесты:
`socket.test.ts` (vitest) и E2E `ws-reconnect.spec.ts` (обрыв через `routeWebSocket` → stale → reconnect → новый
запрос `/occupancy`).

## 4. Безопасность (§5)

Access-токен в памяти (Zustand), refresh — httpOnly cookie `parking_refresh` (`SameSite=Strict`, `Secure` вне dev,
path `/api/v1/auth`), один origin через прокси; авто-refresh с дедупликацией параллельных 401 (`client.test.ts`);
логаут — `POST /api/v1/auth/logout` (отзыв семейства) и очистка состояния. CSP `default-src 'self'; script-src 'self';
style-src 'self'; …` без `unsafe-inline`/`unsafe-eval` (сборка Vite без инлайн-скриптов, Tailwind — внешний CSS,
инлайн-`style` запрещён eslint-правилом; Radix/shadcn не используются именно из-за инлайн-стилей — диалог и вкладки
свои, с ARIA), HSTS, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options`. Заголовки
отдаёт `vite preview` (E2E/Lighthouse) и `nginx.conf` (prod). Секретов в бандле нет: `detect-secrets` по `dist/`
(A-37: gitleaks-бинарь недоступен на стенде, в CI — gitleaks-action).

## 5. Gate (см. gate-6.out)

`npm run build` (gen:api + tsc + vite) чист; eslint/tsc чисты; vitest 6; Playwright 5 сценариев против реального
стека (`backend/scripts/e2e_stack.py`: отдельная БД, uvicorn, publisher, consumer — площадка A2 без шлагбаума, сага
закрывает сессию после наличных): полный операторский поток, обрыв WS, RBAC viewer, изоляция tenant B на всех
экранах и в API (404), CSP/заголовки/инлайн. Lighthouse Live Parking (desktop preset, стенд 2 vCPU, benchmarkIndex
≈ 400): performance 98, accessibility 100, best-practices 100.
