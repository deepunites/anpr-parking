# Отчёт о готовности к production — фаза 10

**Дата:** 2026-09-20. **Вердикт:** система готова к **пилоту на 1–3 площадках с реальными камерами после выполнения
условий §6**; к запуску на 1000 площадок (профиль Medium) — только после staging-цикла (нагрузка, учения, реальный Vault/Sentinel),
потому что производительность на целевой нагрузке здесь **не подтверждена** (§2.1). Всё, что ниже, опирается на артефакты;
где артефакта нет — так и написано. Приложение А — чек-лист фазы 10 со ссылками по каждому пункту.

Среда, в которой всё проверялось: рабочая станция без root/Docker (PostgreSQL 16.2 pgserver + contrib, Redis 7, MinIO,
Patroni/PgBouncer/etcd-аналог raft, rootless podman, k6, Trivy/gitleaks/semgrep, helm/kubeconform/OpenTofu). Кластера Kubernetes
и staging-контура **не было** (A-62) — это главный ограничитель доверия к §1.

## 1. Что работает (с подтверждающими артефактами)

| Область | Что именно | Подтверждение |
|---|---|---|
| Домен и архитектура | модульный монолит, DDD-слои, запрет кросс-модульных импортов и HTTP в транзакции (I-9) статически | `backend/tests/architecture/test_imports.py`, `test_sql_fragments.py`; `docs/backend/gate-10.out` |
| Схема данных | 14 миграций up/down/up, партиции (`anpr_event`, `outbox`, `anpr_idempotency`), RLS на всех tenant-таблицах, append-only триггеры (`audit_log`, `payment`, `manual_correction`) | `backend/tests/db/test_migrations.py`, `test_partitions.py`, `test_roles.py`; gate-10 «alembic up/down/up» |
| Изоляция tenant | RLS под ролями приложения и под PgBouncer (transaction pooling, `set_config(..., true)`), матрица межтенантных запросов по всем эндпоинтам OpenAPI | `tests/db/test_pgbouncer_rls.py`, `tests/security/test_cross_tenant_matrix.py`, `tests/invariants/test_i02_tenant_isolation.py`, `frontend/e2e/tenant-isolation.spec.ts` |
| Camera API | ключи с pepper и постоянным временем сравнения, rate limit, окна времени, идемпотентность через advisory lock, оффлайн-буфер (202) при сбое БД, replay без дублей | `tests/devices/test_camera_auth.py`, `tests/security/test_timing.py`, `tests/invariants/test_i03_idempotency.py`, `test_i04_no_lost_events.py`; failure-тесты «pg outage/partition» — `docs/backend/gate-8.out` строки GATE |
| ENTRY/EXIT | ENTRY с политиками площадки (full, blacklist, low confidence), EXIT-сага с возобновлением после падения воркера, MANUAL_REVIEW при отказе шлагбаума после оплаты (ADR-12) | `tests/parking/test_entry.py`, `test_exit_saga.py`, `test_manual_operations.py`, `test_concurrency.py`, `tests/invariants/test_i01…i08`, failure «barrier dead», e2e «full path» (`gate-8.out`) |
| Тариф и деньги | детерминированный движок, целые деньги, покрытие 100 % и mutation 97.4 % на `tariff/domain` + `billing/domain` | `docs/backend/coverage-layers.md`, `docs/backend/mutation-report.md` |
| Платежи | наличные (смены кассира, расхождения при закрытии), Payme/Click вебхуки с подписью/replay-окном/inbox-дедупом, возвраты, сверка с провайдером, двойная оплата → очередь возврата | `tests/billing/` (webhooks, race, edge cases, refunds, reconciliation), `docs/PAYMENTS.md` |
| Идентичность | EdDSA JWT, ротация refresh с детекцией reuse (отзыв семьи), lockout, argon2id, RBAC-контракт по `x-permission` для каждого эндпоинта | `tests/security/test_hardening.py`, `tests/api/test_rbac_contract.py`, `tests/api/test_auth.py` |
| Шлагбаумы | ретраи/бэкофф, circuit breaker, SSRF-политика endpoint'ов с allowlist площадки, аудит ручных действий (I-15) | `tests/devices/test_barrier_reliability.py`, `test_endpoint_policy.py`, `tests/invariants/test_i15_manual_actions_audited.py` |
| События | outbox после коммита (I-5), Redis Streams с порядком на агрегат (шарды), DLQ + replay, супервизор циклов воркеров | `tests/invariants/test_i05_outbox_after_commit.py`, `tests/workers/`, failure «dlq replay», RUNBOOK учение 3 (`docs/backend/runbook-drills/`) |
| Медиа | presigned POST с политикой, namespace ключа камеры, lifecycle 90 дней, отложенная отправка при сбое MinIO, удаление всех версий при стирании ПДн | `tests/media/test_media.py`, `tests/security/test_hardening.py::test_image_object_key_cannot_point_outside_camera_namespace`, `tests/privacy/test_erasure.py` |
| Приватность | процесс запроса субъекта REQUESTED→…→COMPLETED с необратимой анонимизацией, тест необратимости (БД, дамп, логи, кеши, MinIO), финансы не меняются | `docs/PRIVACY.md`, `tests/privacy/test_erasure.py`, `alembic/sql/0014_up.sql` |
| Наблюдаемость | метрики с бюджетом кардинальности, алерты с unit-тестами promtool и runbook-якорями, дашборды, Loki-лейблы без ПДн, трассы с запретом атрибутов, Sentry `before_send`, корреляция end-to-end | `docs/OBSERVABILITY.md`, `deploy/prometheus/alerts_test.yaml`, `tests/observability/`, `docs/backend/metrics-cardinality.md` |
| PostgreSQL HA | Patroni + sync standby + PgBouncer: failover под потоком записей RTO 19.2 с; pgBackRest-эквивалент: restore-verify с `pg_amcheck` и контрольными суммами, PITR RTO 14.6 с (3.5 МБ) | `docs/backend/gate-9.out` (GATE patroni failover / backup verify / PITR), `deploy/postgres/lab/pgha_lab.py`, RUNBOOK учение 1 |
| Redis HA | Sentinel 2/3 на трёх ВМ (Terraform + Ansible), клиент и Celery по `redis+sentinel://`; учение: master убит, тот же клиент пишет через 2.5 с | `backend/tests/failure/test_redis_sentinel.py` (gate-10), `deploy/ansible/roles/redis`, `deploy/terraform/envs/*/main.tf::module.redis` |
| Контейнеры и кластер | образы alpine non-root с 0 уязвимостей Trivy, Helm-чарт (HPA, PDB, NetworkPolicy default-deny, probes, securityContext), kubeconform на трёх профилях values, Kyverno «только подписанные образы» | `docs/backend/gate-9.out`, `deploy/helm/parking`, `deploy/k8s/platform` |
| CI/CD | ci.yml (lint → typecheck → tests → invariants отдельно → oasdiff → security → образы), cd.yml (staging smoke → chaos kill → rollback → production с approval), drift.yml, nightly.yml | `.github/workflows/*.yml`, `scripts/rollback.sh`, `backend/scripts/smoke_staging.py` |
| Безопасность кода | bandit, pip-audit, npm audit, Trivy, gitleaks, semgrep — зелёные; исключения с `review_by` | `scripts/security_scan.py`, `security/exceptions.yaml`, gate-10 |
| Фронтенд | MVP-экраны (Login, Live Parking, Sessions, Payments) + Dashboard/Cameras/Barriers/Access/Users; Playwright 5 сценариев против реального стека (flow, rbac, security, tenant-isolation, ws-reconnect) | `docs/FRONTEND.md`, `frontend/e2e/*.spec.ts`, `docs/backend/gate-6.out` |
| Документация эксплуатации | 15 сценариев RUNBOOK в единой структуре, 3 учения с реальными логами; модель угроз с file:line; стоимость на три профиля | `docs/RUNBOOK.md`, `docs/THREAT_MODEL.md`, `docs/COST.md` |

## 2. Что не работает или не проверено в бою — и почему

1. **Производительность на целевой нагрузке не подтверждена.** Критерий ТЗ «200 RPS, p95 < 150 мс» на песочнице 2 vCPU
   не выполнен ни в одном сценарии (baseline p95 1652 мс, peak 9475 мс — `docs/backend/load/*.md`, `gate-8.out`). Причина
   измерена: ≈ 18 SQL и 40–60 мс CPU на событие при двух воркерах API и БД на том же ядре; часть устранена профилированием
   (`profile-before/after.md`: OpenTelemetry 13 % → 6 % CPU). **2026-09-21:** стоимость события снижена до 10 SQL на
   ENTRY и EXIT в одной транзакции (`docs/backend/sql-budget.md`); повтор `baseline` на той же песочнице — p95 1652 → 688 мс,
   EXIT end-to-end p95 9.6 → 4.3 с, критерий ТЗ на песочнице по-прежнему не достигается. Прогноз для production (3 узла × 8 vCPU, HPA до 24 подов):
   ≈ 10 соб./с на под при 60 % CPU ⇒ 200 соб./с достижимо по CPU, но **латентность p95 < 150 мс требует измерения на staging**
   (nightly.yml запускает k6 против staging — ещё ни разу не выполнялся). Функциональные критерии нагрузки выполнены:
   0 потерянных событий, 0 дублей сессий, RSS стабилен (soak).
2. **Kubernetes-часть не запускалась.** Кластера не было (A-62): манифесты валидированы статически (helm lint/template +
   kubeconform), образы собраны rootless podman и просканированы, но rolling update, HPA, NetworkPolicy, PDB, Kyverno и
   откат (`scripts/rollback.sh`) **живьём не исполнялись** — они выполняются джобами cd.yml при первом деплое staging.
3. **Учения RUNBOOK — 3 из 15 и на локальной лаборатории, не на staging** (A-71): Patroni с raft вместо etcd, `wal_archive.py`
   вместо pgBackRest, Redis без пароля. Команды остальных 12 сценариев проверены на копируемость, не исполнением.
4. **Vault и Sentinel — только против моков/локальных бинарников.** `VaultKeyProvider` проверен `httpx.MockTransport` (A-70);
   Redis Sentinel — локальные `redis-server`/`redis-sentinel` без TLS/пароля. Первый реальный контакт — staging.
5. **Реальные камеры и шлагбаумы не подключались** (A-02, A-03, A-29): Camera API принимает нормализованный JSON и presigned
   PUT; vendor-адаптер под конкретные модели (Hikvision/Dahua ISAPI/HTTP listening) — не написан, потому что модели не названы.
   Драйвер шлагбаума — HTTP REST и Modbus TCP по контракту A-29; ни один реальный контроллер не проверен.
6. **Платёжные провайдеры — по публичной документации, без сертификации.** Payme Merchant API и Click SHOP API реализованы по
   спецификациям и проверены тестами с воспроизведёнными форматами; sandbox-сертификация у провайдеров не проходилась (A-33).
7. **Правовая часть не подтверждена юристом** (A-67): сроки ответа, анонимизация вместо удаления, ретеншены 7 лет/400 дней/90 дней.
8. **Testcontainers-часть CI не исполнялась локально** (A-47): CI-джобы используют services PostgreSQL/Redis/MinIO —
   конфигурация написана, первый прогон покажет расхождения (порты, contrib-модули `btree_gist`/`amcheck` в образе postgres).
9. **Terraform `plan` против реального Proxmox не выполнялся** (A-66): `validate` + `tofu test` с mock-провайдерами.

## 3. Известные риски первого месяца

| Риск | Вероятность | Как обнаружим | Что делать |
|---|---|---|---|
| Латентность camera-событий выше 150 мс на пике — очереди на въезде | высокая | `CameraEventsLatencyHigh`, `ExitE2ESlow` (`deploy/prometheus/alerts.yaml`) | HPA `minReplicas` вверх, `PARKING_DB_POOL_SIZE`; SQL-бюджет события уже снижен (§4 п.1), дальше — CTE чтений EXIT по профилю staging |
| Формат событий реальных камер не совпадает с нормализованным JSON | высокая | `camera_event_rejected_total{reason="VALIDATION"}` растёт с первой площадки | vendor-адаптер (§4 п.2); до него — «ручной режим» площадки (A-01) |
| NetworkPolicy/CIDR staging не совпадают с фактической сетью — поды не видят БД/Redis/MinIO | средняя | readiness 503 сразу после деплоя, `smoke_staging.py` красный | править `values-staging.yaml networkPolicy.*`, A-62 |
| Sentinel failover под нагрузкой: 2–5 с ошибок Redis → часть кешей fail-open, 202 в буфер, `worker_loop_restarts_total` | средняя | `RedisDown` короткий, `worker_loop_restarts_total`, `anpr_offline_buffer_length` | ничего вручную; проверить replay (`RUNBOOK` 3) |
| Расхождения сверки с провайдером из-за неизвестных статусов/таймзон | средняя | `ReconciliationBacklog`, `reconciliation_discrepancies_open` | RUNBOOK 10; правка маппинга статусов |
| Часы камер уходят > skew → события помечаются, при > 24 ч отклоняются как REPLAY | средняя | `CameraClockSkew` | NTP на площадке; RUNBOOK `camera-clock-skew` |
| Хранилище кадров заполняется быстрее расчёта (кадры крупнее 300 КБ) | средняя | `DiskUsage` MinIO, COST §3 | ретеншен 30 дней или качество JPEG; A-68 |
| Первое учение восстановления на production-объёме > 15 мин | низкая | `backup_verify.sh` еженедельно, `BackupRestoreVerifiedStale` | delta restore, `process-max` (INFRA §4) |
| Сговор оператора и водителя (ручные открытия «по знакомству») | средняя | отчёт по `audit_log` (доля ручных действий по оператору) — дашборд *Business* | регламент заказчика; долг §4 п.5 |

## 4. Технический долг (с оценкой)

| # | Долг | Цена исправления | Цена бездействия |
|---|---|---|---|
| 1 | ~~Стоимость camera-события: ≈ 18 SQL/событие~~ **Сделано 2026-09-21** (`docs/backend/sql-budget.md`): 19/16 → 10/10 SQL, одна транзакция на ENTRY, кеши процесса площадки/шлагбаума/тарифа (TTL `reference_cache_ttl_seconds` = 10 с), ON CONFLICT вместо savepoint, outbox одним executemany. Остаток: k6 на staging (nightly.yml) и, по его профилю, CTE для чтений EXIT | 0 (сделано); staging — по A-62 | — |
| 2 | Vendor-адаптеры камер (`/api/v1/camera/vendor/{vendor}`): парсинг родного формата, загрузка кадра в MinIO от имени сервера | 3–5 дней на вендора (нужна камера в руках) | без него ни одна реальная камера не подключится |
| 3 | Ротация DEK/HMAC: версии ключей поддержаны в данных, но джоба перешифрования батчами (`enc_key_version` N → N+1) не написана | 3 дня | ротация ключа tenant = только для новых записей; компрометация ключа требует ручной миграции |
| 4 | Mutation testing только на `tariff/domain` и `billing/domain` (A-53); расширить на `parking/domain` (сага) и `access/domain` | 2–3 дня (+ время прогонов) | скрытые «выжившие» мутанты в логике саги |
| 5 | Контроль сговора: «четыре глаза» на ручное открытие/коррекцию (второй оператор или менеджер), аудит просмотра кадров (T-13, T-16 остаточный) | 3 + 2 дня | детектирование только постфактум (A-72) |
| 6 | Redis: TLS между подами и ВМ Redis (сейчас пароль + NetworkPolicy + VLAN) | 1–2 дня (сертификаты из `module.pki`) | трафик кешей/стримов в открытом виде внутри VLAN 21 |
| 7 | `docs/openapi.json`, `EVENTS.md` генерируются скриптами, но проверка «сгенерированное == закоммиченному» — только `npm run check:api` фронтенда | 0.5 дня (джоба в ci.yml) | рассинхрон контракта |
| 8 | Соль/pepper ключей камер один на инсталляцию (`camera_key_pepper`); ротация pepper = перевыпуск всех ключей | 2 дня (версия pepper в ключе) | при компрометации pepper — массовый перевыпуск ключей |
| 9 | Скрипты лаборатории (`pgha_lab.py`, `wal_archive.py`) дублируют то, что в production делает pgBackRest/etcd — двойная поддержка | 1 день (пометить как dev-only, вынести из production-документации) | путаница в RUNBOOK при отличии команд |
| 10 | Frontend: экраны каталога после MVP (Reports, Tariffs editor, Subscriptions UI) — API есть, UI нет | 8–10 дней | настройка тарифов/абонементов через API/скрипты |

## 5. Чего система не умеет (границы)

- **Автономная работа площадки без связи с ЦОД**: шлагбаумы не открываются автоматически, площадка в ручном режиме (A-01);
  edge-компонента нет.
- **Мультивалютность**: только UZS, целые тийины/сумы (I-13); курсы и иностранные карты — нет.
- **Онлайн-оплата водителем без оператора (QR у выезда, мобильное приложение)**: платёж создаётся по сессии через API
  оператора/кассы либо провайдером по заказу — публичного платёжного UI нет.
- **Распознавание номеров**: делает камера (A-02); сервер не обрабатывает видео/изображения, только принимает результат и кадр.
- **Уведомления людям**: только Telegram (A-15); SMS/e-mail/push — нет.
- **Мультирегиональность / DR во втором ЦОД**: три зоны одного ЦОД; катастрофа ЦОД = восстановление из офсайт-бэкапа
  вручную (RTO часы, не минуты).
- **Биллинг B2B (счета tenant'ам за платформу), CRM, интеграция с 1С/бухгалтерией**: вне скоупа; выгрузки — SQL-представления
  reporting (A-14).
- **Redis Cluster / шардинг PostgreSQL**: один primary; свыше ≈ 400 соб./с (A-63) требуется шардинг consumer'ов и, дальше,
  партиционирование по tenant — не проектировалось.
- **Управление парковочными местами (конкретное место, датчики занятости)**: только счётчик занятости площадки.
- **Абонементы с онлайн-продлением/автоплатежом**: абонемент создаётся оператором; автосписаний нет.

## 6. Условия запуска (до первой реальной машины на первой площадке)

Обязательно, в порядке выполнения:

1. **Staging-контур поднят и прошёл cd.yml целиком**: `smoke_staging.py --stack`, `chaos_kill_api.sh`, `rollback.sh`
   зелёные; NetworkPolicy проверены живьём (A-62). Артефакт: лог джоб cd.yml.
2. **Vault production**: политика/AppRole для `kv/parking/production/tenant-keys`, ключи tenant сгенерированы
   (`KeyProvider.generate_entry`), `VaultKeyProvider` стартует fail-fast (A-70). Артефакт: лог старта api `vault_keys_loaded`.
3. **Redis Sentinel и PostgreSQL Patroni на production-ВМ** развёрнуты Ansible, учения 1 и 3 RUNBOOK повторены на staging
   с production-топологией (etcd, pgBackRest) — RTO записаны в RUNBOOK часть C (A-61, A-71).
4. **Нагрузочный прогон k6 на staging** (`nightly.yml`): peak 200 соб./с; если p95 > 150 мс — остаток долга §4 п.1 (CTE чтений EXIT) до запуска
   на > 50 площадок; для пилота ≤ 3 площадок достаточно p95 < 1 с при 0 потерь.
5. **Камеры и шлагбаумы**: модели названы (A-02, A-03), vendor-адаптер написан и проверен на стенде с реальной камерой
   (событие → кадр в MinIO → сессия), драйвер шлагбаума проверен на реальном контроллере с SSRF-allowlist площадки.
6. **Платёжные провайдеры**: sandbox-сертификация Payme и Click пройдена (A-33), секреты мерчанта в Vault, вебхуки
   доступны только через ingress с rate limit; сверка запущена по расписанию.
7. **Юридическая проверка** (A-67): срок ответа субъекту, анонимизация, ретеншены, текст уведомления при инциденте
   (RUNBOOK 12) — подписаны юристом; `docs/PRIVACY.md` обновлён.
8. **Бизнес-политики площадки подтверждены заказчиком**: поведение при обрыве связи (A-01, A-10, A-28), blacklist (A-25),
   семантика тарифа (A-21), Telegram как канал (A-15) — значения выставлены в настройках площадки.
9. **Эксплуатация**: on-call назначен, Alertmanager маршрутизирует в Telegram/пейджер, дежурный прочитал RUNBOOK и выполнил
   один сценарий вручную на staging; еженедельный `backup_verify` включён и первый отчёт `backup_restore_verified_timestamp` есть.
10. **Бюджет** (A-68, A-69): КП провайдера хостинга и договоры с провайдерами оплаты заменили оценки COST.md.

Желательно до пилота, обязательно до масштабирования: долг §4 п.1 (стоимость события), п.3 (ротация ключей), п.5 (четыре глаза).

---

## Приложение А. Финальный чек-лист фазы 10 (со ссылками)

Условные обозначения: ✅ выполнено и подтверждено артефактом; ⚠️ выполнено частично/не в целевой среде — подробности в §2; ❌ не выполнено.

| | Пункт | Артефакт |
|---|---|---|
| ✅ | DDD-структура, архитектурный тест импортов зелёный | `backend/tests/architecture/test_imports.py`, `test_sql_fragments.py` → `docs/backend/gate-10.out` |
| ✅ | Схема БД, миграции применяются и откатываются | gate-10 «alembic upgrade head / downgrade base / upgrade head», `tests/db/test_migrations.py` |
| ✅ | RLS проверен под ролью приложения и под пулером | `tests/db/test_roles.py`, `tests/security/test_hardening.py::test_app_role_cannot_disable_or_bypass_rls`, `tests/db/test_pgbouncer_rls.py` (маркер `pooler`, PgBouncer 1.25 — gate-9) |
| ✅ | OpenAPI 3.1 генерируется, oasdiff в CI | `docs/openapi.json`, `.github/workflows/ci.yml` job `openapi-compat`, `backend/scripts/openapi_compat.py` |
| ✅ | REST API полный, RBAC на каждом эндпоинте | `tests/api/test_rbac_contract.py` (таблица из `x-permission` для всех операций), `docs/BACKEND.md` §API |
| ✅ | WebSocket с изоляцией tenant | `backend/tests/api/test_ws.py`, `frontend/e2e/tenant-isolation.spec.ts`, `ws-reconnect.spec.ts` |
| ⚠️ | Frontend, все экраны MVP + остальные | MVP + списки — `docs/FRONTEND.md` §2, `gate-6.out`; экраны каталога после MVP — долг §4 п.10 |
| ✅ | JWT, ротация refresh, детекция reuse | `tests/api/test_auth.py` (refresh rotation/reuse → revoke family, lockout), `tests/security/test_hardening.py::test_forged_jwt_variants_are_rejected` |
| ✅ | Multi-tenancy на двух уровнях | RLS (`alembic/sql/0008_up.sql`) + tenant из JWT (`api/deps.py`) + `lot_scope`; `tests/invariants/test_i02_tenant_isolation.py`, `tests/security/test_cross_tenant_matrix.py` |
| ✅ | Camera API, аутентификация, валидация, идемпотентность | `tests/devices/test_camera_auth.py`, `tests/anpr/test_ingest_api.py`, `test_offline.py`, `tests/invariants/test_i03_idempotency.py`, `tests/security/test_timing.py` |
| ✅ | ENTRY workflow | `tests/parking/test_entry.py`, `tests/invariants/test_i01_one_active_session.py`, `test_i08_blacklist_never_opens.py` |
| ✅ | EXIT-сага, возобновляемая | `tests/parking/test_exit_saga.py`, failure «barrier dead» и «pg outage» (`gate-8.out`), `parking.saga.recover` (celery_app) |
| ✅ | Tariff engine, покрытие ≥ 95 %, mutation ≥ 85 % | `docs/backend/coverage-layers.md` (100 %), `docs/backend/mutation-report.md` (97.4 %) |
| ✅ | Subscription, VIP, blacklist, whitelist | `tests/access/test_decide.py`, `test_resolver.py`, `tests/invariants/test_i08_blacklist_never_opens.py`, `docs/backend/access-bench.out` |
| ✅ | Payment lifecycle, возвраты, сверка | `tests/billing/test_online.py`, `test_refunds.py`, `test_reconciliation.py`, `test_webhooks.py`, `tests/invariants/test_i06_payment_immutable.py` |
| ✅ | Barrier: ретраи, circuit breaker, SSRF-защита, аудит | `tests/devices/test_barrier_reliability.py`, `test_endpoint_policy.py`, `tests/invariants/test_i15_manual_actions_audited.py` |
| ✅ | Redis Streams, DLQ, replay | `tests/workers/test_outbox_and_consumer.py`, failure «dlq replay» (`gate-8.out`), RUNBOOK учение 3 (часть C, скрипт `docs/backend/runbook-drills/drill_dlq.sh`) |
| ✅ | Outbox, гарантия at-least-once, порядок на агрегат | `tests/invariants/test_i05_outbox_after_commit.py`, `tests/workers/test_outbox_and_consumer.py`, `docs/EVENTS.md` |
| ✅ | MinIO, presigned, lifecycle, ретеншен 90 дней | `tests/media/test_media.py`, `backend/scripts/minio_dev.py setup` (вывод `mc ilm ls` в gate), `deploy/ansible/roles/minio` |
| ✅ | Audit log append-only | `tests/invariants/test_i11_audit_append_only.py`, `alembic/sql/0007_up.sql` |
| ✅ | GDPR/ЗРУ workflow, тест необратимости | `docs/PRIVACY.md`, `tests/privacy/test_erasure.py` (GATE-строка в gate-10) |
| ⚠️ | Prometheus, Grafana, Loki, Sentry, OpenTelemetry, Alertmanager | конфигурации и тесты — `deploy/prometheus`, `deploy/grafana`, `deploy/loki`, `tests/observability/`; стек как сервисы в кластере не запускался (A-62) |
| ⚠️ | PostgreSQL HA + автоматический failover (протестирован) | протестирован на лаборатории Patroni raft: RTO 19.2 с (`gate-9.out`), failure-lab RTO 10.4 с (`gate-8.out`); на etcd/staging — условие запуска 3 |
| ⚠️ | Redis HA | Sentinel: код + IaC + учение на локальных бинарниках (`tests/failure/test_redis_sentinel.py`, gate-10); на ВМ staging не разворачивался |
| ⚠️ | Бэкап + PITR + подтверждённое восстановление | verify (`pg_amcheck` + контрольные суммы) и PITR выполнены (`gate-9.out`), но на объёме 3.5 МБ и с `wal_archive.py` вместо pgBackRest (A-61) |
| ⚠️ | Kubernetes, Helm, Terraform | helm lint/template + kubeconform (3 профиля), образы Trivy 0, `tofu validate` + `tofu test` (моки) — `gate-9.out`, gate-10; кластер и `plan` против Proxmox — нет |
| ⚠️ | GitHub Actions, откат проверен | workflows написаны; `scripts/rollback.sh` проверен только синтаксически и на локальном стенде smoke — исполнение в cd.yml на staging (A-62) |
| ✅ | Сканеры безопасности зелёные | `scripts/security_scan.py` → gate-10 (bandit, pip-audit, npm audit, trivy, gitleaks, semgrep) |
| ✅ | Unit ≥ 80 % (по слоям — по таблице фазы 8) | `docs/backend/coverage-layers.md`: проект 90.9 %, все слои выше порогов |
| ✅ | Integration, E2E | `tests/` (реальные PostgreSQL/Redis/MinIO), `tests/e2e` (8 passed, `gate-8.out`), `frontend/e2e` (Playwright 5) |
| ❌ | k6: 200 RPS, p95 < 150 ms — отчёт | отчёт есть — `docs/backend/load/peak.md`: p95 9475 мс на песочнице 2 vCPU при ÷10 интенсивности; критерий **не выполнен**, см. §2 п.1 и условие запуска 4 |
| ✅ | Failure-тесты: все строки таблицы фазы 8 | `docs/TESTING.md` §5 (таблица ↔ `tests/failure/test_0*.py`), 15 passed (`gate-8.out`), + `test_redis_sentinel.py` (gate-10) |
| ⚠️ | 15 runbook'ов, 3 прогнаны на staging | 15 сценариев — `docs/RUNBOOK.md` часть A; 3 прогнаны с логами — часть C и `docs/backend/runbook-drills/`, **на локальной лаборатории, не staging** (A-71) |
| ✅ | Threat model со ссылками на код | `docs/THREAT_MODEL.md` — 24 угрозы, каждая митигация с file:line |
| ✅ | COST.md с тремя профилями | `docs/COST.md` — Small/Medium/Enterprise, стоимость на 1000 сессий, быстрорастущие статьи |
| ⚠️ | Учение по восстановлению, RTO замерен | PITR 14.6 с и restore-verify 17.3 с на 3.5 МБ (`gate-9.out`); production-объём — на staging (A-61) |
| ✅ | Все 16 инвариантов имеют зелёный тест | `tests/invariants/test_i01…test_i16` (72 теста, `docs/TESTING.md` §3), gate-10 |
| ⚠️ | docs/ASSUMPTIONS.md: каждое допущение подтверждено или снято | статус проставлен для всех 74: 43 подтверждено, 4 снято, **27 открыто** (17 — заказчик, 2 — юрист, 8 — staging): их нельзя закрыть без заказчика/контура, они и есть условия запуска §6 |
