# Инфраструктура, CI/CD, восстановление — фаза 9

**Статус:** 2026-09-20. Gate-вывод — `docs/backend/gate-9.out`. Код: `backend/Dockerfile`, `frontend/Dockerfile`,
`deploy/helm/parking` (чарт приложения), `deploy/k8s/platform` (платформенный слой), `deploy/postgres` (Patroni/pgBackRest/
PgBouncer/HAProxy/etcd + лаборатория `lab/`), `deploy/ansible`, `deploy/terraform`, `.github/workflows`, `scripts/`
(`smoke_staging.py`, `migration_check.py`, `rollback.sh`, `chaos_kill_api.sh`). Допущения — `docs/ASSUMPTIONS.md` A-60…A-66.

Рамка (CORE Q5/Q6, A-04): self-hosted Kubernetes (RKE2) на ВМ Proxmox в трёх зонах отказа (три хоста), PostgreSQL под
Patroni на отдельных ВМ, MinIO вместо S3, всё внутри контура UZ. Что из gate выполнено на рабочей станции, а что требует
staging-кластера — таблица в конце (§8).

## 1. Контейнеры

Многостадийная сборка (`backend/Dockerfile`): builder ставит зависимости из `uv.lock` (`--no-dev`: pgserver, mutmut, bandit в
образ не попадают), runtime — `python:3.12-alpine3.22` без компиляторов, uv и pip; пользователь `app` 10001, root-fs read-only
(пишущий каталог только `/tmp` — emptyDir), capabilities сброшены (Helm `containerSecurityContext`). Frontend — сборка Vite в
`node:24-bookworm-slim`, раздача `nginxinc/nginx-unprivileged` (uid 101, порт 8080), CSP/HSTS из `frontend/nginx.conf`, source
maps удаляются из образа (загружаются в Sentry из CI). `.dockerignore` — allowlist: только манифесты, `src`, `alembic`
(тесты, `.env*`, `.git`, ключи — вне контекста).

**Выбор базы по Trivy** (та же БД уязвимостей, 2026-09-20):

| База | CRITICAL | HIGH | Примечание |
|---|---|---|---|
| `python:3.12-slim-bookworm` | 5 (unfixed: perl-base ×3, libsqlite3, zlib1g) | 55 unfixed | gate «без critical» не проходит |
| `python:3.12-slim-trixie` | 0 | 44 unfixed | без исправлений от дистрибутива — шум в отчётах |
| `gcr.io/distroless/python3-debian13` | 0 | 21 unfixed | Python 3.13, нет apk/shell для отладки |
| **`python:3.12-alpine3.22` + `apk upgrade`** | **0** | **0** (10 HIGH с фиксами устранены upgrade'ом) | все зависимости — musllinux-колёса, компиляторы только в builder |

Итог: backend `localhost/parking-backend:0.9.0-sandbox` — 240 МБ, **0 уязвимостей, 0 секретов**; frontend
`parking-frontend:0.9.0-sandbox` — 22.7 МБ, **0 уязвимостей** (после `apk upgrade`: libcrypto3 3.5.6 → 3.5.8). Оба собраны
rootless podman 6.1 (podman-static, без демона и root) и запущены с `--read-only --cap-drop ALL`: `/health/live` 200,
`/health/ready` 200 с PostgreSQL по TCP. Теги неизменяемые `{semver}-{git-sha}` (`parking.imageTag` в Helm падает на
`latest`/не-semver в production; Kyverno в кластере — вторая линия). Единственные CVE образа были в pip базового образа —
pip/ensurepip удалены из runtime.

Пробы: `/health/live` — только процесс; `/health/ready` — PostgreSQL **и** Redis, но под снимается с трафика только при
недоступном Redis: без PostgreSQL API продолжает принимать события камер в оффлайн-буфер (фаза 4), и readiness по БД снял
бы все поды разом — тот же провал, от которого §1 предостерегает для liveness (A-60, `tests/api/test_health.py`).

## 2. Kubernetes (Helm-чарт `deploy/helm/parking`)

| Компонент | Реплики (prod) | Почему |
|---|---|---|
| api (uvicorn, 1 воркер/под) | 3, HPA 3…24 по CPU 60 % и `parking_http_requests_per_second` 20/под | реестр метрик на процесс (A-54); RPS через prometheus-adapter |
| publisher | 2 (1 активная — advisory lock в PostgreSQL по прямому соединению) | порядок событий; горячий резерв |
| consumer | 2 (аренда 8 шардов, ≤ 8 активных) | consumer group распределит |
| replayer, camera-health, celery-beat | 1 (singleton, PDB `maxUnavailable: 1`) | группа offline / A-43 / планировщик |
| celery exit (`saga,barrier`), payments, maintenance | 2 / 2 / 2, `--pool=solo` | очереди по критичности; barrier изолирован от consumer'а (`PARKING_BARRIER_DISPATCH=celery`) |
| frontend (nginx) | 2 | statless |

Обязательное по §2 (проверено на отрендеренных манифестах — 134 ресурса через `kubeconform -strict`, 0 ошибок): PDB на
каждый Deployment (10/10), `podAntiAffinity` required по узлу + preferred по зоне, `terminationGracePeriodSeconds`
(api 30 + `preStop sleep 5` и `--timeout-graceful-shutdown`; consumer 60; celery exit 60 / payments 120 / maintenance 300),
SIGTERM в коде — `workers/runtime.py::_serve` передаёт `stop` в цикл (до фазы 9 событие создавалось, но в цикл не
попадало: процесс жил до SIGKILL; `tests/workers/test_shutdown.py`), Celery-воркер поднимает контейнер и `/metrics` при
старте (`worker_init`), `securityContext` (runAsNonRoot 10001, no-new-privileges, drop ALL, RuntimeDefault, read-only fs),
ServiceAccount без automount, секреты только `ExternalSecret` → Vault (`kv/parking/<env>/app`; `kind: Secret` в git нет,
inline-режим — только sandbox), NetworkPolicy default-deny + явные правила: ingress-контроллер → api/frontend; frontend →
только api; api/воркеры → PostgreSQL (6432 PgBouncer, 5000 primary для maintenance-DSN), Redis, MinIO, контроллеры
шлагбаумов (CIDR площадок), провайдеры платежей (443, без приватных диапазонов), namespace наблюдаемости; migrate-Job →
только PostgreSQL. **Под фронтенда до PostgreSQL не достучится** по построению (нет правила egress к CIDR БД) — живая
проверка на staging (§8).

**Ресурсы — из замеров фазы 8** (`docs/backend/load/*.json`, `docs/backend/profile-after.md`):

| Процесс | RSS под нагрузкой (фаза 8) | requests / limits | Вывод |
|---|---|---|---|
| api | 337–357 МБ (baseline/absolute, 2 воркера ⇒ на процесс) | 500m / 384Mi → 1 CPU / 768Mi | CPU: 40–60 мс на событие камеры ⇒ ≈ 10 соб./с на под при 60 % целевой утилизации; 3 пода = baseline 30 RPS, 24 пода = пик 200 RPS |
| consumer | 132–137 МБ | 250m / 192Mi → 1 CPU / 512Mi | ≈ 5 обработчиков на выезд, шарды параллельно |
| publisher | 129–131 МБ | 100m / 192Mi → 500m / 384Mi | батчи по 500, NOTIFY |
| replayer | 106–128 МБ | 100m / 192Mi → 500m / 384Mi | |
| celery (solo) | ≈ consumer | 100m / 192Mi → 500m / 384Mi (maintenance 1 CPU / 512Mi) | |
| frontend | nginx ≈ 10 МБ | 50m / 32Mi → 250m / 128Mi | |

Абсолютный пик 400 соб./с (§6 фазы 8) со стоимостью события фазы 8 (≈ 18 SQL) требовал ≈ 40 подов API — HPA за 5 минут
столько не поднимет; допущение A-63: пик покрывается заранее поднятыми репликами по календарю (мероприятия). После
снижения стоимости события до 10 SQL и одной транзакции (`docs/backend/sql-budget.md`, 2026-09-21) число подов на пик
пересчитывается по k6 на staging. Кеши справочников процесса — `config.referenceCacheTtlSeconds` (10 с): правка
площадки/шлагбаума/тарифа видна всем подам не позже TTL.

Rolling update с readiness gate (`maxUnavailable: 0, maxSurge: 1`); blue-green не нужен: состояние WebSocket в Redis,
миграции обратносовместимы в пределах релиза (§5). Платформенный слой (`deploy/k8s/platform`): PSA `restricted`, ESO
`ClusterSecretStore vault`, prometheus-adapter, Kyverno (только подписанные cosign образы из harbor, запрет `latest`,
обязательные probes/resources/non-root), cert-manager DNS-01, CronJob снапшотов Vault, `versions.yaml`.

**Redis HA (фаза 10 §5).** Redis 7 + Sentinel на трёх ВМ по зонам (`module.redis` в Terraform, роль `deploy/ansible/roles/redis`:
`requirepass`/`masterauth`, AOF everysec, `min-replicas-to-write 1`, `maxmemory-policy volatile-lru` — кеши с TTL вытесняются,
стримы и оффлайн-буфер без TTL никогда; Sentinel `down-after-milliseconds 5000`, кворум 2/3). Приложение и Celery ходят по
`PARKING_REDIS_URL=redis+sentinel://:pw@r1:26379,r2:26379,r3:26379/parking?db=0` (`platform/redis.py`: `Sentinel.master_for`,
после failover пул сам переоткрывает соединения; Celery — `sentinel://` transport с `master_name`). NetworkPolicy egress —
6379 и 26379. Учение на локальных бинарниках (`tests/failure/test_redis_sentinel.py`, `-m failure`): master убит SIGKILL,
тот же клиент пишет снова через 2.5 с, данные до отказа на новом master. Redis внутри Kubernetes (StatefulSet) не используется —
как и PostgreSQL, состояние живёт на ВМ с локальным диском.

## 3. PostgreSQL: Patroni

**Выбор — Patroni на ВМ** (не CloudNativePG): так решено в CORE Q5; кроме того, на self-hosted Proxmox без реплицируемого CSI
локальные NVMe-диски ВМ надёжнее и быстрее PVC, а жизненный цикл СУБД отвязан от обновлений кластера. Топология §4 CORE:
primary + **sync standby** (`synchronous_mode: true`, `synchronous_node_count: 1`; `app_critical` пишет с
`synchronous_commit=remote_apply` — RPO 0 для сессий/платежей) + **async standby** (тег `nosync`, отчёты через HAProxy :5001,
базовые бэкапы `backup-standby=y`). DCS — etcd v3 ×3 на тех же ВМ (TLS, кворум 2/3), `failsafe_mode` — при потере etcd
primary не уходит в read-only, пока узлы видят друг друга. HAProxy :5000 → primary по `GET /primary`, :5001 → реплики.
Файлы: `deploy/postgres/patroni.yml.j2`, `etcd.conf.yml.j2`, `haproxy.cfg.j2`, роли Ansible.

**PgBouncer 1.25, `pool_mode = transaction`** (`pgbouncer.ini.j2`). Ловушка `SET LOCAL` проверена тестом, не документацией:
[`tests/db/test_pgbouncer_rls.py`](../backend/tests/db/test_pgbouncer_rls.py) на лаборатории — 160 транзакций двух
tenant'ов вперемешку через пулер, **утечек контекста 0** (UoW ставит `set_config(..., is_local=true)`), а контрольный
session-level `SET` **утёк в 40 из 40** новых соединений. Поэтому статический тест
`tests/architecture/test_sql_fragments.py::test_no_session_level_set_or_listen_in_pooled_code` запрещает session-level
SET/LISTEN/advisory lock везде, кроме outbox publisher, который держит отдельное прямое соединение к primary (maintenance
DSN → HAProxy :5000, минуя пулер). Prepared statements asyncpg работают через пулер (`max_prepared_statements = 500`,
PgBouncer ≥ 1.21) — весь E2E-набор (8 тестов) прошёл с приложением на Patroni через PgBouncer.

**Автоматический failover — тест** (`deploy/postgres/lab/pgha_lab.py failover`, Patroni 3.3.7 + raft-DCS на рабочей станции,
в production — etcd с теми же `ttl/loop_wait`, A-61): `kill -9` postmaster и агента primary под потоком записей через
«HAProxy» — новый primary = sync standby, **RTO 19.2 с** (lab `ttl 20/loop_wait 5`; production `ttl 30/loop_wait 10` ⇒ ожидаемо
30–45 с, замер на staging), потерянных подтверждённых записей 0 (322 успешных INSERT = 322 строки после failover), старый
primary вернулся репликой через pg_rewind/basebackup на timeline 2.

**Параметры** (`patroni.yml.j2`, ВМ 8 vCPU / 32 ГБ / NVMe; отклонения от дефолта):

| Параметр | Значение | Почему |
|---|---|---|
| `max_connections` | 300 | PgBouncer держит ≤ 200 серверных соединений (2 пулера × 100), запас — реплики, verify, обслуживание; дефолт 100 мал для 24 подов API × pool 10 без пулера |
| `shared_buffers` / `effective_cache_size` | 8 GB / 24 GB | 25 % / 75 % RAM ВМ; рабочий набор фазы 8 — партиции суток `anpr_event`/`outbox` (≈ 3 МБ WAL/мин при 6 соб./с ⇒ ≈ 100 МБ/мин при 200) |
| `work_mem` | 32 MB | отчёты/сверка сортируют десятки тысяч строк; при 200 соединениях ×32 МБ ≪ RAM; дефолт 4 МБ даёт сортировки на диске |
| `maintenance_work_mem` | 1 GB | VACUUM/индексы партиций |
| `checkpoint_timeout` / `max_wal_size` / `completion_target` | 15 min / 8 GB / 0.9 | при пиковом WAL ≈ 100 МБ/мин чекпойнт по объёму не чаще раза в ~10 мин, размазан на 90 % интервала — без всплесков fsync под пиком |
| `wal_compression=zstd`, `wal_keep_size=2GB` | | меньше трафика на sync standby; окно для pg_rewind |
| `archive_timeout=60s` | | RPO архива ≤ 60 с даже при пустом трафике ночью |
| `autovacuum_naptime=15s`, `cost_limit=2000`, `cost_delay=2ms`, 5 воркеров | | горячие партиции суток должны вакуумироваться чаще дефолтных 1 мин/200 |
| `shared_preload_libraries=pg_stat_statements,auto_explain`, `auto_explain.log_min_duration=500ms` (+`log_analyze`, без timing) | | §3; overhead analyze без timing мал |
| `idle_in_transaction_session_timeout=60s` | | защита от зависших транзакций через пулер (per-role: `app_critical` 10 с из миграции 0001) |
| `huge_pages=try`, `data-checksums` | | checksums — обнаружение битых страниц при pg_amcheck/восстановлении |

**Autovacuum-профиль горячих таблиц** — миграция `0013` (`app.autovacuum_profile` + `app.apply_autovacuum_profile`):
`anpr_idempotency`, `outbox` (`scale_factor 0.02`, threshold 1000, `fillfactor 80` для HOT-обновлений `status/published_at`),
`consumer_inbox`, `anpr_event`, `webhook_inbox`; партиционированные родители не передают reloptions листам, поэтому профиль
применяется к каждой партиции, а `app.ensure_range_partitions` ставит его новым партициям автоматически.

Лаборатория воспроизводит всё это без root (`deploy/postgres/lab`): PostgreSQL 16.2 из pgserver + contrib, собранный
`scripts/build_pg_contrib.sh` (`pg_stat_statements`, `auto_explain`, `amcheck`, `btree_gist`), Patroni через `uvx`, PgBouncer
из deb Ubuntu (без установки), WAL-архив в MinIO (`wal_archive.py`).

## 4. Backup и восстановление

Production — **pgBackRest** (`pgbackrest.conf.j2`, systemd-таймеры в роли `pgbackrest`): полный бэкап ежедневно 01:00,
дифференциальный каждые 6 ч (07/13/19 + block-incremental), WAL непрерывно (`archive-push`, `archive-async`), PITR
(`--type=time`), retention 30 полных / WAL 7 дней, репозиторий — отдельный MinIO (не на ВМ кластера), бакет с
версионированием и object-lock, репозиторий зашифрован `aes-256-cbc`, бэкап снимается с async standby.

**Бэкап существует, только если восстановился.** Еженедельно (воскресенье 03:00, verify-ВМ, `backup_verify.sh`): restore
последнего набора в изолированный инстанс → archive recovery до конца WAL → `pg_amcheck --heapallindexed --parent-check` →
контрольные суммы бизнес-данных за вчера (число сессий, сумма PAID-платежей, число outbox-записей) сравниваются с primary →
метрика `backup_restore_verified_timestamp` (textfile collector) и `backup_restore_duration_seconds`; алерт
`BackupRestoreVerifiedStale` (> 8 дней, promtool-тест в `alerts_test.yaml`), runbook `#backup-restore-verify`.

Учения выполнены реально на лаборатории (тот же механизм PostgreSQL: базовый бэкап + непрерывный WAL-архив в MinIO + PITR;
pgBackRest заменён скриптом `wal_archive.py`, A-61) на данных E2E-прогона приложения:

| Учение | Результат |
|---|---|
| Проверка бэкапа (`pgha_lab.py verify`) | restore 17.3 с, `pg_amcheck` rc=0, контрольные суммы источник = восстановленный (`sessions_today 10`, `payments_sum_today 1 000 000`, `outbox_total 58`), `backup_restore_verified_timestamp=1789896131` |
| PITR на произвольную точку (`pgha_lab.py pitr`) | target 09:22:16.24Z; в источнике 6 строк-маркеров, ≤ target — 3; восстановлено 3, «после target» 0; **RTO 14.6 с** |
| Полная потеря primary → восстановление из бэкапа | failover 19.2 с (§3) — данные не теряются; восстановление «с нуля» из репозитория = PITR-учение: на 3.5 МБ 14.6 с, оценка для production-объёма (≈ 50 ГБ, NVMe, `process-max=4`, zstd) — 8–12 мин ≤ **SLO 15 мин**; фактический замер на staging (§8) — если > 15 мин, чинить процедуру (delta restore, больше `process-max`), не SLO |

Ошибки, пойманные учением (и исправленные в скриптах): бэкап с реплики содержит `standby.signal`/`primary_conninfo` —
восстанавливаемый инстанс подключался к primary как ещё одна реплика (`replication slot "pg3" is active`); архив двух
кластеров под одним префиксом — `archive_command` «идемпотентно» принимал чужой сегмент по размеру (теперь сверка ETag/MD5 и
ошибка при конфликте; у pgBackRest это stanza + system identifier); последний сегмент с checkpoint record не был
заархивирован к моменту restore (`switch_and_wait_archived`); `amcheck` должен быть установлен в БД (`--install-missing`).

**MinIO**: версионирование включено на `pg-backups` и медиа-бакете, репликация бакета во второй MinIO (учение: второй
инстанс на :9010, `mc replicate add … --replicate delete,delete-marker,existing-objects`, новый объект появился на реплике,
статус `online, Errors: 0`). Terraform (`modules/minio_buckets`): `pg-backups-<env>` с object-lock, ILM для кадров.
**Vault**: CronJob `vault operator raft snapshot` ежедневно → бакет `vault-snapshots` (метрика `vault_snapshot_timestamp`);
восстановление — `raft snapshot restore -force`.

## 5. CI/CD (`.github/workflows`)

`ci.yml`: lint → typecheck → unit+integration (реальные PostgreSQL/Redis/MinIO как services) → **invariants** (отдельный job,
блокирует merge) → **openapi-compat** (`oasdiff breaking` по `origin/main` + `scripts/openapi_compat.py`) → **migration-check**
(`scripts/migration_check.py`: каждая новая ревизия отдельно на копии схемы staging — ночной артефакт `pg_dump --schema-only`;
время, наблюдение `pg_locks` каждые 50 мс, статический разбор `ALTER TABLE` больших таблиц: ACCESS EXCLUSIVE без пометки
`-- migration-check: allow-lock` = блок; проверено на синтетическом `ALTER COLUMN TYPE` — заблокирован) → security-scan
(gitleaks по всей истории, bandit, pip-audit, npm audit, trivy fs, semgrep) → build (тег `{semver}-{git-sha}`) →
container-scan (Trivy `--ignore-unfixed --exit-code 1` + SARIF) → push (harbor, подпись cosign keyless; Kyverno пускает только
подписанные) → helm lint/template + kubeconform + tofu validate/test.

`cd.yml`: `main` → staging (`helm upgrade --atomic`, миграции pre-upgrade Job) → смоук `scripts/smoke_staging.py` (реальное
camera-событие: ENTRY → ACTIVE → EXIT → обязательство → наличные кассиром → CLOSED → платёж в API; при провале —
`helm rollback`) → `rollback-drill` (откат на предыдущую ревизию → смоук → возврат) → `chaos-drill`
(`scripts/chaos_kill_api.sh`: k6 baseline ÷10 + `kubectl delete pod` API, критерий 0 ошибок) → production **только с тега
`vX.Y.Z`** (версия обязана совпадать с `pyproject`), только после approval (environment `production` с reviewers), подпись
образа проверяется, тот же git-sha, что прошёл staging. Смоук проверен локально: `smoke_staging.py --stack` → `SMOKE OK …
ENTRY→ACTIVE→EXIT→EXIT_CONFIRMED→CLOSED, payments=1, 1.8s`.

**Откат** — одна команда `scripts/rollback.sh <release> <ns>` (`helm rollback` на предыдущую ревизию, `--wait`, смоук).
Схема при откате **не откатывается**: правило релизов — миграции обратносовместимы в пределах одного релиза:
1) *expand* — новые колонки/таблицы nullable или с DEFAULT, новые индексы `CONCURRENTLY`, старый код их не замечает;
2) *migrate* — код читает/пишет и старое, и новое; бэкфилл батчами джобом maintenance;
3) *contract* — через релиз удаляем старое. Если откат кода несовместим с уже применённой миграцией (нарушено правило) —
откат кода запрещён, чинится **forward-fix**: новая миграция, возвращающая совместимость (или релиз N+1 с исправлением);
`alembic downgrade` в production не выполняется (данные, записанные новым кодом, потерялись бы). CI-барьер: `migration_check`
+ ревью `contract`-миграций с обязательной ссылкой на релиз, в котором завершился *migrate*.

## 6. Terraform и Helm

`deploy/terraform`: модули `vm` (Proxmox `bpg/proxmox`, cloud-init, зона отказа = хост), `dns` (RFC2136), `minio_buckets`
(object-lock + версионирование, tfstate), `pki` (внутренний CA → Vault PKI, KV v2 для ESO); окружения `envs/staging`,
`envs/production` (3 узла RKE2 по зонам, 3 ВМ PostgreSQL/etcd, 2 PgBouncer+HAProxy, 2 MinIO, verify-ВМ), RKE2 через
cloud-init (CIS-профиль, Canal, снапшоты etcd в MinIO). Состояние — S3-бэкенд в MinIO с `use_lockfile = true` (блокировка без
DynamoDB), отдельные ключи/креды на окружение; production применяется только с раннера production после approval,
из CI по расписанию — `tofu plan -detailed-exitcode` (`drift.yml`, каждые 6 ч): дрейф → issue + алерт `InfraDrift` в
Alertmanager. Ручных изменений нет по построению: у людей нет учёток Proxmox/Vault с правом записи в production, только через
pipeline. Проверено локально (OpenTofu 1.10.6): `tofu fmt -check`, `validate` для staging и production, `tofu test` с
`mock_provider` (2 прогона: три зоны для k8s и PostgreSQL, бакет бэкапов) — план без облака.

Helm: `helm lint --strict` для default/staging/sandbox, `helm template` → 142 ресурса, `kubeconform -strict` 134 valid / 0
invalid (8 CRD без схем: ExternalSecret, PrometheusRule, ServiceMonitor, PodMonitor). Три окружения = три values-файла и три
Vault-пути.

## 7. Изменения кода в фазе

`api/v1/health.py` (readiness с Redis, A-60), `workers/runtime.py` (SIGTERM → stop, `wait_for` в camera-health),
`workers/celery_app.py` (`worker_init` → контейнер/метрики), `platform/config.py` (`barrier_dispatch`), миграция `0013`,
`tests/api/test_health.py`, `tests/workers/test_shutdown.py`, `tests/db/test_pgbouncer_rls.py`,
`tests/architecture` (session-level SET), `scripts/{migration_check,smoke_staging,build_pg_contrib}`, алерт
`BackupRestoreVerifiedStale`.

## 8. Что выполнено здесь и что требует staging

| Пункт gate §7 | На рабочей станции (без root/Docker/кластера) | На staging (CI: cd.yml / nightly.yml) |
|---|---|---|
| docker build + Trivy без critical | ✅ rootless podman, 0 уязвимостей оба образа, запуск read-only/non-root | container-scan в CI |
| helm lint/template | ✅ + kubeconform | job helm-terraform |
| terraform plan staging | ✅ validate + `tofu test` (mock providers); настоящий `plan` требует Proxmox/Vault | drift.yml |
| кластер staging, смоук | ⛔ кластера нет; смоук-скрипт проверен на локальном стенде | deploy-staging + smoke |
| убит под API под k6 | ⛔ нет кластера; скрипт `chaos_kill_api.sh` | chaos-drill |
| убит primary PostgreSQL → failover, RTO | ✅ Patroni lab: RTO 19.2 с, потерь 0 | повтор с etcd, ttl 30 |
| восстановление из бэкапа, контрольные суммы, RTO | ✅ 17.3 с, суммы сошлись | backup-verify timer |
| PITR на произвольную точку | ✅ 14.6 с, «после target» 0 | pgBackRest `--type=time` |
| PgBouncer transaction pooling + RLS тестом | ✅ 0 утечек / ловушка воспроизведена | job pooler |
| откат релиза в staging | ⛔ нет кластера; `rollback.sh` + правило expand/migrate/contract | rollback-drill |
| NetworkPolicy: фронтенд → PostgreSQL | ⛔ нет CNI; политики отрендерены, default-deny + явные правила | `kubectl exec frontend -- nc -zv pgbouncer 6432` в chaos-drill |
| секретов в git и образе нет | ✅ gitleaks + Trivy secret scan 0 | security-scan |
| resources из замеров фазы 8 | ✅ таблица §2 | k6 nightly на staging уточняет |
