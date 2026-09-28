# k6 — сценарий `baseline` (k6-baseline-1789939899, после оптимизации SQL-бюджета 2026-09-21; фаза 8 — `baseline.md`)

SCALE=1/30 (длительность 142 с), RATE_SCALE=1/5 (интенсивности §6 поделены), uvicorn workers=2, стенд: pgserver; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 688.2 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 1174.4 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.000 % (5xx/сеть: 0) | < 0.1 % | ✅ |
| EXIT end-to-end p95 (без оплаты, n=299) | 4.29 s | < 2 s | ❌ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 721 (дублей 33, 202: 0) = в БД 688 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': -0.075, 'consumer': 0.021, 'publisher': -0.321, 'replayer': -0.018} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 3, 0, 1, 0, 2, 1, 0, 0, 0, 3, 0, 2, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0] | не растёт монотонно | ✅ |
| лаг репликации | n/a (нет реплики) s | < 5 s | ✅ |

Запросов всего 3568; heartbeat p95 207.2 ms; operator API p95 0.0 ms; WS-сообщений 37425; соединений БД max 32; Redis PING p95 5.79 ms; WAL за прогон 9.4 MB; RSS в конце (KB) {'api': 304256, 'consumer': 131448, 'publisher': 87128, 'replayer': 125212}; лаг стримов (pending) max 29, в конце 0; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/baseline-sql-budget-k6.log`, полные данные — `baseline-sql-budget.json`.
