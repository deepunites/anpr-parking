# k6 — сценарий `soak` (k6-soak-1789884091)

SCALE=1/144 (длительность 325 с), RATE_SCALE=1/5 (интенсивности §6 поделены), uvicorn workers=2, стенд: pgserver; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 3022.5 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 4881.8 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.000 % (5xx/сеть: 0) | < 0.1 % | ✅ |
| EXIT end-to-end p95 (без оплаты, n=756) | 27.89 s | < 2 s | ❌ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 1791 (дублей 85, 202: 0) = в БД 1706 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': -0.027, 'consumer': 0.031, 'publisher': 0.001, 'replayer': -0.356} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 0, 0, 0, 1, 0, 0, 0, 0, 2, 1, 0, 2, 3, 4, 1, 2, 0, 0, 0, 4, 3, 1, 1, 1, 5, 0, 0, 0, 0, 10, 1, 1, 3, 6, 0, 2, 0, 2, 5] | не растёт монотонно | ✅ |
| лаг репликации | n/a (нет реплики) s | < 5 s | ✅ |

Запросов всего 8763; heartbeat p95 926.2 ms; operator API p95 0.0 ms; WS-сообщений 83749; соединений БД max 41; Redis PING p95 10.34 ms; WAL за прогон 28.8 MB; RSS в конце (KB) {'api': 317632, 'consumer': 132480, 'publisher': 128172, 'replayer': 81944}; лаг стримов (pending) max 157, в конце 0; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/soak-k6.log`, полные данные — `soak.json`.
