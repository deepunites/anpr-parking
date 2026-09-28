# RUNBOOK

Сценарии эксплуатации. На каждый алерт из `deploy/prometheus/alerts.yaml` ведёт `runbook_url` с якорем раздела
(`<a id="…">`); тест `tests/observability/test_alerts_and_dashboards.py` проверяет, что якорь существует.
Структура каждого сценария едина (фаза 10 §1): **Обнаружение → Влияние → Немедленное → Диагностика → Восстановление →
Проверка → Откат → После инцидента**. Каждая команда копируется как есть, ожидаемый вывод приведён. Часть A —
15 обязательных сценариев, часть B — остальные алерты, часть C — фактические логи трёх прогонов на стенде.

Соглашения: `$NS` — namespace релиза (`parking` / `parking-staging`), `$PGB` — HAProxy/PgBouncer ВМ (`10.10.20.21`),
`$REDIS` — `redis-cli -h $(redis-cli -h 10.10.21.11 -p 26379 SENTINEL get-master-addr-by-name parking | head -1) -p 6379 -a "$REDIS_PASSWORD"` (текущий master по Sentinel), `$SENTINEL` — `redis-cli -h 10.10.21.11 -p 26379 -a "$SENTINEL_PASSWORD"`, `psql` — через HAProxy primary `psql "postgresql://ops@$PGB:5000/parking"`
(роль `ops`: только SELECT + функции обслуживания), `$API` — `https://parking.example.uz`, токен оператора — `$TOKEN`
(`POST /api/v1/auth/login`). Логи — Loki: `{app="parking"} | json | correlation_id="<uuid>"`. Метрики — Prometheus
(`job="parking-api"`, `job="parking-worker"`). Все ручные действия через API требуют `reason` (I-15) и попадают в `audit_log`.

## Часть A. Обязательные сценарии

<a id="postgres-primary-down"></a>
### 1. PostgreSQL primary недоступен (`PostgresPrimaryDown`)

- **Обнаружение.** Алерт `PostgresPrimaryDown` (`dependency_up{dependency="postgres"}==0` или `offline_mode_active==1` 1 мин); дашборд *Pipeline* → «Зависимости»; лог `{app="parking"} |= "anpr_db_unavailable"` и `offline_mode_entered`; на HAProxy `http://$PGB:7000/` backend `primary` без зелёного сервера.
- **Влияние.** Водитель: въезд принимается в оффлайн-буфер (камера получает 202), шлагбаум въезда открывается по кешу ключей; **выезд с оплатой не работает** (сага требует БД), охранник открывает вручную. Оператор: кассы, история, отчёты → 503 `DEPENDENCY_UNAVAILABLE`. Бизнес: оплаты за время сбоя не принимаются, выручка теряется на время сбоя, события не теряются.
- **Немедленное (5 минут).**
  ```bash
  patronictl -c /etc/patroni/patroni.yml list           # на любой ВМ PostgreSQL
  ```
  Ожидаемо при штатном failover (в течение ttl 30 с + loop_wait 10 с):
  ```
  | Member | Host        | Role         | State     | TL | Lag in MB |
  | pg-1   | 10.10.20.11 | Replica      | running   |  2 |         0 |   ← бывший primary (или отсутствует, если ВМ потеряна)
  | pg-2   | 10.10.20.12 | Leader       | running   |  2 |           |
  | pg-3   | 10.10.20.13 | Sync Standby | streaming |  2 |         0 |
  ```
  Если `Leader` есть — ничего не делать: API закрывает breaker сам (пробы каждые 5 с), replayer воспроизводит буфер. Если `Leader` **нет** дольше 60 с:
  ```bash
  patronictl -c /etc/patroni/patroni.yml failover --candidate pg-2 --force   # ручной failover на sync standby
  ```
  Ожидаемо: `Successfully failed over to "pg-2"`.
- **Диагностика.**
  ```bash
  curl -s $API/health/ready ; echo
  ```
  `{"status":"degraded","ready":true,"checks":{"postgresql":"error: DBAPIError","redis":"ok"}}` — под остаётся в Service (A-60).
  ```bash
  $REDIS XLEN anpr:offline                      # растёт во время сбоя, → 0 после
  curl -s $API/metrics | grep -E "^offline_(mode_active|queue_length)"
  ssh pg-1 'sudo journalctl -u patroni --since "-15 min" | tail -20'   # причина: OOM, диск, сеть
  ssh pg-1 'df -h /var/lib/postgresql'                                  # см. сценарий 13
  ```
- **Восстановление.** (1) убедиться в `Leader` (выше); (2) вернуть потерянный узел: `systemctl start patroni` на нём — Patroni сам сделает `pg_rewind`/basebackup и он станет `Replica`; (3) дождаться replay буфера:
  ```bash
  $REDIS XLEN anpr:offline            # → 0
  curl -s http://<replayer-pod>:9464/metrics | grep ^offline_replayed_total   # offline_replayed_total{result="ok"} растёт
  ```
  (4) проверить, что sync standby назначен (`Sync Standby` в `list`), иначе `remote_apply` для `app_critical` ждёт: `patronictl edit-config` → `synchronous_mode: true`.
- **Проверка.** `dependency_up{dependency="postgres"}==1` на всех репликах API ≥ 2 мин; тестовое событие камеры отвечает 200 `ENTRY_CONFIRMED` (`backend/scripts/smoke_staging.py`); `SELECT count(*) FROM anpr_event WHERE queued_offline AND received_at > now() - interval '1 hour'` = числу событий из буфера.
- **Откат.** Если новый primary ведёт себя неправильно (ошибки данных): `patronictl switchover --candidate <старый>` после его возврата; буфер `anpr:offline` **не удалять** до полного replay; `patronictl reinit pg-N` только для реплики, не для лидера.
- **После инцидента.** Длительность оффлайна, число событий из буфера, число выездов, открытых вручную (`SELECT count(*) FROM exit_saga WHERE state='MANUAL_REVIEW' AND created_at BETWEEN …`), причина потери узла; при потере ВМ — Terraform `plan` покажет дрейф, пересоздать ВМ через pipeline.
- **Прогон на стенде:** часть C, лог 1.

<a id="postgres-replica-lag"></a>
### 2. Лаг синхронной реплики растёт / реплика отвалилась (`PostgresReplicaLag`)

- **Обнаружение.** `PostgresReplicaLag` (`pg_replication_lag_seconds > 30` 2 мин или метрика отсутствует); дашборд *Pipeline* → «Репликация»; `PaymentConfirmSlow` как симптом (коммиты `app_critical` ждут `remote_apply`).
- **Влияние.** Водитель: подтверждение оплаты и открытие шлагбаума на выезде замедляются до `statement_timeout` (5 с) → отказы саг в `MANUAL_REVIEW`. Оператор: кассовые операции «висят». Бизнес: при отвале реплики и `synchronous_mode_strict: false` primary продолжает писать без синхронной копии — окно RPO > 0.
- **Немедленное.**
  ```bash
  patronictl -c /etc/patroni/patroni.yml list
  psql "postgresql://ops@$PGB:5000/parking" -c "SELECT application_name, state, sync_state, write_lag, flush_lag, replay_lag FROM pg_stat_replication"
  ```
  Ожидаемо здоровое: одна строка `sync_state=sync`, `replay_lag < 00:00:01`. Если строки нет — реплика отвалилась; если `replay_lag` растёт — реплика не успевает (диск/CPU/долгий запрос на ней).
- **Диагностика.**
  ```bash
  ssh pg-3 'sudo journalctl -u patroni --since "-30 min" | grep -iE "error|fatal|rewind" | tail'
  ssh pg-3 'df -h /var/lib/postgresql; iostat -x 1 3 | tail -5'
  psql "postgresql://ops@10.10.20.13:5432/parking" -c "SELECT pid, now()-xact_start AS age, query FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC LIMIT 5"   # долгие запросы отчётов на реплике
  ```
- **Восстановление.** Реплика отвалилась: `ssh pg-3 sudo systemctl restart patroni` → Patroni догоняет из архива WAL (pgBackRest) или делает `pg_rewind`; если `list` показывает `start failed` — `patronictl reinit pg-3` (полный basebackup, ~10 мин на 50 ГБ). Реплика не успевает: убить долгий запрос (`SELECT pg_terminate_backend(<pid>)`), увеличить `max_standby_streaming_delay` только временно. Sync standby отсутствует и запись стоит (strict-режим): `patronictl edit-config` → `synchronous_mode_strict: false` (осознанное понижение durability, зафиксировать в инциденте).
- **Проверка.** `pg_replication_lag_seconds < 5` 5 мин; `payment_confirm_latency_seconds` p95 < 2 с; `patronictl list` показывает `Sync Standby`.
- **Откат.** Если перезапуск реплики нагрузил primary (IO): остановить реплику (`systemctl stop patroni` на ней), оставить async до окна обслуживания; `synchronous_mode_strict` вернуть после.
- **После инцидента.** Период без синхронной реплики (RPO-риск), причина лага, размер узла.

<a id="redis-down"></a>
### 3. Redis недоступен (`RedisDown`)

- **Обнаружение.** `RedisDown` (`dependency_up{dependency="redis"}==0` 1 мин); readiness API 503 (поды уходят из Service — Redis обязателен); логи `rate_limiter_unavailable`, `camera_auth_cache_unavailable`, `worker_loop_crashed{error_type=ConnectionError}`.
- **Влияние.** Водитель: **ingest работает** (ключи камер — из БД, rate-limit fail-open, идемпотентность — advisory lock БД), но оффлайн-буфера нет: если одновременно ляжет БД — камеры получают 503. Саги стоят (стримы не публикуются), live-панель не обновляется, кеш решений доступа мимо (медленнее на 1 запрос к БД). Оператор: WS «stale», операции через API работают. Бизнес: выезды с оплатой задерживаются на время сбоя (outbox копит события — `OutboxLag`).
- **Немедленное.**
  ```bash
  $REDIS PING                      # ожидаемо PONG; при сбое: Could not connect to Redis at …: Connection refused
  $SENTINEL SENTINEL get-master-addr-by-name parking      # ожидаемо: 1) "10.10.21.12" 2) "6379" — текущий master
  $SENTINEL SENTINEL masters | grep -E "^(name|ip|flags|num-other-sentinels|quorum)$" -A1
  ssh redis-1 systemctl status redis-server redis-sentinel --no-pager | grep -E "Active:"
  ```
  Sentinel переключает master сам за `down-after-milliseconds` 5 с + выборы (< 30 с; учение на локальной лаборатории — 2.5 с,
  `tests/failure/test_redis_sentinel.py`). Клиенты (`redis+sentinel://`) переподключаются без рестарта. Если master
  не выбран (нет кворума 2/3 — две ВМ Redis недоступны): поднять хотя бы одну ВМ (`systemctl start redis-server redis-sentinel`)
  либо принудительно `$SENTINEL SENTINEL failover parking` при живом кворуме.
- **Диагностика.**
  ```bash
  $REDIS INFO memory | grep -E "used_memory_human|maxmemory_policy"   # ожидаемо volatile-lru: вытесняются только кеши с TTL, стримы/буфер без TTL — никогда
  $REDIS INFO persistence | grep -E "aof_enabled|rdb_last_bgsave_status"
  $REDIS XLEN events:0 ; $REDIS XPENDING events:0 core
  ```
- **Восстановление.** После возврата Redis ничего вручную: publisher продолжает публикацию из outbox (порядок сохранён), consumer/replayer перезапускают цикл сами (супервизор, бэкофф 1→30 с — `worker_loop_restarts_total`), API-кеши прогреваются. Потеря данных Redis (AOF выключен): стримы восстанавливаются из outbox — `UPDATE outbox SET status='PENDING', published_at=NULL WHERE published_at > '<момент потери>'` от роли `app_maintenance`; потребители идемпотентны по `consumer_inbox`. Оффлайн-буфер `anpr:offline` при потере Redis теряется — события из него **не восстановимы** (камеры ретранслируют по своему буферу, A-01).
- **Проверка.** `dependency_up{dependency="redis"}==1` (обновляется раз в 15 с); `curl -s $API/health/ready` → `"ready":true`; `outbox_oldest_pending_age_seconds → 0`; `worker_loop_restarts_total` не растёт; тестовое событие камеры → 200.
- **Откат.** Если новый master пуст и consumer «догоняет» с нуля — это штатно; если переопубликовали outbox дважды — дубликаты отсекает inbox, действий не требуется.
- **После инцидента.** Длительность, объём переопубликованного, `maxmemory` vs использование, проверить включённость AOF (`appendonly yes`, `appendfsync everysec`).
- **Прогон на стенде:** часть C, лог 2 (в первом прогоне обнаружено и исправлено: consumer падал при отказе Redis).

<a id="barrier-failure"></a>
### 4. Шлагбаум не открывается на конкретной площадке (`BarrierFailure`)

- **Обнаружение.** `BarrierFailure` (`barrier_command_total{result="FAILED"}` > 3 за 5 мин на `lot`); `ExitSuccessRatioLow`; лог `barrier_attempt_failed` с `barrier_id`, `error`; звонок охранника площадки.
- **Влияние.** Водитель: стрела не поднимается, очередь на выезде/въезде. Оператор: сессии уходят в `MANUAL_REVIEW` (`EXIT_BARRIER_FAILED`), очередь `review-queue` растёт. Бизнес: оплаты сохранены (ADR-12), но недовольство и ручная работа.
- **Немедленное.** Открыть вручную для стоящих машин (оператор, I-15):
  ```bash
  curl -s -X POST $API/api/v1/sessions/<session_id>/open-exit -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"reason":"шлагбаум не отвечает, открыт по звонку охраны"}'
  ```
  Ожидаемо `200` с `"state":"MANUAL_REVIEW"` и `manual_command_id`; если контроллер мёртв — охранник открывает кнопкой, сессию закрыть: `POST /api/v1/sessions/<id>/close` `{"reason":"открыт ключом"}`.
- **Диагностика.**
  ```bash
  curl -s "$API/api/v1/barriers?lot_id=<lot>" -H "Authorization: Bearer $TOKEN" | jq '.[] | {id,name,endpoint,driver,breaker_state}'
  curl -s "$API/api/v1/sessions/review-queue?lot_id=<lot>" -H "Authorization: Bearer $TOKEN" | jq length
  psql "postgresql://ops@$PGB:5001/parking" -c "SELECT state, last_error, count(*) FROM barrier_command WHERE lot_id='<lot>' AND created_at > now()-interval '1 hour' GROUP BY 1,2"
  ```
  Ожидаемо при отказе: `state=FAILED, last_error='ConnectError'` (сеть/VPN до площадки) или `'controller timeout'` (контроллер завис) или `'HTTP 500'`. Проверить сеть с пода: `kubectl -n $NS exec deploy/parking-celery-exit -- python -c "import socket; socket.create_connection(('<ip контроллера>', 80), 3)"` — `ConnectionRefused`/`timeout` = сеть или NetworkPolicy (`barriers.cidrs`).
- **Восстановление.** Сеть/VPN площадки — сетевой инженер; контроллер завис — перезагрузить по питанию (охрана); после этого breaker (5 отказов → OPEN 60 с) закроется сам по пробе; ручные саги закрыть через `open-exit`.
- **Проверка.** `barrier_command_total{result="OK"}` растёт на площадке; `curl -X POST $API/api/v1/barriers/<id>/open -d '{"reason":"тест после ремонта"}'` → 200 `OK`; `review-queue` пуст.
- **Откат.** Не применимо (ручной режим площадки остаётся, пока не починено).
- **После инцидента.** Число ручных открытий (`audit_log action='BARRIER_OPEN_MANUAL'`), длительность, вендор контроллера; если повторяется — заменить контроллер, проверить `barrier_timeout_seconds`.

<a id="camera-offline"></a>
### 5. Камера не присылает события (`CameraOffline`)

- **Обнаружение.** `CameraOffline` (`camera_status_total{status="OFFLINE"|"SILENT"}` > 0 на площадке 10 мин); дашборд *Operations* → «Камеры»; `LotWithoutCameras` — площадка без единой живой камеры.
- **Влияние.** Водитель: въезд/выезд не фиксируется — на въезде шлагбаум по камере не откроется (охранник), на выезде сессии не закрываются → позже «поздние» события или orphan. Бизнес: потеря выручки за неучтённые выезды.
- **Немедленное.**
  ```bash
  curl -s "$API/api/v1/cameras?lot_id=<lot>" -H "Authorization: Bearer $TOKEN" | jq '.[] | {code,status,last_heartbeat_at,last_event_at}'
  ```
  `status:"OFFLINE"` — нет heartbeat > 2 мин (камера/сеть); `"SILENT"` — heartbeat есть, событий нет дольше обычного (распознавание/ключ). Позвонить охране: камера включена? свет? Открывать по кнопке до починки.
- **Диагностика.**
  ```bash
  {app="parking"} | json | event="camera_auth_failed" | camera_code="<code>"      # Loki: неверный/отозванный ключ
  curl -s $API/metrics | grep -E '^camera_event_rejected_total.*reason="(RATE_LIMITED|REPLAY|INVALID_PLATE)"'
  ssh camera-gw 'ping -c 3 <ip камеры>'
  ```
  Ключ отозван/просрочен → выпустить новый (`POST /api/v1/cameras/<id>/keys`) и прописать в камере; `REPLAY` → часы камеры (сценарий `CameraClockSkew`); `RATE_LIMITED` → камера шлёт дубли, проверить прошивку.
- **Восстановление.** Сеть/камера — техник площадки; после возврата камера отправляет буфер (события с `late=true`, шлагбаум не трогают); orphan-сессии закрыть оператором (`POST /sessions/<id>/orphan`).
- **Проверка.** `camera_status_total{status="ONLINE"}` для камеры; `camera_event_total` растёт; `last_event_at` свежий.
- **Откат.** Не применимо.
- **После инцидента.** Длительность, число `late` событий и orphan-сессий, замена оборудования.

<a id="payments-failing"></a>
### 6. Платёжный провайдер недоступен (`PaymentsFailing`)

- **Обнаружение.** `PaymentsFailing` (`gateway_calls_total{result="error"|"timeout"}` > 20 % за 5 мин по провайдеру); лог `provider_timeout`, `webhook_rejected`; жалобы кассиров «оплата не проходит».
- **Влияние.** Водитель: онлайн-оплата (Payme/Click) не проходит — **наличные в кассе работают** (первый класс, CORE). Оператор: очередь на кассе. Бизнес: комиссия провайдера не теряется, выручка перетекает в наличные; саги с `PAYMENT_PENDING_VERIFICATION` ждут до 5 мин, затем `MANUAL_REVIEW`.
- **Немедленное.** Объявить кассирам: принимать наличные (`POST /payments/<id>/confirm-cash`). Проверить провайдера снаружи:
  ```bash
  curl -s -o /dev/null -w "%{http_code} %{time_total}\n" https://checkout.paycom.uz/   # ожидаемо 200 < 1 с
  curl -s $API/metrics | grep -E '^gateway_calls_total\{provider="PAYME"'
  ```
- **Диагностика.** Таймауты только у нас (`gateway_calls_total{result="timeout"}`) при работающем провайдере → сеть/NetworkPolicy `providers` (443) или DNS: `kubectl -n $NS exec deploy/parking-celery-payments -- python -c "import httpx;print(httpx.get('https://checkout.paycom.uz', timeout=5).status_code)"`. Ошибки подписи вебхуков (`webhook_rejected reason=SIGNATURE`) → ключ провайдера ротирован — обновить в Vault `kv/parking/production/app` (`PARKING_SECRETS_JSON`), ESO обновит Secret за ≤ 1 ч (`refreshInterval`), поды перечитают при рестарте.
- **Восстановление.** Провайдер вернулся → джобы `payments-verify` (раз в 60 с) дозапрашивают статусы попыток, `payments-orphans` применяет отложенные вебхуки; ничего вручную. Саги в `MANUAL_REVIEW` c `EXIT_PAYMENT_UNVERIFIED`: сверить платёж у провайдера, затем `open-exit` (деньги списаны) или `confirm-cash`.
- **Проверка.** `gateway_calls_total{result="ok"}` растёт; тестовый чекаут через staging; `SELECT count(*) FROM payment_attempt WHERE status='PENDING' AND created_at < now()-interval '10 min'` → 0.
- **Откат.** Не применимо; при затяжном сбое — отключить провайдера для UI (`provider_account.active=false` через админ-API) чтобы не плодить PENDING.
- **После инцидента.** Длительность, число платежей, ушедших в наличные, расхождения в сверке (сценарий 10).

<a id="dlq-growing"></a>
### 7. Накопление DLQ (`DlqGrowing`)

- **Обнаружение.** `DlqGrowing` (`dlq_length{stream="events:dlq"|"anpr:dlq"} > 0` 10 мин или `rate(dlq_messages_total[15m]) > 0`); лог `consumer_dlq` с `event_type`, `error`.
- **Влияние.** Водитель: события саги/сессии не обработаны → выезд «висит» в `ACCESS_RESOLVED`/`BARRIER_OPENING` (recover-джоб через 30 с уводит в `MANUAL_REVIEW`), live-панель отстаёт. `anpr:dlq` — события камер, не попавшие в БД после replay (I-4: они не потеряны, но сессия не создана).
- **Немедленное.**
  ```bash
  $REDIS XLEN events:dlq ; $REDIS XLEN anpr:dlq
  $REDIS XREVRANGE events:dlq + - COUNT 3      # поля error, event_type, source_stream, attempt
  ```
  Ожидаемо: `error` вида `ConnectionRefusedError: …`/`TimeoutError` (транзитный сбой БД — replay после восстановления) или `NotFoundError: saga not found` (событие чужого/удалённого кластера — не переигрывать) или `ValidationError` (несовместимый контракт — багфикс).
- **Диагностика.**
  ```bash
  curl -s "$API/api/v1/admin/dlq?stream=events&limit=20" -H "Authorization: Bearer $PLATFORM_TOKEN" | jq '.[] | {id, event_type: .fields.event_type, error: .fields.error}'
  {app="parking",component="consumer"} | json | event="consumer_handler_failed" | error_type != ""     # Loki: кто и почему
  ```
- **Восстановление.** Устранить причину (сценарий 1/3, багфикс), затем replay — сначала dry-run, затем боевой, только PLATFORM_ADMIN, с reason:
  ```bash
  curl -s -X POST $API/api/v1/admin/dlq/replay -H "Authorization: Bearer $PLATFORM_TOKEN" -H 'Content-Type: application/json' -d '{"stream":"events","dry_run":true,"reason":"после восстановления БД"}'
  ```
  `{"dry_run":true,"count":N,…}` → повторить с `"dry_run":false` → `{"dry_run":false,"count":N}`. Потребители идемпотентны (`consumer_inbox`), повторная обработка безопасна. Не подлежащие replay записи (`NotFoundError` чужого кластера) удалить: `$REDIS XDEL events:dlq <id>` с записью причины в инцидент.
- **Проверка.** `XLEN events:dlq` → 0; `dlq_length` → 0; `consumer_processed_total{result="ok"}` вырос на N; сессии из событий дошли до терминала.
- **Откат.** Replay создал дубликаты эффектов (не должен): остановить consumer (`kubectl scale deploy/parking-consumer --replicas=0`), разобрать по `correlation_id`, откатить вручную по `audit_log`.
- **После инцидента.** Причина, число записей, время между появлением и replay; если DLQ из-за контракта — добавить тест в `tests/workers`.
- **Прогон на стенде:** часть C, лог 3.

<a id="outbox-lag"></a>
### 8. Лаг outbox растёт (`OutboxLag`)

- **Обнаружение.** `OutboxLag` (`outbox_oldest_pending_age_seconds > 30` 2 мин); дашборд *Pipeline* → «Outbox»; лог publisher: `outbox_publish_failed`, `leadership_lost`.
- **Влияние.** Водитель: сага выезда не стартует (`parking.exit.requested` не опубликован) → после оплаты шлагбаум не открывается, пока лаг не сойдёт; live-панель не обновляется. Бизнес: очереди на выезде.
- **Немедленное.**
  ```bash
  kubectl -n $NS get pods -l app.kubernetes.io/component=publisher      # ожидаемо 2 Running
  kubectl -n $NS logs deploy/parking-publisher --tail=30 | grep -E "leader|outbox_publish"
  psql "postgresql://ops@$PGB:5000/parking" -c "SELECT status, count(*), now()-min(created_at) AS oldest FROM outbox GROUP BY 1"
  ```
  Ожидаемо здоровое: `PENDING` ≤ десятков, `oldest < 5 s`. Ни одного лидера (`is_leader=false` у обоих) → advisory lock держит зависшее соединение: `SELECT pid, application_name, state, now()-state_change FROM pg_stat_activity WHERE pid IN (SELECT pid FROM pg_locks WHERE locktype='advisory')`; `SELECT pg_terminate_backend(<pid>)` для мёртвого.
- **Диагностика.** Redis недоступен (сценарий 3) — самая частая причина; `FAILED` в outbox (`status='FAILED'`) — невалидный payload (`outbox_failed_total`): `SELECT event_type, last_error FROM outbox WHERE status='FAILED' LIMIT 5`.
- **Восстановление.** Redis вернулся → publisher догоняет сам (батчи по 500, `outbox_publish_lag_seconds`); publisher завис → `kubectl -n $NS rollout restart deploy/parking-publisher`; `FAILED` события — исправить причину, `UPDATE outbox SET status='PENDING', attempts=0 WHERE status='FAILED' AND event_type='…'` (`app_maintenance`).
- **Проверка.** `outbox_oldest_pending_age_seconds < 5`; `outbox_published_total` растёт; `stream_pending_total` не растёт.
- **Откат.** Не применимо.
- **После инцидента.** Длительность лага, максимальный размер очереди, была ли потеря лидерства без причины (тайм-аут advisory lock — проверить `idle_in_transaction_session_timeout` на maintenance-соединении).

<a id="double-charge"></a>
### 9. Двойное списание у клиента

- **Обнаружение.** Обращение клиента/провайдера; алерт `ReconciliationBacklog` с `kind=DUPLICATE_REMOTE`; `SELECT * FROM reconciliation_discrepancy WHERE kind='DUPLICATE_REMOTE' AND status='OPEN'`; лог `webhook_duplicate_payment`.
- **Влияние.** Клиент: списано дважды за одну сессию. Бизнес: обязанность вернуть, репутация. Система: второй платёж уже `PAID` и лежит в очереди возврата (по построению: два `click_trans_id` на один заказ → второй принят и помечен `DUPLICATE_REMOTE`, PAYMENTS §6).
- **Немедленное.** Найти оба платежа:
  ```bash
  psql "postgresql://ops@$PGB:5001/parking" -c "SELECT p.id, p.status, p.method, p.amount, p.paid_at, d.kind, d.status AS discrepancy FROM payment p LEFT JOIN reconciliation_discrepancy d ON d.payment_id=p.id WHERE p.session_id='<session_id>' ORDER BY p.created_at"
  ```
  Ожидаемо: первая строка `PAID` без расхождения, вторая `PAID` + `DUPLICATE_REMOTE OPEN`. Если второй платёж не помечен — открыть расхождение вручную через `POST /api/v1/reconciliations` (сверка за день) и повторить запрос.
- **Диагностика.** Кто списал дважды: `SELECT provider, provider_transaction_id, http_status, response_payload->>'error' FROM payment_attempt WHERE payment_id IN (…)`; `webhook_inbox` по `provider_event_id` — сколько раз доставлен и с каким ответом (5 одинаковых вебхуков → один эффект, PAYMENTS §3; двойное списание = **два разных** `trans_id`).
- **Восстановление.** Возврат второго платежа (MANAGER, `payment:refund`, I-15):
  ```bash
  curl -s -X POST $API/api/v1/payments/<second_payment_id>/refunds -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"amount_minor":<amount>,"reason":"двойное списание, расхождение <discrepancy_id>"}'
  ```
  Ожидаемо `201` `{"status":"PENDING"…}`; джоб `payments-refunds` (раз в 60 с) проводит возврат у провайдера → `REFUNDED`; затем закрыть расхождение: `POST /api/v1/reconciliation-discrepancies/<id>/resolve` `{"status":"RESOLVED","reason":"возврат <refund_id>"}`.
- **Проверка.** `SELECT status FROM refund WHERE payment_id='<second>'` → `DONE`; у провайдера транзакция `cancelled/refunded`; клиенту ответ; `reconciliation_discrepancies_open{kind="DUPLICATE_REMOTE"}` уменьшился.
- **Откат.** Возврат не прошёл у провайдера (`refund.status=FAILED`, `last_error`) — повторить через 15 мин или вернуть наличными из кассы (`confirm-cash` с отрицательной суммой запрещён — только `refunds` c `method=CASH`), зафиксировать в инциденте.
- **После инцидента.** Причина дубля (клиент нажал дважды / провайдер ретранслировал с новым id), число случаев за месяц; если > 5 — договориться с провайдером об идемпотентности по `merchant_trans_id`.

<a id="reconciliation-provider-favor"></a>
### 10. Расхождение сверки в пользу провайдера

- **Обнаружение.** Ежедневная сверка (`payments-reconcile`, 03:00) → `reconciliation_discrepancy` с `kind IN ('MISSING_LOCAL','AMOUNT_MISMATCH')` где у провайдера сумма/платёж есть, у нас нет или меньше; алерт `ReconciliationBacklog` (открытых > 20 или старше 48 ч); дашборд *Business* → «Сверка».
- **Влияние.** Бизнес: деньги списаны с клиента, но сессия не закрыта/платёж не учтён → клиент мог остаться перед закрытым шлагбаумом (сценарий 6), выручка занижена в отчётах; налоговые/бухгалтерские риски при систематическом расхождении.
- **Немедленное.**
  ```bash
  curl -s "$API/api/v1/reconciliation-discrepancies?status=OPEN" -H "Authorization: Bearer $TOKEN" | jq '.items[] | {id,kind,payment_id,local_amount,remote_amount,remote_transaction_id}'
  ```
  `MISSING_LOCAL` — у провайдера есть `PAID`-транзакция, у нас нет платежа: найти по `remote_transaction_id` в `webhook_inbox` (вебхук не дошёл/отклонён по подписи?) — `SELECT status, attempts, last_error FROM webhook_inbox WHERE provider_event_id LIKE '%<trans_id>%'`.
- **Диагностика.** `webhook_inbox.status='REJECTED'` + `last_error='SIGNATURE'` → ротация ключа провайдера (сценарий 6); `status='PENDING'` (orphan: вебхук пришёл раньше платежа) → джоб `payments-orphans` применит сам; нет строки вообще → провайдер не доставил вебхук: запросить статус вручную `POST /api/v1/payments/<id>/checkout` не нужно — использовать `GET /api/v1/payments/<payment_id>` после `payments-verify`.
- **Восстановление.** Для `MISSING_LOCAL` с подтверждённой оплатой у провайдера: оператор фиксирует платёж через `resolve` с `status=RESOLVED` и `reason` (ссылка на транзакцию провайдера) — сага закрывается через `open-exit`, если машина ещё на площадке; для `AMOUNT_MISMATCH` — возврат разницы (сценарий 9) или доначисление наличными по согласованию с клиентом. Массовое расхождение за день (> 1 % платежей) — остановить онлайн-оплату (`provider_account.active=false`) до выяснения.
- **Проверка.** Повторная сверка за тот же период: `POST /api/v1/reconciliations` `{"provider":"PAYME","period_start":"<date>","period_end":"<date>","reason":"повтор после разбора"}` → `matched_count` = `local_count` = `remote_count`, новых расхождений 0.
- **Откат.** Ошибочно закрытое расхождение — открыть заново через `resolve` `{"status":"OPEN","reason":…}`; платежи не мутируются (I-6), только возвраты/новые записи.
- **После инцидента.** Доля расхождений по провайдеру за месяц, корневая причина (сеть вебхуков, ключи), SLA провайдера на доставку вебхуков.

<a id="camera-key-compromise"></a>
### 11. Подозрение на компрометацию ключа камеры

- **Обнаружение.** Всплеск `camera_event_total` с одной камеры вне её обычного профиля (SILENT/EWMA), события с IP не площадки (`audit_log` не хранит IP камер — смотреть ingress-логи по `X-Camera-Key` prefix), `camera_auth_abuse_total` (перебор ключей с IP), сообщение вендора/сотрудника об утечке конфигурации камеры, событие без соответствующей записи на видеорегистраторе.
- **Влияние.** Злоумышленник с ключом может: создавать фиктивные ENTRY/EXIT (искажать занятость, открывать шлагбаум выезда для чужой машины по «своему» EXIT — только если сессия существует и оплачена/льготна), подменять `event_time` (ограничено окном `max_clock_skew`), не может: читать данные, платить, менять правила (ключ даёт только `POST /camera/events|heartbeat|media`). Ключ привязан к одной камере и площадке — ущерб локален.
- **Немедленное.** Ротировать ключ немедленно (старый живёт grace-окно 10 мин для перепрошивки камеры) или отозвать сразу:
  ```bash
  curl -s -X POST $API/api/v1/camera-keys/<key_id>/revoke -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"reason":"подозрение на компрометацию, инцидент INC-…"}'
  curl -s -X POST $API/api/v1/cameras/<camera_id>/keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"label":"после инцидента","reason":"замена скомпрометированного ключа"}'
  ```
  Ожидаемо: `200 {"revoked_at":"…"}`, затем `201 {"raw":"<новый ключ, показывается один раз>"}`. Кеш кандидатов инвалидируется событием `devices.camera_key.revoked.v1` (≤ 1 с); старый ключ получает 401.
- **Диагностика.** Что успел сделать ключ:
  ```bash
  psql "postgresql://ops@$PGB:5001/parking" -c "SELECT event_type, result, count(*), min(event_time), max(event_time) FROM anpr_event WHERE camera_id='<camera_id>' AND received_at > now()-interval '24 hours' GROUP BY 1,2"
  psql "postgresql://ops@$PGB:5001/parking" -c "SELECT id, status, entry_time, exit_time FROM parking_session WHERE lot_id='<lot>' AND created_at > '<время подозрения>' ORDER BY created_at"
  ```
  Ингресс-логи: запросы с `X-Camera-Key` префиксом ключа (первые 16 символов) с IP вне VPN площадки. Сравнить с видеоархивом камеры.
- **Восстановление.** Фиктивные сессии — `POST /sessions/<id>/orphan` или `close` с `reason` (аудит); фиктивные бесплатные выезды — оценить ущерб, при необходимости `BLACKLIST` номеров-участников (`POST /access-rules`). Пепперы ключей (`PARKING_CAMERA_KEY_PEPPER`) **не ротируются** по одному инциденту (все ключи всех камер) — только при компрометации Vault (THREAT_MODEL T-11).
- **Проверка.** Старый ключ → `401 invalid camera key` (`camera_auth_failed_total` растёт при попытках); новая камера шлёт с новым ключом (`camera_status ONLINE`).
- **Откат.** Ключ был отозван ошибочно → новый ключ уже прописан, отзыв не отменяется (аудит), выпустить ещё один.
- **После инцидента.** Как утёк ключ (конфиг камеры, вендорский доступ), проверить остальные ключи площадки, включить срок жизни ключей (`expires_in` 180 дней), отчёт о фиктивных событиях.

<a id="tenant-data-leak"></a>
### 12. Подозрение на утечку данных tenant

- **Обнаружение.** Обращение tenant («вижу чужие сессии»), ответ API с чужим `tenant_id` в теле, всплеск `403/404` от одного пользователя на чужих id (перебор), аномальные `GET /sessions/search` с высокой частотой (`http_requests_total{route="/api/v1/sessions/search"}`), тест `tests/security/test_cross_tenant_matrix.py` красный в CI.
- **Влияние.** ПДн (номера, кадры) одного tenant доступны другому — инцидент по ЗРУ-547 с обязанностью уведомления регулятора и субъектов; репутация платформы.
- **Немедленное.** (1) Зафиксировать: `correlation_id` запроса из обращения → Loki `{app="parking"} | json | correlation_id="<uuid>"` — какой `tenant_id` в контексте и какой `user_id`; (2) ограничить: отозвать сессии пользователя `POST /api/v1/users/<id>/revoke-tokens` (все refresh) и при необходимости деактивировать; (3) если подозрение на баг RLS — переключить трафик в read-only? нет: остановить только подозрительный эндпоинт через ingress (`nginx.ingress.kubernetes.io/server-snippet` `location = /api/v1/… { return 503; }`).
- **Диагностика.** Проверить изоляцию прямо на primary под ролью приложения:
  ```bash
  psql "postgresql://app_critical@$PGB:5000/parking" -c "SELECT set_config('app.current_tenant_id','<tenant_b>',false); SELECT count(*) FROM parking_session WHERE tenant_id='<tenant_a>'"
  ```
  Ожидаемо `0` (RLS, I-2). Ненулевой результат = дефект политики — критический инцидент. Проверить, что у ролей нет `BYPASSRLS`: `SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname LIKE 'app_%'` → все `f`. Проверить пулер: `tests/db/test_pgbouncer_rls.py` на staging (session-level SET утекает — сценарий известен, приложение его не использует).
  Аудит доступа: `SELECT user_id, action, entity_id, created_at FROM audit_log WHERE tenant_id='<tenant_b>' AND created_at > '<T-24h>' ORDER BY created_at` и HTTP-логи пользователя.
- **Восстановление.** Дефект кода → хотфикс через pipeline (тест матрицы обязателен), до выкладки — блокировка эндпоинта; утечка через учётку (фишинг, общий пароль) → сброс пароля, ротация refresh, проверка MFA-политики. Уведомление: DPO/руководство в течение 24 ч; регулятор и субъекты — по ЗРУ-547 (срок и форму подтверждает юрист, A-67).
- **Проверка.** Матрица межтенантных проверок зелёная на staging и production-smoke; повтор запроса из обращения → 404; `tests/invariants/test_i02_tenant_isolation.py` зелёный.
- **Откат.** Хотфикс ухудшил доступность → `scripts/rollback.sh`, блокировка эндпоинта остаётся.
- **После инцидента.** Объём утечки (какие записи, сколько субъектов), корневая причина, постмортем с добавлением теста в матрицу, отчёт регулятору.

<a id="postgres-disk"></a>
### 13. Диск PostgreSQL заканчивается (`PostgresDiskAlmostFull`, `DiskUsageHigh`)

- **Обнаружение.** `DiskUsageHigh` (> 80 % 10 мин, `node_filesystem_avail_bytes` по `/var/lib/postgresql`), `PostgresDiskAlmostFull` (> 95 % — критический, до отказа записи); дашборд *Pipeline* → «Диск PostgreSQL».
- **Влияние.** При 100 %: PostgreSQL останавливает запись (`PANIC: could not write to file`), primary падает → сценарий 1 (оффлайн-режим камер, стоп оплат); WAL не архивируется → RPO растёт.
- **Немедленное.**
  ```bash
  ssh pg-1 'df -h /var/lib/postgresql; sudo du -sh /var/lib/postgresql/16/main/{base,pg_wal} /var/spool/pgbackrest'
  ```
  Ожидаемо: `pg_wal` ≤ `max_wal_size` (8 ГБ) + `wal_keep_size` (2 ГБ). `pg_wal` > 20 ГБ → архивация стоит (`archive_command` падает) или слот реплики держит WAL:
  ```bash
  psql "postgresql://ops@$PGB:5000/parking" -c "SELECT last_archived_wal, last_failed_wal, last_failed_time FROM pg_stat_archiver"
  psql "postgresql://ops@$PGB:5000/parking" -c "SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained FROM pg_replication_slots"
  ```
- **Диагностика.** Неактивный слот с большим `retained` — реплика отвалилась (сценарий 2) → `patronictl` вернуть реплику или удалить слот (`SELECT pg_drop_replication_slot('<name>')` — только если реплика будет пересоздана `reinit`); `last_failed_wal` не NULL → MinIO/pgBackRest недоступен (`pgbackrest check --stanza=parking`); рост `base` — раздутые горячие таблицы: `SELECT relname, n_dead_tup, last_autovacuum FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 5` (профиль 0013 должен держать `n_dead_tup` низким).
- **Восстановление.** Освободить: дропнуть просроченные партиции раньше расписания — `SELECT app.drop_range_partitions('anpr_idempotency', interval '2 days')`, `SELECT app.drop_range_partitions('outbox', interval '3 days')` (только `app_maintenance`); `VACUUM (VERBOSE) outbox` не освобождает место ОС — при необходимости `pg_repack`. Расширить диск ВМ: Terraform `data_disk_gb` → `tofu apply` (Proxmox расширяет онлайн) → `growpart`/`resize2fs` на ВМ (Ansible роль `base`). Никогда не удалять файлы из `pg_wal` вручную.
- **Проверка.** `node_filesystem_avail_bytes` > 30 %; `pg_stat_archiver.last_failed_wal` пуст; алерт погас.
- **Откат.** Дроп партиций необратим (данные старше ретеншена по построению); расширение диска — необратимо, но безопасно.
- **После инцидента.** Прогноз роста (`docs/COST.md` §хранилище), пересмотр `retention` и размера диска, алерт по `pg_replication_slots` неактивным > 1 ч.

<a id="failed-deploy-rollback"></a>
### 14. Провалившийся деплой, откат с применённой миграцией

- **Обнаружение.** `helm upgrade --atomic` завершился ошибкой (readiness gate не прошёл) или смоук `smoke_staging.py` упал после выкладки; `ApiErrorBudgetBurnFast` сразу после релиза; `kubectl -n $NS rollout status` не завершается.
- **Влияние.** Часть подов новой версии не готова — трафик идёт на старые (rolling, `maxUnavailable: 0`), т. е. водители/операторы обычно не замечают; если новая версия отдаёт 5xx на готовых подах — ошибки до отката.
- **Немедленное.**
  ```bash
  helm -n $NS history parking | tail -3
  kubectl -n $NS get pods -l app.kubernetes.io/instance=parking | grep -v Running
  kubectl -n $NS logs deploy/parking-api --tail=50 | grep -E "config_invalid|startup_failed|Traceback" | head
  kubectl -n $NS get jobs -l app.kubernetes.io/component=migrate ; kubectl -n $NS logs job/<parking-migrate-…> | tail -20
  ```
  `--atomic` уже откатил Helm-ревизию сам, если не прошёл readiness; иначе — одна команда:
  ```bash
  scripts/rollback.sh parking $NS        # helm rollback на предыдущую ревизию + rollout status + смоук
  ```
  Ожидаемо: `rollback parking/$NS: revision N → N-1`, `rolled back in 40s`, `SMOKE OK …`.
- **Диагностика: миграция уже применена.** `alembic current` в поде старой версии показывает head новой (`kubectl -n $NS exec deploy/parking-api -- alembic current`). Правило релизов: миграция обратносовместима в пределах релиза (expand → migrate → contract в трёх релизах, INFRA §5) — старый код работает с новой схемой, **откат схемы не делается**. Проверить, что миграция была expand: новые колонки nullable/с DEFAULT, новые таблицы, индексы `CONCURRENTLY`; `docs/DATABASE.md` и `migration_check.json` из CI.
- **Восстановление.** Если правило нарушено (contract-миграция удалила колонку/таблицу, старый код падает `UndefinedColumn`): **не** `alembic downgrade` в production (потеря данных, записанных новым кодом); вперёд: хотфикс-релиз, возвращающий совместимость (новая expand-миграция, воссоздающая колонку из бэкапа/по умолчанию) через pipeline с `migration-check`; до него — старый код на 5xx по затронутым эндпоинтам, остальное работает. Если миграция зависла (лок): `SELECT pid, state, wait_event_type, query FROM pg_stat_activity WHERE application_name LIKE 'alembic%'` → `pg_terminate_backend`; job перезапустится при следующем `helm upgrade`.
- **Проверка.** `helm -n $NS history parking` — статус `deployed` у нужной ревизии; смоук OK; `alembic current` = ожидаемый head; error budget не горит.
- **Откат отката.** Нужна именно новая версия (например, из-за данных, которые уже пишет новый код) → `helm -n $NS rollback parking <N>` обратно после хотфикса.
- **После инцидента.** Почему readiness/смоук не поймали в staging (rollback-drill в cd.yml прошёл?), нарушение правила expand/contract → добавить проверку в `migration_check` (список запрещённых DDL для big tables), запись в `docs/INFRA.md` §5.

<a id="manual-review-mass"></a>
### 15. Массовое зависание сессий в `MANUAL_REVIEW`

- **Обнаружение.** `ExitSuccessRatioLow`, рост `SELECT count(*) FROM exit_saga WHERE state='MANUAL_REVIEW'` (дашборд *Operations* → «Очередь ручного разбора»), очередь `GET /api/v1/sessions/review-queue` > 20 на площадку, жалобы операторов.
- **Влияние.** Водитель: выезд требует оператора (звонок/кнопка), очереди. Оператор: ручной разбор каждого. Бизнес: платежи не теряются (ADR-12), но ручные открытия без оплаты возможны — риск потерь.
- **Немедленное.** Определить причину по `review_reason`:
  ```bash
  psql "postgresql://ops@$PGB:5001/parking" -c "SELECT lot_id, review_reason, count(*), min(created_at) FROM exit_saga WHERE state='MANUAL_REVIEW' AND created_at > now()-interval '2 hours' GROUP BY 1,2 ORDER BY 3 DESC"
  ```
  | `review_reason` | Причина | Куда |
  |---|---|---|
  | `EXIT_BARRIER_FAILED` (`last_error='barrier command PENDING'`) | очередь consumer/Celery не успевает (лаг) — команда исполняется позже дедлайна 30 с | сценарии 8/`StreamConsumerLag`; после исполнения опоздавшей команды сессия закрывается сама (`OK_LATE`) |
  | `EXIT_BARRIER_FAILED` (`ConnectError`/`timeout`) | контроллер площадки | сценарий 4 |
  | `EXIT_PAYMENT_UNVERIFIED` | провайдер не подтвердил за 5 мин | сценарий 6 |
  | `EXIT_LOW_CONFIDENCE` | камера распознаёт плохо (грязь, ночь, прошивка) | сценарий 5, `confidence_review_min` |
  | `EXIT_BLACKLIST` | по построению (I-8) | штатно, оператор |
  | `UNKNOWN_EXIT` | нет сессии (въезд не зафиксирован) | сценарий 5 на камере въезда; политика площадки `unknown_exit_policy` |
- **Диагностика.** Лаг конвейера: `stream_pending_total`, `redis_stream_lag`, `celery_queue_length{queue="barrier"}`; `SELECT state, count(*) FROM barrier_command WHERE created_at > now()-interval '1 hour' GROUP BY 1` — много `PENDING` = исполнитель не забирает команды (`kubectl -n $NS get pods -l app.kubernetes.io/component=celery-exit`).
- **Восстановление.** Устранить причину; разобрать очередь: оплаченные/льготные → `POST /sessions/<id>/open-exit` (`reason`), уехавшие по кнопке → `POST /sessions/<id>/close`; массово — только скриптом по списку id с одним `reason` и отдельной записью в инциденте (каждое действие всё равно аудируется). Для `barrier command PENDING` после восстановления консьюмера ничего делать не нужно — сессии закроются сами (проверка ниже).
- **Проверка.** Очередь уменьшается без ручных действий (для лага) или после разбора; `exit_end_to_end_seconds` p95 < 2 с; `ExitSuccessRatioLow` погас.
- **Откат.** Массовое ошибочное закрытие: сессии не «переоткрываются» (I-6/аудит) — заново создать сессию нельзя, ущерб фиксируется по `audit_log` и компенсируется клиенту; поэтому массовые действия — только после подтверждения причины.
- **После инцидента.** Число ручных действий, длительность, отчёт по причинам; если `PENDING`-дедлайн 30 с срабатывает под пиком — пересмотреть ресурсы consumer/celery (INFRA §2) до расширения дедлайна.

## Часть B. Остальные алерты

<a id="api-down"></a>
## API недоступен (`ApiDown`)

- **Обнаружение.** Алерт `ApiDown` (нет ни одной реплики `up{job="parking-api"}==1` 1 мин); дашборд *SLO* → «Доступность API»; лог-запрос `{app="parking",component="api"} |= "startup_failed"`.
- **Влияние.** Камеры получают ошибки и копят события в локальном буфере (вендорские камеры — до нескольких тысяч); операторы, кассы и фронтенд не работают; шлагбаумы открываются только вручную.
- **Немедленное.** `kubectl -n parking get pods -l app=parking-api` → если поды в `CrashLoopBackOff`, смотреть `kubectl logs --previous`; если поды `Running`, но `up=0` — проблема сети/Ingress: `kubectl get ingress`, `curl -sS https://api…/health`.
- **Диагностика.** `/health` → `{"status":"ok"}`; `kubectl describe pod` (OOMKilled?); лог `config_invalid`/`migration_pending` — API отказывается стартовать при неприменённых миграциях (`alembic current` ≠ head).
- **Восстановление.** Конфиг/секреты: исправить values → `helm upgrade`. Миграции: `alembic upgrade head` из job. OOM: увеличить лимит, перезапустить. После каждого шага — `curl /health` и `up==1` в Prometheus.
- **Проверка.** Алерт погас; `entry_total` растёт; камеры отдали буфер (`camera_event_total` всплеск, `late=true` в логах).
- **Откат.** `helm rollback parking <предыдущая ревизия>`; при откате версии с миграцией — только вперёд-совместимые миграции (I-8), даунгрейд БД не делать.
- **После инцидента.** Собрать `kubectl logs --previous`, время недоступности (error budget), число событий, пришедших с `late=true`; постмортем в течение 48 ч.

<a id="lot-without-cameras"></a>
## Площадка полностью без камер (`LotWithoutCameras`)

- **Обнаружение.** `LotWithoutCameras` (ни одной ONLINE 5 мин при наличии камер); дашборд *Operations* → «Камеры по статусу»; `GET /api/v1/cameras?lot_id=…`.
- **Влияние.** На площадке нет автоматического въезда/выезда; сессии не создаются и не закрываются; выручка площадки останавливается.
- **Немедленное.** Связаться с площадкой: питание/сеть/коммутатор (все камеры разом = инфраструктура площадки, не камеры). Оператор открывает шлагбаумы вручную (`POST /api/v1/barriers/{id}/open`), фиксирует въезды в ручном режиме.
- **Диагностика.** `camera_status_by_lot{lot="…"}`; последний heartbeat камер (`GET /api/v1/cameras/{id}` → `last_seen_at`); `camera_auth_failed_total` (ключи отозваны?); `POST /api/v1/camera/heartbeat` с ключом камеры вручную с площадки (`curl`).
- **Восстановление.** Сеть/питание → камеры сами шлют буфер (`late=true`) → сессии создаются задним числом (без открытия шлагбаума — `LATE_ACCEPTED`).
- **Проверка.** Все камеры `ONLINE`; `entry_total{lot}` растёт; расхождений между ручным журналом и сессиями нет.
- **Откат.** Не применимо.
- **После инцидента.** Список сессий, закрытых вручную; корректировка тарифа для «ручных» въездов при необходимости.

<a id="backup-missing"></a>
## Бэкап не выполнен (`BackupMissing`)

- **Обнаружение.** `BackupMissing` (метрика `parking_backup_last_success_timestamp_seconds` старше 26 ч или отсутствует); CronJob `parking-backup` в `kubectl get jobs`.
- **Влияние.** RPO нарушен: при потере primary теряются сессии и платежи за сутки.
- **Немедленное.** `kubectl -n parking logs job/parking-backup-<дата>`; запустить вручную `kubectl create job --from=cronjob/parking-backup backup-manual`.
- **Диагностика.** Место в бакете (S3 quota), креды (`403`), долгий `pg_dump`/`pgBackRest` при lock; pushgateway доступен?
- **Восстановление.** Устранить причину → ручной запуск → убедиться, что `parking_backup_last_success_timestamp_seconds` обновилась; раз в квартал — тестовое восстановление (фаза 9).
- **Проверка.** Алерт погас; размер бэкапа сопоставим с предыдущими.
- **Откат.** Не применимо.
- **После инцидента.** Период без бэкапа; проверка, что WAL-архивация (PITR) работала всё это время.

<a id="backup-restore-verify"></a>
## Проверка восстановления бэкапа не проходит (`BackupRestoreVerifiedStale`)

- **Обнаружение.** `BackupRestoreVerifiedStale` (метрика `backup_restore_verified_timestamp` старше 8 дней или отсутствует); журнал `backup-verify.service` на verify-ВМ (`journalctl -u backup-verify`).
- **Влияние.** Бэкап не считается существующим: неизвестно, восстановится ли он, RTO 15 мин не гарантирован (INFRA §4).
- **Немедленное.** Запустить проверку вручную: `systemctl start backup-verify` на verify-ВМ; смотреть шаг, на котором падает (restore / pg_amcheck / контрольные суммы).
- **Диагностика.** `pgbackrest info --stanza=parking` (есть ли полный бэкап и непрерывный WAL: `wal archive min/max`); `pgbackrest check`; место на verify-ВМ; расхождение контрольных сумм — сверить `parking_session`/`payment` за вчера на primary и в восстановленном инстансе (порт 5499).
- **Восстановление.** Повреждённый набор → внеплановый `pgbackrest --type=full backup`, затем повторная проверка; разрыв WAL — проверить `archive_command` на primary (`pg_stat_archiver.last_failed_wal`).
- **Проверка.** Метрика обновилась, `backup_restore_duration_seconds` в пределах SLO (≤ 900 с).
- **Откат.** Не применимо.
- **После инцидента.** Учение восстановления (INFRA §4) повторить, RTO записать.

<a id="certificate-expiring"></a>
## Сертификат истекает (`CertificateExpiringSoon`)

- **Обнаружение.** `CertificateExpiringSoon` (blackbox: < 7 дней); `kubectl get certificate -n parking` (cert-manager).
- **Влияние.** После истечения камеры и фронтенд получают TLS-ошибки — полная остановка.
- **Немедленное.** `kubectl describe certificate` → причина (ACME challenge не проходит, DNS, rate limit Let's Encrypt).
- **Диагностика.** `kubectl get order,challenge -n parking`; логи cert-manager; доступность `/.well-known/acme-challenge/` снаружи.
- **Восстановление.** Исправить DNS/Ingress → `kubectl delete certificaterequest …` для повторной выдачи; в крайнем случае — вручную выпущенный сертификат в Secret.
- **Проверка.** `probe_ssl_earliest_cert_expiry - time() > 30d`; `openssl s_client -connect api…:443 | openssl x509 -noout -dates`.
- **Откат.** Вернуть предыдущий Secret с сертификатом, если новый не принимается камерами (цепочка).
- **После инцидента.** Почему автопродление не сработало за 30 дней до истечения.

<a id="exit-success-low"></a>
## Доля успешных открытий шлагбаума ниже 98 % (`ExitSuccessRatioLow`)

- **Обнаружение.** `ExitSuccessRatioLow` (10 мин); дашборд *Operations* → «Команды шлагбаумов», *SLO* → «Открытие шлагбаума».
- **Влияние.** Водители стоят перед закрытым шлагбаумом; операторы открывают вручную; очередь и жалобы.
- **Немедленное.** `sum by (lot,result)(increase(barrier_command_total[10m]))` — одна площадка (контроллер/сеть) или все (воркер barrier/Redis)? Одна — см. [Отказ шлагбаума](#barrier-failure); все — проверить воркер `barrier` (`kubectl get pods -l component=worker`) и `DlqGrowing`.
- **Диагностика.** Лог `barrier_command_completed` c `result`, `latency_ms`; `barrier_breaker_state` (OPEN → драйвер недоступен); `barrier_circuit_open`; для Modbus — `nc -zv <ip> 502` с воркера.
- **Восстановление.** Перезапуск контроллера/коммутатора площадки; при ошибке драйвера после релиза — `helm rollback`; breaker закрывается сам (half-open через `retry_after`).
- **Проверка.** Доля OK > 99.5 % за 10 мин; `MANUAL_REVIEW`-саги закрыты (`POST /api/v1/barriers/{id}/open` с `saga_id` закрывает сессию).
- **Откат.** См. восстановление.
- **После инцидента.** Число ручных открытий, длительность, SLO-бюджет.

<a id="camera-events-errors"></a>
## POST /camera/events: 5xx выше 0.5 % (`CameraEvents5xxHigh`)

- **Обнаружение.** `CameraEvents5xxHigh`; дашборд *SLO* → «Camera events: ошибки»; лог `{component="api"} | json | path="/api/v1/camera/events" | status>=500`.
- **Влияние.** Камеры ретраят и буферизуют — часть въездов/выездов с задержкой; при 100 % 5xx — как `ApiDown` для площадок.
- **Немедленное.** Sentry → группировка по исключению; если `anpr_db_unavailable` — это `PostgresPrimaryDown`; если `lock_timeout` — шторм дубликатов (камера шлёт один кадр многократно).
- **Диагностика.** По `correlation_id` из ответа камеры (`X-Correlation-Id`) — трасса; `camera_event_rejected_total{reason}`; латентность БД (`pg_stat_statements`).
- **Восстановление.** Баг — hotfix/rollback; перегрузка — масштабировать API (HPA), проверить пул соединений (`Pool.CRITICAL`).
- **Проверка.** Доля 5xx < 0.1 % 15 мин; `late=true` события отработали.
- **Откат.** `helm rollback`.
- **После инцидента.** Error budget, тест на воспроизведение.

<a id="api-error-budget"></a>
## Расход error budget API (`ApiErrorBudgetBurnFast` / `ApiErrorBudgetBurnSlow`)

- **Обнаружение.** Multi-window burn-rate алерты; дашборд *SLO* → «Error budget (30 дней)».
- **Влияние.** При сохранении темпа месячный SLO 99.9 % будет нарушен (fast — за ~2 дня, slow — за ~5).
- **Немедленное.** Fast: относиться как к инциденту — найти источник 5xx (`sum by (route)(rate(http_request_duration_seconds_count{status_class="5xx"}[5m]))`). Slow: завести задачу, заморозить рискованные релизы до восстановления бюджета.
- **Диагностика.** Sentry по release; сопоставить с деплоями (аннотации Grafana); зависимости (`dependency_up`).
- **Восстановление.** Устранить источник; при регрессии релиза — rollback.
- **Проверка.** Burn rate < 1 в обоих окнах.
- **Откат.** `helm rollback`.
- **После инцидента.** Отчёт по бюджету за месяц, решение о release freeze.

<a id="camera-clock-skew"></a>
## Часы камер сдвинуты (`CameraClockSkew`)

- **Обнаружение.** `CameraClockSkew` (≥3 событий с |skew| > 60 с за 15 мин на площадке); дашборд *Operations* → «Сдвиг часов камер»; лог `anpr_event_accepted` с полем `clock_skew_seconds` (в `anpr_event` — колонка).
- **Влияние.** Событие принято и шлагбаум открыт, но время события искажено: тариф считает длительность по времени камеры — недоплата/переплата, споры по чекам; при сдвиге > окна replay (24 ч) события начнут отклоняться как `REPLAY`.
- **Немедленное.** Определить камеру: `SELECT camera_id, avg(clock_skew_seconds) FROM anpr_event WHERE lot_id='…' AND received_at > now()-interval '1 hour' GROUP BY 1`. Проверить NTP на камере (веб-интерфейс вендора) и на площадочном шлюзе.
- **Диагностика.** Сдвиг постоянный (часы стоят/неверная TZ) или плывущий (нет NTP)? Все камеры площадки (шлюз/NTP-сервер) или одна?
- **Восстановление.** Включить NTP/исправить часовой пояс камеры; сессии за период — пересчитать тариф при спорах (расчёт детерминирован, I-7: `POST /api/v1/tariffs/quote` с фактическим временем).
- **Проверка.** Новые события с `clock_skew_seconds < 60`; алерт погас через 15 мин.
- **Откат.** Не применимо.
- **После инцидента.** Добавить NTP-мониторинг на шлюзах площадок; список спорных сессий за период сдвига.

<a id="reconciliation-backlog"></a>
## Очередь сверки растёт (`ReconciliationBacklog`)

- **Обнаружение.** `ReconciliationBacklog` (>10 открытых расхождений 1 ч); дашборд *Business* → «Сверка»; `GET /api/v1/reconciliation-discrepancies?status=OPEN`.
- **Влияние.** Отчётность расходится с провайдерами; спорные выезды; риск неучтённых денег.
- **Немедленное.** Разобрать по `kind`: `MISSING_LOCAL` (платёж у провайдера, у нас нет — потерянный вебхук: replay через `POST /api/v1/reconciliations`), `AMOUNT_MISMATCH`, `MISSING_REMOTE` (наш PAID без провайдера — ручная проверка), `DUPLICATE_REMOTE` (двойной платёж → возврат), `STATUS_MISMATCH`.
- **Диагностика.** По `payment_id` → `payment_attempt` (что вернул провайдер), `webhook_inbox` (пришёл ли вебхук), логи по `correlation_id`.
- **Восстановление.** `POST /api/v1/reconciliation-discrepancies/{id}/resolve` с решением (MANAGER+); возвраты через `POST /api/v1/payments/{id}/refunds`.
- **Проверка.** `reconciliation_discrepancies_open` < 10; следующий ежедневный прогон без новых.
- **Откат.** Решения по расхождениям фиксируются в audit — отмена только новым решением.
- **После инцидента.** Если причина системная (вебхуки теряются) — см. [Платежи не проходят](#payments-failing).

<a id="stream-consumer-lag"></a>
## Лаг консьюмера стримов (`StreamConsumerLag`)

- **Обнаружение.** `StreamConsumerLag` (>1000 не доставленных + без ACK 5 мин; `stream_pending_total` = `XINFO GROUPS lag + pending`); дашборд *Pipeline* → «Стримы: отставание по шардам».
- **Влияние.** Задержка открытия шлагбаума после оплаты и обновлений live-панели растёт пропорционально лагу.
- **Немедленное.** `kubectl get pods -l component=consumer`; `redis-cli XPENDING events:N core` — один шард (лизинг завис у мёртвого консьюмера — освободится через `lease_ms`) или все (мало реплик).
- **Диагностика.** `consumer_handled duration_ms` в логах (медленный обработчик — БД?); `consumer_processed_total{result="error"}` и `consumer_retry_scheduled_total` (повторы ждут в `events:N:delayed`, шард они не блокируют — но каждый повтор снова стоит обработчика); `redis_stream_lag` = не доставленные + не подтверждённые (`XINFO GROUPS`). Внутри процесса шарды идут параллельно: один медленный обработчик тормозит только свой шард.
- **Восстановление.** Масштабировать consumer (до 8 реплик = число шардов); устранить медленный обработчик; зависший лизинг — `redis-cli DEL lease:core:events:N`.
- **Проверка.** `stream_pending_total < 100` на всех шардах.
- **Откат.** Не применимо.
- **После инцидента.** Пиковая нагрузка vs. число реплик — обновить HPA.

<a id="disk-usage"></a>
## Диск заполнен более 80 % (`DiskUsageHigh`)

- **Обнаружение.** `DiskUsageHigh` (15 мин); node_exporter.
- **Влияние.** При 100 % PostgreSQL перестанет принимать записи (PANIC на WAL) — остановка платежей и сессий.
- **Немедленное.** Что растёт: `du -sh /var/lib/postgresql/*` (WAL — архивация не работает? `pg_stat_archiver`), логи (`journalctl --disk-usage`), Redis AOF, партиции `anpr_event`/`audit_log` без ретеншена.
- **Диагностика.** `SELECT pg_size_pretty(pg_database_size('parking'))`; размер партиций по месяцам; `ls /var/lib/postgresql/.../pg_wal | wc -l`.
- **Восстановление.** Починить архивацию WAL; отсоединить/удалить партиции старше ретеншена (фаза 2: `detach partition` → `pg_dump` в архив → `drop`); расширить PVC.
- **Проверка.** Использование < 70 %.
- **Откат.** Отсоединённые партиции возвращаются `ATTACH PARTITION` из архива.
- **После инцидента.** Прогноз роста, алерт на прогноз (`predict_linear`).

<a id="camera-events-latency"></a>
## POST /camera/events: p95 выше 250 мс (`CameraEventsLatencyHigh`)

- **Обнаружение.** `CameraEventsLatencyHigh`; дашборд *SLO* → «Camera events: латентность».
- **Влияние.** Шлагбаум открывается позже; при росте — таймауты камер и дубликаты (второй запрос получает кэшированный ответ).
- **Немедленное.** Трасса медленного запроса (Tempo: `name="entry.process" duration>250ms`) — где время: `access.resolve` (БД/чёрный список), `tariff.calculate`, advisory lock (`lock_timeout` — дубликаты), Redis (`camera_auth_seconds`).
- **Диагностика.** `pg_stat_statements` top по `mean_exec_time`; `camera_auth_seconds` p95 (Redis); нагрузка API (`http_request_duration_seconds_count` rps) vs. реплики.
- **Восстановление.** Индексы/план запроса; масштабировать API; при деградации Redis — см. `RedisDown`.
- **Проверка.** p95 < 150 мс 15 мин.
- **Откат.** `helm rollback` при регрессии после релиза.
- **После инцидента.** Нагрузочный профиль, обновление HPA.

<a id="exit-e2e-slow"></a>
## EXIT end-to-end p95 выше 4 с (`ExitEndToEndSlow`)

- **Обнаружение.** `ExitEndToEndSlow` (`exit_end_to_end_seconds{paid="no"}`); дашборд *SLO* → «EXIT end-to-end».
- **Влияние.** Очереди на выезде даже у машин без оплаты (абонементы/льготы).
- **Немедленное.** Разложить по этапам трассы EXIT (экспортируется 100 %): `exit.start` → outbox (`OutboxLag`?) → `consume …` (`StreamConsumerLag`?) → `exit.saga.step` → `barrier.open` (медленный контроллер: `latency_ms` в `barrier_command_completed`).
- **Диагностика.** Trace по `session_id` в Tempo; `outbox_oldest_pending_age_seconds`; `stream_pending_total`; `barrier_command_completed latency_ms`.
- **Восстановление.** По найденному этапу: publisher/consumer реплики, контроллер шлагбаума, БД.
- **Проверка.** p95 < 2 с 15 мин.
- **Откат.** См. этап.
- **После инцидента.** Разбивка по этапам, SLO-бюджет.

<a id="payment-confirm-slow"></a>
## Подтверждение платежа p95 выше 10 с (`PaymentConfirmSlow`)

- **Обнаружение.** `PaymentConfirmSlow`; дашборд *SLO* → «Подтверждение платежа», *Business* → «Провайдеры».
- **Влияние.** Водители ждут у шлагбаума после оплаты; саги уходят в `PAYMENT_PENDING_VERIFICATION` (таймаут `provider_timeout_seconds`).
- **Немедленное.** По провайдеру: `gateway_seconds` p95, `gateway_calls_total{result="timeout"}`; статус-страница провайдера; вебхуки приходят (`webhook_total{result="ok"}`)?
- **Диагностика.** Наша сторона: пул соединений, `webhook_inbox` (дубликаты), задержка job `verify_pending` (Celery beat жив?).
- **Восстановление.** Провайдер — ждать/эскалировать; свои задержки — масштабировать API/воркер payments.
- **Проверка.** p95 < 5 с; `PAYMENT_PENDING_VERIFICATION`-саг нет старше 5 мин.
- **Откат.** Не применимо.
- **После инцидента.** SLA провайдера, число саг, закрытых job'ом verify.

## Часть B2. Новые поверхности v2 (этап 8)

<a id="webhooks-stuck"></a>
## Вебхуки клиента не доставляются (`WebhookDeliveriesDead`)

- **Обнаружение.** `WebhookDeliveriesDead` (доставки исчерпали повторы 1/5/30 мин за час); метрика `webhook_delivery_total{result="dead"|"retry"}`; лог `webhook_delivery_failed` (поля `delivery_id`, `status`, `error`), `webhook_endpoint_auto_disabled`; у клиента в «Интеграции → Вебхуки» — «ошибки: N подряд» или «выключен автоматически: 3 суток без ответа».
- **Влияние.** Внешняя система клиента (1С/ERP) не получает события; проезд машин, оплаты и шлагбаумы не затронуты — вебхуки отправляет отдельный воркер `webhooks` после коммита события.
- **Немедленное.** Узнать масштаб: один адрес или все? `psql -c "SELECT e.tenant_id, e.url, e.consecutive_failures, e.failing_since, e.active, e.disabled_reason FROM webhook_endpoint e WHERE e.consecutive_failures > 0 ORDER BY e.consecutive_failures DESC LIMIT 20"`. Если «все и сразу» — проверить воркер: `kubectl -n $NS get deploy parking-webhooks` (READY 1/1), логи `{app="parking", component="webhooks"}`, egress 443 из NetworkPolicy `parking-workers`.
- **Диагностика.** По `last_error` доставки: `webhook address is not public` / `does not resolve` — SSRF-проверка (адрес клиента указывает во внутреннюю сеть или DNS не резолвится; так и должно быть); `HTTP 4xx/5xx` — отвечает сервер клиента; `ConnectTimeout` — недоступен из интернета. `psql -c "SELECT status, last_status, left(last_error, 80), count(*) FROM webhook_delivery WHERE created_at > now() - interval '1 day' GROUP BY 1,2,3 ORDER BY 4 DESC"`.
- **Восстановление.** Проблема у клиента — сообщить ему (контакт в «Клиенты платформы»): исправить адрес/сертификат, затем клиент сам нажимает «Отправить тест» и «Включить», а недоставленные — «Отправить снова» в журнале доставок (`POST /api/v1/webhook-deliveries/{id}/redeliver`, с причиной). Массово за клиента: `psql -c "UPDATE webhook_delivery SET status = 'RETRY', next_attempt_at = now() WHERE status = 'DEAD' AND endpoint_id = '<id>' AND created_at > now() - interval '3 days'"` (только после подтверждения клиента; события старше 3 суток клиенту обычно не нужны). Воркер лежал — перезапустить `kubectl -n $NS rollout restart deploy/parking-webhooks`: доставки с просроченной арендой (2 мин) подберёт заново, дубли отсекает получатель по `X-Parking-Delivery`.
- **Проверка.** `webhook_delivery_total{result="delivered"}` растёт; у адреса `consecutive_failures = 0`; алерт гаснет через час.
- **Откат.** Не применимо (повторная отправка идемпотентна для получателя по `X-Parking-Delivery`).
- **После инцидента.** Если адрес выключался автоматически — сообщить клиенту, что события за период выключения не отправлялись (их видно в «Сессиях»/отчётах; при необходимости — выгрузка через API-ключ).

<a id="event-journal-dropping"></a>
## Журнал событий камер теряет записи (`EventJournalDropping`)

- **Обнаружение.** `EventJournalDropping`; метрики `event_journal_dropped_total`, `event_journal_written_total`; лог `journal_write_failed` (`error_type`, `lost`).
- **Влияние.** Только диагностика клиента: в «Интеграции → Журнал событий» не видно части запросов камер. Приём событий, сессии и шлагбаумы не затронуты — журнал пишется буфером процесса API вне транзакции события (A-91).
- **Немедленное.** `journal_write_failed` с `OperationalError/ConnectionError` — недоступен пул bulk (PgBouncer): см. «PostgreSQL недоступен» (часть A). Без ошибок записи, но растёт `dropped` — буфер 10 000 строк переполнен: поток событий выше, чем успевает запись (сброс раз в 1 с или по 200 строк).
- **Диагностика.** Поток событий: `sum(rate(camera_event_total[5m]))`; время записи — `pg_stat_statements` по `INSERT INTO event_journal`; партиции на сегодня есть? `psql -c "SELECT tablename FROM pg_tables WHERE tablename LIKE 'event_journal_p%' ORDER BY 1 DESC LIMIT 3"` (создаёт `retention.sql` ежедневно на 3 дня вперёд).
- **Восстановление.** Нет партиции — `psql -f backend/scripts/retention.sql` (создаст и почистит); перегрузка — масштабировать API репликами (у каждого пода свой буфер) или поднять `pool_size` bulk в PgBouncer.
- **Проверка.** `increase(event_journal_dropped_total[15m]) == 0`, новые строки в журнале с текущим временем.
- **Откат.** Не применимо.
- **После инцидента.** Потерянные строки не восстанавливаются (события в `anpr_event` целы); сообщить клиенту период, если он разбирал интеграцию.

<a id="sms-gateway-down"></a>
## SMS-шлюз недоступен: коды регистрации и приглашения

- **Обнаружение.** Регистрация отвечает 503 `SERVICE_UNAVAILABLE` «verification code could not be sent»; лог `signup_code_send_failed` (`channel`, `error_type`); жалобы «не пришёл код» — заявки без `verify`: `$REDIS --scan --pattern 'signup:*' | wc -l`.
- **Влияние.** Новые клиенты не могут зарегистрироваться сами; приглашения сотрудников не зависят от SMS — это ссылка, которую администратор отправляет любым мессенджером (A-84); работающие клиенты не затронуты.
- **Немедленное.** Сейчас реального канала нет (A-76): регистрация включена только на стенде, код показывается на экране (`PARKING_SIGNUP_DEV_CODES=true`). В production до подключения шлюза — регистрация через владельца платформы: он заводит клиента и отправляет администратору ссылку-приглашение.
- **Диагностика.** После подключения шлюза: статус-страница провайдера, ответы API шлюза в логах, баланс.
- **Восстановление.** Временно — регистрация через владельца платформы (выше); не включать показ кода на экране в production — это снимает проверку владения телефоном.
- **Проверка.** Тестовая регистрация с реального номера доходит до `verify`.
- **Откат.** Не применимо.
- **После инцидента.** Заявки, не завершившие регистрацию, не сохраняются (заявка живёт в Redis 15 минут) — клиенту нужно пройти регистрацию заново; число неудачных попыток видно по `signup_code_send_failed` в логах.

<a id="api-key-abuse"></a>
## Перебор API-ключей (429 `RATE_LIMITED` на внешних интеграциях)

- **Обнаружение.** Клиент сообщает, что его 1С получает 429; лог `domain_error` с `code=RATE_LIMITED` на путях с `Authorization: Bearer pk_…`.
- **Влияние.** С одного адреса после 20 неверных ключей за 5 минут все запросы с API-ключами получают 429 на 5 минут (A-97); остальные адреса и вход сотрудников не затронуты.
- **Немедленное.** Это или атака, или у клиента в 1С старый/отозванный ключ (частый случай после «Отозвать»). Проверить ключи клиента в «Интеграции → API-ключи»: «отозван»/«истёк» у используемого?
- **Диагностика.** `redis-cli GET apikey:fail:<ip>` — счётчик и `TTL`; в Loki — какие префиксы `pk_…` приходят.
- **Восстановление.** Клиенту — выпустить новый ключ и заменить в 1С; атака — ничего не делать (блок снимается сам), при длительной — заблокировать адрес на ingress.
- **Проверка.** Запросы с верным ключом получают 200.
- **Откат.** `redis-cli DEL apikey:fail:<ip>` — снять блок досрочно.
- **После инцидента.** При атаке — отозвать ключи, которые могли утечь.

## Часть C. Прогоны на стенде (фактические логи)

Стенд — staging-эквивалент на рабочей станции (кластера Kubernetes нет, A-62): приложение `backend/scripts/e2e_stack.py`
(API :8010 два воркера, publisher, consumer, replayer, фейковый контроллер шлагбаума), Redis :6390, PostgreSQL — лаборатория
Patroni ×3 через PgBouncer :6432 (сценарий 1) или локальный pgserver (сценарии 3, 7). Команды и вывод — без правок,
кроме токенов и сокращения путей (`$B` = `backend/`). Скрипты прогонов: `docs/backend/runbook-drills/`.

### Лог 1 — сценарий 1 «PostgreSQL primary недоступен» (узел primary убит → автоматический failover)

```text
### Runbook 1 «PostgreSQL primary недоступен» — Patroni ×3 (лаборатория) + приложение через PgBouncer :6432 → HAProxy-аналог :5500 — 2026-09-20T10:57:01Z

$ uvx --from patroni[raft]==3.3.7 --with psycopg2-binary patronictl -c $B/.pgha/pg1/patroni.yml list
  + Cluster: parking-lab (7687569538443000699) --------+----+-----------+--------------+
  | Member | Host           | Role         | State     | TL | Lag in MB | Tags         |
  +--------+----------------+--------------+-----------+----+-----------+--------------+
  | pg1    | 127.0.0.1:5551 | Sync Standby | streaming |  1 |         0 |              |
  | pg2    | 127.0.0.1:5552 | Leader       | running   |  1 |           |              |
  | pg3    | 127.0.0.1:5553 | Replica      | streaming |  1 |         0 | nosync: true |
  +--------+----------------+--------------+-----------+----+-----------+--------------+

$ curl -s http://127.0.0.1:8010/health/ready
  {"status":"ok","ready":true,"checks":{"postgresql":"ok","redis":"ok"}}

$ camera_event 01G301AA
  200 ENTRY_CONFIRMED queued=False

--- отказ: узел primary=pg2 потерян — kill -9 агента Patroni (pids 101210 101224 101522 101536 ) и postmaster (101275) 10:57:08

$ kill -9 101210 101224 101522 101536  101275

$ curl -s -w ' %{http_code}' http://127.0.0.1:8010/health/ready
  {"status":"degraded","ready":true,"checks":{"postgresql":"error: DBAPIError","redis":"ok"}} 200

$ camera_event 01G302AA
  202 QUEUED queued=True

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN anpr:offline
  1

$ metric 'offline_mode_active'
  offline_mode_active 0.0
  (ждём автоматический failover: ttl 20 с / loop_wait 5 с в лаборатории; production ttl 30 / loop_wait 10)

$ uvx --from patroni[raft]==3.3.7 --with psycopg2-binary patronictl -c $B/.pgha/pg1/patroni.yml list
  + Cluster: parking-lab (7687569538443000699) -+----+-----------+--------------+
  | Member | Host           | Role    | State   | TL | Lag in MB | Tags         |
  +--------+----------------+---------+---------+----+-----------+--------------+
  | pg1    | 127.0.0.1:5551 | Leader  | running |  2 |           |              |
  | pg3    | 127.0.0.1:5553 | Replica | running |  1 |         0 | nosync: true |
  +--------+----------------+---------+---------+----+-----------+--------------+

$ curl -s -w ' %{http_code}' http://127.0.0.1:8010/health/ready
  {"status":"ok","ready":true,"checks":{"postgresql":"ok","redis":"ok"}} 200

$ camera_event 01G303AA
  200 ENTRY_CONFIRMED queued=False

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN anpr:offline
  0

$ $B/.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/psql 'postgresql://postgres@127.0.0.1:6432/parking_e2e' -Atc "SELECT count(*) FILTER (WHERE queued_offline) AS replayed, count(*) AS tota
  1|3

$ curl -s http://127.0.0.1:9466/metrics | grep -E '^offline_replayed_total'
  offline_replayed_total{result="ok"} 1.0

--- возврат старого primary репликой 10:57:42

$ cd /home/deep/projects/anpr-parking/backend && uv run python ../deploy/postgres/lab/pgha_lab.py status >/dev/null; uv run python -c "import sys; sys.path.insert(0,'../deploy/postgres/lab'); import p
  pg1  leader         running    lag=- tl=2
  pg2  replica        streaming  lag=0 tl=2
  pg3  replica        streaming  lag=0 tl=2
  proxy: up, pgbouncer: up

$ uvx --from patroni[raft]==3.3.7 --with psycopg2-binary patronictl -c $B/.pgha/pg1/patroni.yml list
  + Cluster: parking-lab (7687569538443000699) ---+----+-----------+--------------+
  | Member | Host           | Role    | State     | TL | Lag in MB | Tags         |
  +--------+----------------+---------+-----------+----+-----------+--------------+
  | pg1    | 127.0.0.1:5551 | Leader  | running   |  2 |           |              |
  | pg2    | 127.0.0.1:5552 | Replica | streaming |  2 |         0 |              |
  | pg3    | 127.0.0.1:5553 | Replica | streaming |  2 |         0 | nosync: true |
  +--------+----------------+---------+-----------+----+-----------+--------------+
--- завершено: failover автоматический, потерь принятых событий нет (202 → replay), старый primary — реплика 10:57:54
```

Наблюдение: `/health/ready` остаётся 200 `degraded` (под не снимается с трафика, A-60), камера получает `202 QUEUED`,
после failover (≈ 20 с в лаборатории) — `200 ENTRY_CONFIRMED`, буфер `anpr:offline` воспроизведён (`offline_replayed_total{result="ok"}`),
старый primary вернулся репликой на timeline 2. В отдельном прогоне (лог `runbook-drill-pg-restart.log`) убийство только
postmaster'а без агента приводит не к failover, а к перезапуску PostgreSQL Patroni на том же узле за ≈ 10 с.

### Лог 2 — сценарий 3 «Redis недоступен»

```text
### Runbook 3 «Redis недоступен» — стенд e2e_stack (API :8010, Redis :6390) — 2026-09-20T10:45:03Z

$ curl -s http://127.0.0.1:8010/health/ready
  {"status":"ok","ready":true,"checks":{"postgresql":"ok","redis":"ok"}}

$ camera_event 01R101AA
  200 ENTRY_CONFIRMED queued=False

$ $B/.redis/bin/redis-cli -p 6390 -n 2 PING
  PONG

--- отказ: остановка Redis 10:45:03

$ $B/.redis/bin/redis-cli -p 6390 shutdown nosave

$ $B/.redis/bin/redis-cli -p 6390 -n 2 PING
  Could not connect to Redis at 127.0.0.1:6390: Connection refused

$ curl -s -w ' %{http_code}' http://127.0.0.1:8010/health/ready
  {"status":"degraded","ready":false,"checks":{"postgresql":"ok","redis":"error: ConnectionError"}} 503

$ camera_event 01R102AA
  200 ENTRY_CONFIRMED queued=False

$ metric 'dependency_up'
  dependency_up{dependency="postgres"} 1.0
  dependency_up{dependency="redis"} 0.0

$ metric 'redis_up\|camera_event_total' 6

--- восстановление: запуск Redis 10:45:10

$ uv run python $B/scripts/redis_dev.py start 6390
  redis://127.0.0.1:6390/0

$ curl -s -w ' %{http_code}' http://127.0.0.1:8010/health/ready
  {"status":"ok","ready":true,"checks":{"postgresql":"ok","redis":"ok"}} 200

$ camera_event 01R103AA
  200 ENTRY_CONFIRMED queued=False

$ metric 'dependency_up'
  dependency_up{dependency="postgres"} 1.0
  dependency_up{dependency="redis"} 1.0

$ grep -c worker_loop_crashed $B/.e2e/consumer.log $B/.e2e/publisher.log $B/.e2e/replayer.log
  $B/.e2e/consumer.log:5
  $B/.e2e/publisher.log:0
  $B/.e2e/replayer.log:3

$ curl -s http://127.0.0.1:9465/metrics | grep -E '^worker_loop_restarts_total|^consumer_processed_total.*session.created' | head -3
  consumer_processed_total{event_type="parking.session.created.v1",result="ok"} 2.0
  worker_loop_restarts_total{worker="run_consumer"} 5.0

$ pgrep -fc 'main_consumer|main_publisher|main_offline_replayer'
  4
--- восстановлено без ручных действий: воркеры пережили отказ (супервизор перезапустил циклы) 10:45:31
```

Наблюдение: ingest продолжает принимать события (`200 ENTRY_CONFIRMED`) при недоступном Redis, readiness честно отдаёт
503; после возврата Redis воркеры продолжили работу без вмешательства — циклы перезапущены супервизором
(`worker_loop_restarts_total{worker="run_consumer"} 5`). **Первый прогон этого сценария выявил дефект**: consumer и
replayer завершались с `redis.exceptions.ConnectionError` и не возвращались — исправлено в фазе 10
(`workers/runtime.py::supervise`, `tests/workers/test_shutdown.py`).

### Лог 3 — сценарий 7 «Накопление DLQ» (после отказа PostgreSQL)

```text
### Runbook 7 «Накопление DLQ» (после отказа PostgreSQL) — стенд e2e_stack — 2026-09-20T10:46:57Z

$ camera_event 01D201AA
  200 ENTRY_CONFIRMED queued=False

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN events:dlq
  0

--- отказ: остановка PostgreSQL 10:46:57 (события камер уходят в оффлайн-буфер, обработчики consumer'а падают)

$ uv run python $B/scripts/pg_dev.py stop
  waiting for server to shut down.... done
  server stopped

$ camera_event 01D202AA
  202 QUEUED queued=True

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN anpr:offline
  1

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XADD events:1 '*' event_id fc7029a8-aae5-469b-9a5a-c270b7ddaf34 event_type parking.session.created.v1 tenant_id 00000000-0000-4000-8000-0000000000a1 aggregate_ty
  1789901218127-0
  (ждём 3 попытки обработчика: 1 с + 4 с + 16 с бэкоффа)

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN events:dlq
  1

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XREVRANGE events:dlq + - COUNT 1 | grep -A1 -E 'error|event_type' | head -4
  event_type
  parking.session.created.v1
  --
  error

$ metric 'dlq_length\|dlq_messages_total' 4

$ curl -s -H "Authorization: Bearer <token>" 'http://127.0.0.1:8010/api/v1/admin/dlq?stream=events&limit=5' | python3 -c "import json,sys;d=json.load(sys.stdin);[print(e['id'], e['fields'].get('event_

--- восстановление: запуск PostgreSQL 10:47:04

$ uv run python $B/scripts/pg_dev.py start
  postgresql://postgres:@/postgres?host=$B/.pgdata

$ curl -s -w ' %{http_code}' http://127.0.0.1:8010/health/ready
  {"status":"ok","ready":true,"checks":{"postgresql":"ok","redis":"ok"}} 200

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN anpr:offline
  0

$ metric 'offline_replayed_total' 4

--- replay DLQ: сначала dry-run, затем боевой (PLATFORM_ADMIN, reason обязателен)

$ curl -s -X POST -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' 'http://127.0.0.1:8010/api/v1/admin/dlq/replay' -d '{"stream":"events","dry_run":true,"reason":"runbook 7: посл
  dry_run True count 1

$ curl -s -X POST -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' 'http://127.0.0.1:8010/api/v1/admin/dlq/replay' -d '{"stream":"events","dry_run":false,"reason":"runbook 7: пос
  dry_run False count 1

$ $B/.redis/bin/redis-cli -p 6390 -n 2 XLEN events:dlq
  0

$ $B/.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/psql 'postgresql://postgres@/parking_e2e?host=$B/.pgdata' -Atc "SELECT count(*) FROM consumer_inbox WHERE event_id = 'fc7029a8-aae5-469b-
  1

$ $B/.venv/lib/python3.12/site-packages/pgserver/pginstall/bin/psql 'postgresql://postgres@/parking_e2e?host=$B/.pgdata' -Atc "SELECT action, created_at FROM audit_log WHERE action LIKE 'DLQ%' ORDER B
  DLQ_REPLAY|2026-09-20 15:47:29.587467+05
  DLQ_REPLAY_DRY_RUN|2026-09-20 15:47:29.487864+05
--- DLQ пуст, событие обработано один раз (inbox), replay записан в audit_log 10:47:33
```

Наблюдение: событие, полученное consumer'ом во время отказа БД, после трёх попыток (1/4/16 с) попало в `events:dlq`;
после восстановления БД буфер камер воспроизведён, replay DLQ (dry-run → боевой, PLATFORM_ADMIN, reason) обработал событие
ровно один раз (`consumer_inbox`), оба действия — в `audit_log`. Попутно исправлено: во время отказа БД `GET /api/v1/admin/dlq`
отвечал 500 — теперь любой эндпоинт при недоступной БД/Redis отдаёт `503 DEPENDENCY_UNAVAILABLE` c `Retry-After`
(`tests/api/test_health.py::test_dependency_outage_is_503_not_500`).
