# k6 — сценарий `absolute` (k6-absolute-1789882392)

SCALE=1/5 (длительность 84 с), RATE_SCALE=1/10 (интенсивности §6 поделены), uvicorn workers=2, стенд: pgserver; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 20492.1 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 28592.2 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.194 % (5xx/сеть: 2) | < 0.1 % | ❌ |
| EXIT end-to-end p95 (без оплаты, n=99) | 115.5 s | < 2 s | ❌ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 1028 (дублей 52, 202: 0) = в БД 976 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': 0.084, 'consumer': 0.059, 'publisher': 0.017, 'replayer': 0.0} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 16, 4, 4, 0, 6, 4, 5, 0, 1, 0, 0, 0] | не растёт монотонно | ✅ |
| лаг репликации | n/a (нет реплики) s | < 5 s | ✅ |

Запросов всего 1655; heartbeat p95 1563.0 ms; operator API p95 0.0 ms; WS-сообщений 2972; соединений БД max 53; Redis PING p95 7.86 ms; WAL за прогон 10.6 MB; RSS в конце (KB) {'api': 357416, 'consumer': 136672, 'publisher': 130696, 'replayer': 127764}; лаг стримов (pending) max 819, в конце 819; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/absolute-k6.log`, полные данные — `absolute.json`.
