# Профиль API «после» (фаза 8 §6)

Тот же сценарий (`baseline`, SCALE=1/30, RATE_SCALE=1/5, 1 uvicorn-воркер), py-spy `--nonblocking` 50 Гц после
оптимизаций фазы 8. Латентность под профилировщиком не показательна (ptrace/чтение памяти чужого процесса на
2 vCPU) — сравниваются только доли CPU.

| Категория (вершина стека) | до | после |
|---|---|---|
| opentelemetry | 13.1 % | 6.3 % (ASGI-инструментация 6.9 % → 2.2 %: выключены спаны `http send/receive`, WS вне трассировки; DROP дочерних db/client-спанов у несэмплированных трасс) |
| traceback/ast/linecache (форматирование исключений) | ≈ 5 % | 0 % (шторм `TimeoutError` Redis устранён: socket_timeout стенда 1.0 с, breaker БД по таймауту) |
| sqlalchemy + asyncpg | 13.3 % | 18.0 % (доля выросла, потому что накладные расходы вокруг уменьшились; ≈ 18 SQL-операторов на ENTRY — следующий кандидат на оптимизацию: объединить чтения в один CTE, staging фазы 9) |
| код проекта (`parking`) | 5.7 % | 6.9 % |
| redis | 7.9 % | 10.4 % (≈ 7 команд на событие: rate limit, кеш ключа, `last_used`, идемпотентность, решение доступа, heartbeat EWMA) |

Итог: на этом стенде латентность p95 определяется конкуренцией за 2 vCPU между k6, PostgreSQL, Redis, API и
воркерами, а не одним горячим местом в коде; CPU-профиль процесса API стал ровнее (нет пиков от исключений и
трассировки), дальнейшая оптимизация — на репрезентативном железе по профилю оттуда (A-49).

Профиль `api-profile.speedscope.json (после)`: 138.2 с активного времени (py-spy, все потоки; простой не учитывается).

Где проводит время процесс API (по вершине стека):

| Категория | Доля |
|---|---|
| прочее | 27.3 % |
| asyncio (ожидание) | 18.8 % |
| sqlalchemy | 13.7 % |
| redis | 10.4 % |
| fastapi/starlette | 8.7 % |
| parking | 6.9 % |
| opentelemetry | 6.3 % |
| asyncpg | 4.3 % |
| pydantic | 1.8 % |
| uvicorn/h11 | 1.3 % |
| anyio | 0.5 % |

Топ-15 функций по собственному времени:

| Функция | Файл | Доля |
|---|---|---|
| `run` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/runners.py:118 | 10.6 % |
| `process 15224:"/home/deep/projects/anpr-parking/backend/.venv/bin/python -m uvicorn parking.api.main:app --host 127.0.0.1 --port 8010 --workers 1 --log-level warning"` | ?:0 | 10.3 % |
| `__do_execute` | asyncpg/prepared_stmt.py:257 | 2.2 % |
| `reschedule` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/timeouts.py:71 | 2.0 % |
| `_compile_bytecode` | <frozen importlib._bootstrap_external>:757 | 1.1 % |
| `notify` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/threading.py:410 | 0.8 % |
| `_create_fn` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/dataclasses.py:473 | 0.7 % |
| `uuid4` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/uuid.py:725 | 0.6 % |
| `greenlet_spawn` | sqlalchemy/util/_concurrency_py3k.py:203 | 0.5 % |
| `data_received` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/streams.py:284 | 0.5 % |
| `execute` | asyncpg/connection.py:354 | 0.5 % |
| `greenlet_spawn` | sqlalchemy/util/_concurrency_py3k.py:190 | 0.5 % |
| `app` | fastapi/routing.py:137 | 0.5 % |
| `handle_pubsub_push_response` | redis/_parsers/resp3.py:174 | 0.4 % |
| `get_request_handler` | fastapi/routing.py:375 | 0.4 % |

Топ-15 функций проекта по суммарному (inclusive) времени:

| Функция | Файл | Доля |
|---|---|---|
| `__call__` | parking/api/middleware/request_log.py:36 | 21.8 % |
| `__call__` | parking/api/middleware/limits.py:44 | 21.8 % |
| `__call__` | parking/api/middleware/correlation.py:45 | 21.6 % |
| `_ingest` | parking/api/v1/camera.py:107 | 9.0 % |
| `camera_event` | parking/api/v1/camera.py:127 | 9.0 % |
| `<module>` | parking/api/main.py:8 | 7.2 % |
| `process` | parking/modules/anpr/application/ingest.py:204 | 6.8 % |
| `_pump` | parking/api/ws.py:150 | 5.0 % |
| `<module>` | parking/api/app.py:8 | 3.6 % |
| `camera_principal` | parking/api/v1/camera.py:33 | 3.5 % |
| `authenticate` | parking/modules/devices/application/camera_auth.py:73 | 3.4 % |
| `list_sessions` | parking/api/v1/sessions.py:121 | 3.4 % |
| `list_sessions` | parking/modules/parking/application/services.py:891 | 2.7 % |
| `_commit` | parking/modules/anpr/application/ingest.py:340 | 2.7 % |
| `_commit` | parking/modules/anpr/application/ingest.py:331 | 2.6 % |
