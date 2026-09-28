# PHASE 09 — Инфраструктура, CI/CD, восстановление

**Предусловие:** gate фазы 8 пройден.

---

## 1. Контейнеры

- Многостадийная сборка, итоговый образ без компиляторов и `uv`.
- Непривилегированный пользователь, `readOnlyRootFilesystem: true`, сброшенные capabilities.
- Distroless или slim-база. Обоснуй выбор через результат Trivy.
- Теги неизменяемые: `{semver}-{git-sha}`. `latest` в production запрещён.
- Healthcheck разделены: `/health/live` (процесс жив) и `/health/ready` (зависимости доступны). Readiness обязан проверять PostgreSQL и Redis, liveness — не обязан, иначе кратковременная недоступность БД перезапустит все поды разом.
- `.dockerignore` исключает `.git`, `.env`, тесты.

---

## 2. Kubernetes

```
≥ 3 worker node в разных зонах отказа
API:           ≥ 3 реплики, HPA по RPS и CPU
Outbox:        1 активная реплика + leader election (порядок событий)
Consumer:      ≥ 2 реплики (consumer group распределит)
Celery:        ≥ 2 реплики на очередь, очереди разделены по критичности
WebSocket:     ≥ 2 реплики, sticky не требуется (состояние в Redis)
```

Обязательно:

- `PodDisruptionBudget` на все deployment.
- `podAntiAffinity` — реплики одного сервиса не на одном узле.
- `resources.requests` и `limits` выставлены на основании замеров фазы 8, не наугад. Приведи цифры и их источник.
- `NetworkPolicy`: default deny, явные разрешения. Фронтенд не ходит в БД, воркеры не принимают входящие.
- `terminationGracePeriodSeconds` достаточен, чтобы воркер доработал задачу; обработка `SIGTERM` в коде.
- `securityContext`: `runAsNonRoot`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`.
- Секреты — через External Secrets Operator из Vault, не `kind: Secret` в git.

---

## 3. PostgreSQL

Patroni (или CloudNativePG — выбери и обоснуй) с топологией под §4 CORE:

```
primary
sync standby     ← synchronous_standby_names, для RPO = 0 критичных транзакций
async standby    ← для чтения отчётов и PITR
```

- Пулер: PgBouncer в режиме `transaction`. **Проверь совместимость с `SET LOCAL` для RLS** — в transaction pooling он работает, в statement pooling нет. Это ловушка, на которой ломается изоляция tenant.
- Автоматический failover, проверенный тестом, а не документацией.
- `pg_stat_statements`, `auto_explain` для запросов дольше 500 мс.
- Параметры под нагрузку из фазы 8: `max_connections`, `shared_buffers`, `work_mem`, `effective_cache_size`, `checkpoint_*`, `autovacuum_*` для горячих таблиц. Обоснуй каждое отклонение от дефолта.
- Отдельный autovacuum-профиль для `anpr_idempotency` и `outbox`: они с высоким оборотом и раздуваются быстрее всего.

---

## 4. Backup и восстановление

pgBackRest:

```
Полный бэкап:     ежедневно
Дифференциальный: каждые 6 часов
WAL:              непрерывная архивация
PITR:             включён
Retention:        30 дней полных, WAL на 7 дней
Хранилище:        отдельное от кластера, с шифрованием
```

**Бэкап считается существующим только после успешного восстановления.** Обязательно:

- Автоматическая еженедельная проверка: восстановление в изолированное окружение, `pg_amcheck`, проверка нескольких контрольных сумм по бизнес-данным (количество сессий и сумма платежей за день должны совпасть).
- Результат проверки — метрика `backup_restore_verified_timestamp`. Алерт, если старше 8 дней.
- **Учение по восстановлению с замером RTO**, выполненное вручную и задокументированное: полная потеря primary, восстановление из бэкапа, замер фактического времени. Если факт превышает 15 минут — либо чини процедуру, либо меняй SLO. Не оставляй расхождение.

Отдельно: бэкап MinIO (версионирование + репликация в другой бакет) и бэкап конфигурации Vault.

---

## 5. CI/CD

```
lint → typecheck → unit → integration → security scan → build
→ container scan → push → deploy staging → smoke → [approval] → production
```

- Тесты инвариантов — отдельной джобой, падение блокирует merge.
- Проверка совместимости OpenAPI (`oasdiff`): несовместимое изменение в `v1` блокирует merge.
- Проверка миграций: применение на копии схемы production, оценка времени и блокировок. Миграция, берущая `ACCESS EXCLUSIVE` на большую таблицу, блокируется автоматически.
- Деплой production — только с тега, только после ручного подтверждения.
- Стратегия: rolling update с readiness gate. Blue-green — только если обоснован (состояние WebSocket, миграции).
- Откат: одна команда, проверенная в staging. Опиши, что делать, если откат кода несовместим с уже применённой миграцией — это самый частый провал отката. Правило: миграции только обратносовместимые в пределах одного релиза (expand → migrate → contract в трёх релизах).
- Смоук-тест после деплоя: реальное camera-событие в staging, проверка полного пути.

---

## 6. Terraform и Helm

- Terraform: сеть, узлы, хранилище, DNS, TLS, объектное хранилище. Состояние — в удалённом бэкенде с блокировками.
- Helm: чарты приложения, `values` на окружение, `helm lint` и `helm template` в CI.
- Три окружения с раздельными состояниями и секретами. Production недоступен из CI без approval.
- Ручных изменений в production нет. Дрейф детектируется `terraform plan` по расписанию с алертом.

---

## 7. Gate фазы 9

```
[ ] docker build, образ прошёл Trivy без critical — вывод вставлен
[ ] helm lint и helm template чистые
[ ] terraform plan на staging без ошибок
[ ] Кластер staging поднят, приложение работает, смоук пройден
[ ] Убит под API → трафик не потерян (замер во время k6)
[ ] Убит primary PostgreSQL → автоматический failover, RTO замерен и вставлен
[ ] Восстановление из бэкапа выполнено, контрольные суммы сошлись, RTO замерен
[ ] PITR на произвольную точку выполнен, вывод вставлен
[ ] PgBouncer в transaction pooling: RLS-изоляция проверена тестом под пулером
[ ] Откат релиза выполнен в staging, вывод вставлен
[ ] NetworkPolicy проверены: под фронтенда не достучался до PostgreSQL
[ ] Секретов в git и в образе нет — gitleaks и Trivy secret scan чистые
[ ] resources.requests и limits обоснованы замерами фазы 8
```
