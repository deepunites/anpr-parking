# Профиль API «до» (фаза 8 §6)

Снят py-spy (`PARKING_E2E_PROFILE=1`, 100 Гц, ptrace) на первом нагрузочном прогоне `baseline` (SCALE=1/30,
RATE_SCALE=1/5, 1 uvicorn-воркер) до оптимизаций фазы 8. Сводка — `scripts/profile_summary.py`; доли — от активного
времени процесса (py-spy не учитывает простаивающие потоки).

Что видно:

- **traceback/ast/linecache ≈ 5 %** — форматирование исключений в горячем пути: шторм `TimeoutError` Redis при
  `socket_timeout` 0.2 с на голодающем по CPU стенде (`redis_store.get`, `format_exception`). Устранено: стенд и
  нагрузочный прогон используют 1.0 с (A-56), таймаут БД открывает breaker сразу (`trip()`), исключения не логируются
  с трассировкой на каждый запрос.
- **opentelemetry ≈ 13 %** — спаны на каждое ASGI-сообщение `http send`/`http receive` (в том числе для WebSocket)
  и дочерние спаны БД/Redis у несэмплированных ENTRY-трасс. Устранено: `exclude_spans=["receive","send"]`,
  WS-маршруты вне трассировки, `ParkingSampler` отбрасывает (DROP) дочерние `db.system`/CLIENT-спаны у
  несэмплированного родителя.
- аутентификация камеры (`camera_principal` 14.6 % inclusive) дороже самой обработки события (`process` 8.5 %):
  GET кеша кандидатов + HSET `last_used` в Redis на каждый запрос — оставлено (две команды Redis, ≈1 мс), не CPU.

Профиль `api-profile.speedscope.json (до)`: 138.9 с активного времени (py-spy, все потоки; простой не учитывается).

Где проводит время процесс API (по вершине стека):

| Категория | Доля |
|---|---|
| прочее | 32.4 % |
| asyncio (ожидание) | 14.4 % |
| opentelemetry | 13.1 % |
| sqlalchemy | 10.4 % |
| fastapi/starlette | 10.2 % |
| redis | 7.9 % |
| parking | 5.7 % |
| asyncpg | 2.9 % |
| uvicorn/h11 | 1.4 % |
| pydantic | 1.1 % |
| anyio | 0.6 % |

Топ-15 функций по собственному времени:

| Функция | Файл | Доля |
|---|---|---|
| `run` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/runners.py:118 | 8.3 % |
| `process 42634:"/home/deep/projects/anpr-parking/backend/.venv/bin/python -m uvicorn parking.api.main:app --host 127.0.0.1 --port 8010 --workers 1 --log-level warning"` | ?:0 | 4.3 % |
| `_get_code_position` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/traceback.py:369 | 2.3 % |
| `parse` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/ast.py:52 | 1.9 % |
| `reschedule` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/timeouts.py:71 | 1.5 % |
| `__aenter__` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/contextlib.py:704 | 1.2 % |
| `matches` | fastapi/routing.py:1518 | 1.2 % |
| `__do_execute` | asyncpg/prepared_stmt.py:257 | 1.1 % |
| `checkcache` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/linecache.py:75 | 1.1 % |
| `_compile_bytecode` | <frozen importlib._bootstrap_external>:757 | 1.0 % |
| `uuid4` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/uuid.py:725 | 0.9 % |
| `_get_filterable_args` | redis/utils.py:202 | 0.9 % |
| `open_connection` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/asyncio/streams.py:48 | 0.7 % |
| `notify` | /home/deep/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/lib/python3.12/threading.py:410 | 0.7 % |
| `start_span` | opentelemetry/sdk/trace/__init__.py:1259 | 0.7 % |

Топ-15 функций проекта по суммарному (inclusive) времени:

| Функция | Файл | Доля |
|---|---|---|
| `__call__` | parking/api/middleware/correlation.py:45 | 54.0 % |
| `__call__` | parking/api/middleware/limits.py:44 | 53.8 % |
| `__call__` | parking/api/middleware/request_log.py:36 | 53.3 % |
| `camera_event` | parking/api/v1/camera.py:127 | 18.3 % |
| `_ingest` | parking/api/v1/camera.py:107 | 18.2 % |
| `camera_principal` | parking/api/v1/camera.py:33 | 14.6 % |
| `authenticate` | parking/modules/devices/application/camera_auth.py:73 | 14.1 % |
| `_authenticate` | parking/modules/devices/application/camera_auth.py:85 | 9.6 % |
| `process` | parking/modules/anpr/application/ingest.py:204 | 8.5 % |
| `<module>` | parking/api/main.py:8 | 7.3 % |
| `_candidates` | parking/modules/devices/application/camera_auth.py:118 | 6.7 % |
| `get` | parking/modules/devices/infrastructure/redis_store.py:75 | 6.3 % |
| `camera_heartbeat` | parking/api/v1/camera.py:138 | 4.9 % |
| `record` | parking/modules/devices/infrastructure/redis_store.py:117 | 4.7 % |
| `_decision` | parking/modules/anpr/application/ingest.py:322 | 4.3 % |
