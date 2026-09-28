# backend

Python 3.12 (`uv`), PostgreSQL 16, SQLAlchemy 2 async + asyncpg, Alembic (миграции — чистый SQL в `alembic/sql/`).

## Локальная среда без Docker и root

```bash
uv sync                                   # зависимости + Python 3.12
uv run python scripts/pg_dev.py start     # PostgreSQL 16.2 из пакета pgserver, данные в .pgdata/, сокет там же
uv run python scripts/pg_dev.py psql      # psql к нему
uv run python scripts/pg_dev.py stop
```

`pgserver` собран без contrib, а `btree_gist` обязателен (EXCLUDE, I-12). Один раз собрать его через `zig cc` (компилятор с PyPI, root не нужен):

```bash
curl -sSL -o /tmp/pg.tar.gz https://ftp.postgresql.org/pub/source/v16.2/postgresql-16.2.tar.gz && tar -xzf /tmp/pg.tar.gz -C /tmp --wildcards 'postgresql-16.2/contrib/*'
scripts/build_pg_contrib.sh build /tmp/postgresql-16.2        # btree_gist, pg_stat_statements, auto_explain, amcheck (zig cc через uvx ziglang)
```

Артефакты кешируются в `.pgext/`; после каждого `uv sync` (он переустанавливает `pgserver`) `pg_dev.py start` сам доставит расширение из кеша.

## Миграции и данные

```bash
uv run alembic upgrade head               # БД parking_dev (создать: psql -c 'CREATE DATABASE parking_dev')
uv run alembic downgrade base && uv run alembic upgrade head
uv run python scripts/pg_dev.py psql -d parking_dev -v ON_ERROR_STOP=1 -f scripts/seed_dev.sql    # 2 tenant, 3 lot, 6 камер…
uv run python scripts/pg_dev.py psql -d parking_dev -v ON_ERROR_STOP=1 -f scripts/load_1m.sql     # 1 млн сессий для EXPLAIN (минуты)
uv run python scripts/pg_dev.py psql -d parking_dev -f scripts/explain_queries.sql                 # 9 запросов §3 фазы 2
```

DSN для Alembic и приложения: `PARKING_DATABASE_URL` (по умолчанию — локальный pgserver, БД `parking_dev`).

## Redis и воркеры

```bash
uv run python scripts/redis_dev.py start          # Redis 7.4 из conda-forge, без root (scripts/fetch_redis.py)
PARKING_ENV=dev uv run uvicorn parking.api.main:app --port 8000
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_publisher; main_publisher()'
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_consumer; main_consumer()'
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_offline_replayer; main_offline_replayer()'  # anpr:offline → PostgreSQL
PARKING_ENV=dev uv run python -c 'from parking.workers.runtime import main_camera_health; main_camera_health()'        # или Celery beat
```

## MinIO (media, фаза 4)

```bash
uv run python scripts/minio_dev.py setup          # MinIO из conda-forge (scripts/fetch_minio.py), бакет anpr-media, lifecycle 90 дней; креды → .env.dev
uv run python scripts/minio_dev.py stop
PARKING_ENV=dev uv run python scripts/bench_camera_auth.py 200 10   # замер аутентификации камеры (gate фазы 4)
```

Тесты `tests/media` поднимают MinIO сами (`PARKING_TEST_S3_ENDPOINT` — внешний, для CI); `tests/anpr/test_offline.py`
по-настоящему останавливает и запускает локальный PostgreSQL (`scripts/pg_dev.py stop|start`).

## Проверки (gate CORE §13)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/
uv run pytest -q                          # PARKING_TEST_DSN=postgresql://… — внешний кластер (CI, testcontainers); иначе локальный pgserver
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```

## Наблюдаемость (фаза 7)

Переменные: `PARKING_OTEL_EXPORTER_ENDPOINT` (OTLP/HTTP коллектор; пусто — без экспорта, `trace_id` в логах всё равно есть),
`PARKING_OTEL_ENTRY_SAMPLE_RATIO=0.02`, `PARKING_OTEL_API_SAMPLE_RATIO=0.1`, `PARKING_SENTRY_DSN`,
`PARKING_METRICS_PORT` (воркеры; API отдаёт `GET /metrics` сам), `PARKING_LOG_LEVEL`.

```bash
uv run pytest -q tests/observability -s                       # корреляция, трасса EXIT, редакторы, метрики, алерты/дашборды
~/.local/prometheus/bin/promtool check rules ../deploy/prometheus/alerts.yaml
~/.local/prometheus/bin/promtool test rules ../deploy/prometheus/alerts_test.yaml
uv run python scripts/alert_sim.py                            # живая симуляция: publisher убит → OutboxLag; PostgreSQL остановлен → PostgresPrimaryDown
python3 ../deploy/grafana/gen_dashboards.py                   # перегенерировать дашборды (и копии в Helm-чарте)
```
Подробнее — `docs/OBSERVABILITY.md`, `docs/OPERATIONS.md`, `docs/RUNBOOK.md`.

## Frontend и E2E-стенд (фаза 6)

```bash
export PATH=~/.local/node/bin:$PATH                 # Node 24 LTS распакован без root (tar.xz с nodejs.org)
uv run python scripts/e2e_stack.py start             # отдельная БД parking_e2e + uvicorn :8010 + publisher + consumer; frontend/e2e/.stack.json
uv run python scripts/e2e_stack.py stop
cd ../frontend && npm run e2e                        # Playwright сам поднимает стенд и vite preview
```

## Тестирование (фаза 8, `docs/TESTING.md`)

```bash
uv run pytest -q                                              # юнит + интеграционные (реальные PG/Redis/MinIO); failure/e2e исключены addopts
uv run pytest -q --cov --cov-report=json && uv run python scripts/coverage_layers.py coverage.json --markdown ../docs/backend/coverage-layers.md
uv run pytest -q tests/invariants                             # I-1…I-16, отдельная CI-джоба
uv run mutmut run && uv run mutmut results                    # tariff/billing domain; не совмещать с другими сессиями на parking_test
uv run pytest tests/failure -m failure                        # лаборатория primary+standby+toxiproxy (~/.local/tools), останавливает PG/Redis/MinIO
uv run pytest tests/e2e -m e2e                                # сквозной путь на стенде e2e_stack.py (фейковый шлагбаум scripts/fake_barrier.py)
uv run pytest -q tests/security -m slow                       # тайминговая проверка ключа камеры (остальные security-тесты — в обычном прогоне)
uv run python scripts/load_test.py --scenario baseline --scale 30 --rate-scale 5 --workers 2 --cameras 20   # k6 (~/.local/tools/k6); --lab — реплика
PARKING_E2E_PROFILE=1 uv run python scripts/load_test.py --scenario baseline --workers 1 ...                # + py-spy профиль → scripts/profile_summary.py
(cd .. && backend/.venv/bin/python scripts/security_scan.py)  # bandit, pip-audit, npm audit, trivy fs, gitleaks, semgrep; исключения — security/exceptions.yaml
```

## Инфраструктура (фаза 9, `docs/INFRA.md`)

```bash
export PATH=~/.local/tools/podman/podman-linux-amd64/usr/local/bin:$PATH   # rootless podman-static (конфиг ~/.config/containers)
podman build -t parking-backend:0.9.0-sandbox -f Dockerfile .              # из backend/; frontend — аналогично из frontend/
podman save --format docker-archive -o /tmp/b.tar localhost/parking-backend:0.9.0-sandbox && ~/.local/tools/trivy image --input /tmp/b.tar
~/.local/tools/helm lint ../deploy/helm/parking --strict --set image.tag=0.9.0-abcdef1 && ~/.local/tools/helm template parking ../deploy/helm/parking --set image.tag=0.9.0-abcdef1 | ~/.local/tools/kubeconform -strict -ignore-missing-schemas -summary
(cd ../deploy/terraform/envs/staging && ~/.local/tools/tofu init -backend=false && ~/.local/tools/tofu validate && ~/.local/tools/tofu test)
uv run python ../deploy/postgres/lab/pgha_lab.py start|status|failover|verify|pitr|pooler-test|stop   # Patroni ×3 + PgBouncer + WAL-архив в MinIO (PGHA_DB=parking_e2e — на данных e2e)
uv run python scripts/migration_check.py --dsn "postgresql://postgres@/postgres?host=$PWD/.pgdata"   # блокировки/время новых миграций
uv run python scripts/smoke_staging.py --stack                                                       # смоук полного пути (стенд e2e_stack.py)
```
