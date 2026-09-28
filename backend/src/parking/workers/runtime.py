"""Общий runtime воркеров: контейнер на процесс, запуск async-кода из Celery (sync), точки входа publisher/consumer."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable

from prometheus_client import start_http_server

from parking.platform import metrics
from parking.platform.config import Settings, load_settings
from parking.platform.db.engine import build_database
from parking.platform.logging import get_logger
from parking.platform.telemetry import bootstrap_telemetry, instrument_database
from parking.runtime.container import Container, build_container
from parking.runtime.key_refresh import run_key_refresh
from parking.workers.handlers import (
    BarrierDispatcher,
    CeleryBarrierDispatcher,
    InlineBarrierDispatcher,
    build_handlers,
    with_webhooks,
)
from parking.workers.outbox_publisher import OutboxPublisher
from parking.workers.realtime import RealtimeFanout, with_realtime
from parking.workers.stream_consumer import StreamConsumer

log = get_logger("workers")

_container: Container | None = None
_loop: asyncio.AbstractEventLoop | None = None


def container() -> Container:
    """Ленивый контейнер процесса-воркера (Celery форкает процессы — соединения создаются после форка)."""
    global _container  # noqa: PLW0603 — единственная точка инициализации процесса
    if _container is None:
        settings = load_settings()
        bootstrap_telemetry(settings, service="parking-worker")
        start_metrics_server(settings.metrics_port)
        database = build_database(settings)
        instrument_database(database)
        _container = build_container(settings, database)
    return _container


def start_metrics_server(port: int) -> bool:
    """/metrics воркера (publisher, consumer, Celery --pool=solo). Порт занят — соседний процесс уже отдаёт
    метрики этого хоста: логируем и продолжаем (prefork без multiprocess-режима не поддерживается)."""
    if port <= 0:
        return False
    try:
        start_http_server(port, registry=metrics.registry)
    except OSError as exc:
        log.warning("metrics_server_unavailable", port=port, error=str(exc))
        return False
    log.info("metrics_server_started", port=port)
    return True


def run_sync[T](coro: Awaitable[T]) -> T:
    """Celery-задачи синхронны: один event loop на процесс, чтобы пул соединений переживал вызовы."""
    global _loop  # noqa: PLW0603
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


def maintenance_dsn(settings: Settings) -> str:
    return settings.database_url_maintenance.get_secret_value().replace("postgresql+asyncpg://", "postgresql://", 1)


RESTART_BACKOFF_MAX_SECONDS = 30.0


async def supervise(
    name: str,
    run: Callable[[asyncio.Event], Awaitable[None]],
    stop: asyncio.Event,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Цикл воркера под супервизором: инфраструктурная ошибка (Redis/PostgreSQL недоступны — ConnectionError,
    TimeoutError) не убивает процесс, а перезапускает цикл с бэкоффом 1→30 с. Иначе отказ Redis ронял consumer
    (прогон runbook 3 фазы 10), а Kubernetes перезапускал бы под с растущей задержкой CrashLoopBackOff до 5 мин.
    Возвращает число перезапусков."""
    restarts = 0
    backoff = 1.0
    while not stop.is_set():
        try:
            await run(stop)
            return restarts
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            restarts += 1
            metrics.worker_loop_restarts_total.labels(worker=name).inc()
            log.error(
                "worker_loop_crashed", worker=name, error_type=type(exc).__name__, restart_in=backoff, restarts=restarts
            )
            await sleep(backoff)
            backoff = min(backoff * 2, RESTART_BACKOFF_MAX_SECONDS)
    return restarts


async def _serve(make: Callable[[Container, asyncio.Event], Awaitable[None]]) -> None:
    """SIGTERM/SIGINT → stop: цикл воркера дорабатывает текущую запись/батч и выходит сам (Kubernetes даёт
    terminationGracePeriodSeconds до SIGKILL). До фазы 9 событие создавалось, но в цикл не передавалось —
    процесс игнорировал SIGTERM и жил до SIGKILL посреди транзакции."""
    c = container()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    keys_task = asyncio.create_task(run_key_refresh(c, stop))  # v2: ключи новых клиентов без перезапуска
    try:
        await supervise(make.__name__, lambda s: make(c, s), stop)
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(keys_task, 5)
        await c.database.dispose()


async def run_publisher(c: Container, stop: asyncio.Event | None = None) -> None:
    publisher = OutboxPublisher(c.database, c.redis, maintenance_dsn=maintenance_dsn(c.settings))
    await publisher.run_forever(stop or asyncio.Event())


async def run_consumer(c: Container, stop: asyncio.Event | None = None, *, consumer_name: str = "consumer-1") -> None:
    barriers: BarrierDispatcher = (
        CeleryBarrierDispatcher() if c.settings.barrier_dispatch == "celery" else InlineBarrierDispatcher(c)
    )  # prod: очередь Celery barrier изолирует HTTP к контроллерам от потребителя стримов
    consumer = StreamConsumer(
        c.database,
        c.redis,
        with_webhooks(with_realtime(build_handlers(c, barriers), RealtimeFanout(c)), c),
        consumer_name=consumer_name,
    )
    await consumer.run_forever(stop or asyncio.Event())


async def run_offline_replayer(
    c: Container, stop: asyncio.Event | None = None, *, consumer_name: str = "replayer-1"
) -> None:
    """Воспроизведение anpr:offline в PostgreSQL после восстановления (фаза 4 §4)."""
    await c.offline_replayer(consumer_name=consumer_name).run_forever(stop or asyncio.Event())


async def run_camera_health(c: Container, stop: asyncio.Event | None = None, *, interval: float = 30.0) -> None:
    """Альтернатива Celery beat для окружений без Celery: тот же CameraHealthJob раз в 30 с."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        await c.camera_health.run_once()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)  # SIGTERM прерывает паузу сразу


def main_publisher() -> None:
    asyncio.run(_serve(run_publisher))


def main_offline_replayer() -> None:
    asyncio.run(_serve(run_offline_replayer))


def main_camera_health() -> None:
    asyncio.run(_serve(run_camera_health))


async def run_webhook_dispatcher(c: Container, stop: asyncio.Event | None = None, *, interval: float = 2.0) -> None:
    """v2 этап 6: отправка вебхуков — каждые 2 с забирает наступившие доставки (SKIP LOCKED: диспетчеров может быть
    несколько). Сбой прохода не останавливает цикл."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            sent = await c.outgoing_webhooks.deliver_due()
        except Exception as exc:
            log.warning("webhook_dispatch_failed", error_type=type(exc).__name__)
            sent = 0
        if sent == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)


def main_webhooks() -> None:
    asyncio.run(_serve(run_webhook_dispatcher))


def main_consumer() -> None:
    asyncio.run(_serve(run_consumer))
