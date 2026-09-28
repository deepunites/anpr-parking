# k6 — сценарий `peak` (k6-peak-1789882262)

SCALE=1/30 (длительность 79 с), RATE_SCALE=1/10 (интенсивности §6 поделены), uvicorn workers=2, стенд: лаборатория primary+sync standby; k6 v2.2.0.

| Метрика | Значение | Критерий | Статус |
|---|---|---|---|
| POST /camera/events p95 | 9474.6 ms | < 150 ms | ❌ |
| POST /camera/events p99 | 11385.2 ms | < 400 ms | ❌ |
| error rate (camera events) | 0.000 % (5xx/сеть: 0) | < 0.1 % | ✅ |
| EXIT end-to-end p95 (без оплаты, n=95) | 113.42 s | < 2 s | ❌ |
| дубликаты сессий | 0 | 0 | ✅ |
| потерянные события: принято 849 (дублей 38, 202: 0) = в БД 811 + DLQ 0 + буфер 0 | потеряно 0 | 0 | ✅ |
| рост RSS | {'api': 0.049, 'consumer': 0.056, 'publisher': 0.016, 'replayer': 0.0} | < 10 % | ✅ |
| outbox_pending под нагрузкой | [0, 0, 12, 1, 8, 10, 5, 2, 7, 0, 1] | не растёт монотонно | ✅ |
| лаг репликации | 0.012143 s | < 5 s | ✅ |

Запросов всего 1449; heartbeat p95 2211.5 ms; operator API p95 0.0 ms; WS-сообщений 3194; соединений БД max 53; Redis PING p95 16.9 ms; WAL за прогон 9.3 MB; RSS в конце (KB) {'api': 344712, 'consumer': 136600, 'publisher': 130752, 'replayer': 127804}; лаг стримов (pending) max 672, в конце 672; DLQ consumer'а за прогон 0; саг в MANUAL_REVIEW 0.

k6 thresholds: FAILED; лог — `docs/backend/load/peak-k6.log`, полные данные — `peak.json`.
