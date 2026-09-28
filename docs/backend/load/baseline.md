# k6 — сценарий `baseline` (k6-baseline-1789882076)

SCALE=1/30 (длительность 139 с), RATE_SCALE=1/5 (интенсивности §6 поделены), uvicorn workers=2, стенд: pgserver; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 1651.8 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 2535.3 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.000 % (5xx/сеть: 0) | < 0.1 % | ✅ |
| EXIT end-to-end p95 (без оплаты, n=313) | 9.62 s | < 2 s | ❌ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 721 (дублей 42, 202: 0) = в БД 679 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': 0.024, 'consumer': 0.04, 'publisher': 0.019, 'replayer': 0.0} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 0, 1, 0, 8, 2, 1, 6, 0, 3, 0, 0, 6, 1, 3, 1, 0, 0, 10, 2, 2, 0, 0, 0, 0] | не растёт монотонно | ✅ |
| лаг репликации | n/a (нет реплики) s | < 5 s | ✅ |

Запросов всего 3550; heartbeat p95 457.6 ms; operator API p95 0.0 ms; WS-сообщений 36254; соединений БД max 35; Redis PING p95 6.49 ms; WAL за прогон 9.8 MB; RSS в конце (KB) {'api': 337552, 'consumer': 134140, 'publisher': 131004, 'replayer': 127784}; лаг стримов (pending) max 52, в конце 0; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/baseline-k6.log`, полные данные — `baseline.json`.
