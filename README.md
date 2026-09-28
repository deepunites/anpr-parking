# ANPR Parking Platform

Multi-tenant SaaS автоматизированной парковки с ANPR. Ведётся по `docs/prompts/` (Prompt Kit v3.0), фаза за фазой.

**Быстрый запуск на одном компьютере:** Windows — `START.bat` (нужен Docker Desktop, инструкция — `README-START.txt`);
Linux/macOS — `docker compose -f deploy/local/compose.yml up -d --build`, затем http://localhost:8080. Состав и
первичная настройка — `deploy/local/` (compose повторяет Helm-чарт; секреты стенда создаются при первом запуске).

| Документ | Что там |
|---|---|
| `docs/prompts/00-CORE.md` | рамка: приоритеты, масштаб, SLO, 16 инвариантов, стек, ответы Q1–Q9 |
| `docs/ARCHITECTURE.md` | фаза 1: C4, bounded contexts, context map, sequence и state диаграммы, failure matrix, безопасность, таблица инвариантов |
| `docs/adr/` | ADR-01…07 |
| `docs/ASSUMPTIONS.md` | допущения, требующие подтверждения |
| `docs/DATABASE.md` | фаза 2: критика черновика, решения, ERD, партиции/ретеншен, RLS, EXPLAIN, gate |
| `docs/BACKEND.md` | фаза 3: структура, подфазы 3.1–3.5, что отложено, замеры |
| `docs/DEVICES.md` | фаза 4: camera API и аутентификация, heartbeat/здоровье, шлагбаумы (драйверы, ретраи, breaker, SSRF), оффлайн-режим и DLQ, media; `docs/backend/gate-4.out` — реальный вывод gate |
| `docs/PAYMENTS.md` | фаза 5: наличные и смены, Payme/Click (checkout, вебхуки, верификация), возвраты, сверка, интеграция с EXIT-сагой; `docs/backend/gate-5.out` |
| `docs/FRONTEND.md` | фаза 6: SPA (`frontend/`), WebSocket `/ws/v1/lots/{lot_id}` с политикой бэкпрешера, безопасность (CSP, токены), E2E/Lighthouse; `docs/backend/gate-6.out` |
| `docs/OBSERVABILITY.md`, `docs/OPERATIONS.md`, `docs/RUNBOOK.md` | фаза 7: логи/корреляция, метрики и кардинальность (`docs/backend/metrics-cardinality.md`), трассировка (`docs/backend/trace-exit-saga.json`), дашборды и алерты (`deploy/`), Sentry; `docs/backend/gate-7.out` |
| `docs/TESTING.md` | фаза 8: покрытие по слоям (`docs/backend/coverage-layers.md`), mutation testing (`mutation-report.md`), инварианты, интеграционные, failure-матрица, E2E, k6 (`docs/backend/load/`), профили (`profile-before/after.md`), сканеры и тесты безопасности (`security/`); `docs/backend/gate-8.out` |
| `docs/INFRA.md` | фаза 9: контейнеры (Dockerfile'ы, выбор базы по Trivy), Helm-чарт приложения (`deploy/helm/parking`) и платформенный слой (`deploy/k8s/platform`), PostgreSQL под Patroni/PgBouncer/pgBackRest (`deploy/postgres`, лаборатория `lab/` с учениями failover/бэкап/PITR), Ansible, Terraform (Proxmox, `tofu test`), CI/CD (`.github/workflows`), откат и правило expand→migrate→contract; `docs/backend/gate-9.out` |
| `docs/PRODUCTION_READINESS.md`, `docs/RUNBOOK.md`, `docs/THREAT_MODEL.md`, `docs/PRIVACY.md`, `docs/COST.md` | фаза 10: отчёт о готовности (что работает/не проверено/риски/долг/границы/условия запуска + чек-лист со ссылками), 15 runbook-сценариев и 3 учения с логами (`docs/backend/runbook-drills/`), модель угроз с file:line, процесс ЗРУ-547 с тестом необратимости, стоимость трёх профилей; `docs/backend/gate-10.out` |
| `frontend/` | React 18 + TS + Vite: Login, Live Parking, Sessions, Payments, списки; `frontend/README.md` — команды, `e2e/` — Playwright |
| `docs/EVENTS.md`, `docs/openapi.json` | реестр integration events и контракт API — генерируются из кода |
| `backend/` | код: миграции Alembic (SQL), `platform/db`, тесты инвариантов; `backend/README.md` — локальная среда (Node 24 для фронта: `~/.local/node/bin`) |

Состояние: **проект завершён** — фазы 1–10 выполнены (2026-09-20). Итоговый отчёт для руководства — `docs/FINAL_REPORT.md`. Вердикт и условия запуска — `docs/PRODUCTION_READINESS.md`; открытые допущения (заказчик/юрист/staging) — `docs/ASSUMPTIONS.md`.
