# k6 — сценарий `operators` (k6-operators-1789882529)

SCALE=1/10 (длительность 85 с), RATE_SCALE=1/5 (интенсивности §6 поделены), uvicorn workers=2, стенд: pgserver; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 417.8 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 1068.9 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.000 % (5xx/сеть: 0) | < 0.1 % | ✅ |
| EXIT end-to-end p95 (без оплаты, n=45) | 0.72 s | < 2 s | ✅ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 121 (дублей 3, 202: 0) = в БД 118 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': 0.012, 'consumer': 0.019, 'publisher': 0.004, 'replayer': 0.0} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] | не растёт монотонно | ✅ |
| лаг репликации | n/a (нет реплики) s | < 5 s | ✅ |

Запросов всего 1590; heartbeat p95 81.2 ms; operator API p95 0.0 ms; WS-сообщений 6617; соединений БД max 25; Redis PING p95 4.03 ms; WAL за прогон 1.7 MB; RSS в конце (KB) {'api': 333840, 'consumer': 131556, 'publisher': 129312, 'replayer': 127596}; лаг стримов (pending) max 1, в конце 0; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/operators-k6.log`, полные данные — `operators.json`.
