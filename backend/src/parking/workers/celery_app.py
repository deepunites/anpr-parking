"""Celery: очереди saga / barrier / maintenance (payments и notify — фазы 5 и 7). Broker и backend — Redis."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import structlog
from celery import Celery
from celery.schedules import schedule
from celery.signals import before_task_publish, task_postrun, task_prerun, worker_init

from parking.platform.redis import celery_broker
from parking.shared.correlation import correlation_id_var, get_correlation_id, new_correlation_id
from parking.workers.runtime import container, run_sync

_broker_url, _broker_options = celery_broker(os.environ.get("PARKING_REDIS_URL", "redis://localhost:6379/0"))
app = Celery("parking", broker=_broker_url)
app.conf.update(
    broker_transport_options={**_broker_options, "visibility_timeout": 3600},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "parking.saga.advance": {"queue": "saga"},
        "parking.saga.recover": {"queue": "saga"},
        "parking.barrier.execute": {"queue": "barrier"},
        "parking.maintenance.*": {"queue": "maintenance"},
        "parking.payments.*": {"queue": "payments"},
        "parking.media.*": {"queue": "maintenance"},
    },
    beat_schedule={
        "recover-sagas": {"task": "parking.saga.recover", "schedule": schedule(30.0)},
        "sweep-stale-sessions": {"task": "parking.maintenance.sweep_stale", "schedule": schedule(600.0)},
        "camera-health": {"task": "parking.maintenance.camera_health", "schedule": schedule(30.0)},
        "payments-verify": {"task": "parking.payments.verify_pending", "schedule": schedule(60.0)},
        "payments-orphans": {"task": "parking.payments.replay_orphans", "schedule": schedule(60.0)},
        "payments-refunds": {"task": "parking.payments.process_refunds", "schedule": schedule(60.0)},
        "payments-reconcile": {"task": "parking.payments.reconcile_daily", "schedule": schedule(24 * 3600.0)},
        "sweep-prepaid": {"task": "parking.maintenance.sweep_prepaid", "schedule": schedule(300.0)},
        "media-retry": {"task": "parking.media.retry_pending", "schedule": schedule(60.0)},
    },
    timezone="UTC",
)


@worker_init.connect
def _init_worker(**_: Any) -> None:
    """Контейнер (и /metrics на PARKING_METRICS_PORT) поднимаются при старте воркера, а не при первой задаче:
    иначе liveness-проба Kubernetes по /metrics не проходит на простаивающем воркере."""
    container()


# --- сквозной correlation_id через Celery (traceparent пропагирует CeleryInstrumentor) ---


@before_task_publish.connect
def _attach_correlation(headers: dict[str, Any] | None = None, **_: Any) -> None:
    if headers is not None and not headers.get("correlation_id"):
        headers["correlation_id"] = get_correlation_id()


@task_prerun.connect
def _enter_correlation(task: Any = None, **_: Any) -> None:
    request = getattr(task, "request", None)
    cid = getattr(request, "correlation_id", None) or (request.headers or {}).get("correlation_id") if request else None
    token = correlation_id_var.set(str(cid) if cid else new_correlation_id())
    structlog.contextvars.bind_contextvars(
        correlation_id=correlation_id_var.get(), celery_task=getattr(task, "name", None)
    )
    _TOKENS[id(request)] = token


@task_postrun.connect
def _leave_correlation(task: Any = None, **_: Any) -> None:
    request = getattr(task, "request", None)
    token = _TOKENS.pop(id(request), None)
    structlog.contextvars.unbind_contextvars("correlation_id", "celery_task")
    if token is not None:
        correlation_id_var.reset(token)


_TOKENS: dict[int, Any] = {}


@app.task(name="parking.saga.advance", max_retries=5, default_retry_delay=2)
def advance_saga(tenant_id: str, saga_id: str) -> str:
    return run_sync(container().parking.advance_saga(UUID(tenant_id), UUID(saga_id))).value


@app.task(name="parking.saga.recover")
def recover_sagas() -> int:
    c = container()
    return run_sync(c.parking.recover_due(c.saga_scanner))


@app.task(name="parking.barrier.execute", max_retries=0)
def execute_barrier_command(tenant_id: str, command_id: str) -> str:
    return run_sync(container().barrier_executor.execute(UUID(tenant_id), UUID(command_id))).value


@app.task(name="parking.maintenance.sweep_stale")
def sweep_stale(tenant_id: str | None = None) -> int:
    c = container()
    if tenant_id is not None:
        return run_sync(c.parking.sweep_stale(UUID(tenant_id)))
    return 0


@app.task(name="parking.maintenance.camera_health")
def camera_health() -> int:
    """Раз в 30 с: статусы камер по Redis-heartbeat + батч-флаш last_heartbeat_at/last_event_at/last_used_at."""
    report = run_sync(container().camera_health.run_once())
    return report.checked


async def _for_each_provider(action: str) -> int:
    c = container()
    total = 0
    for tenant_id, _provider in await c.billing_scanner.tenants_with_providers():
        if action == "verify":
            total += await c.online_payments.verify_unknown(tenant_id)
        elif action == "orphans":
            total += await c.webhooks.replay_orphans(tenant_id)
        elif action == "refunds":
            total += await c.refunds.process_pending(tenant_id)
    return total


@app.task(name="parking.payments.verify_pending")
def verify_pending() -> int:
    """Попытки UNKNOWN/TIMEOUT → status() провайдера (ретрай create() запрещён)."""
    return run_sync(_for_each_provider("verify"))


@app.task(name="parking.payments.replay_orphans")
def replay_orphans() -> int:
    """Вебхуки, пришедшие раньше платежа, применяются, когда платёж появился."""
    return run_sync(_for_each_provider("orphans"))


@app.task(name="parking.payments.process_refunds")
def process_refunds() -> int:
    return run_sync(_for_each_provider("refunds"))


async def _reconcile_daily() -> int:
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    c = container()
    yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).date()
    runs = 0
    for tenant_id, provider in await c.billing_scanner.tenants_with_providers():
        await c.reconciliation.run(tenant_id, provider, yesterday, yesterday, created_by=None)
        runs += 1
    return runs


@app.task(name="parking.payments.reconcile_daily")
def reconcile_daily() -> int:
    """Ежедневная сверка за вчера по каждому активному мерчант-аккаунту; по требованию — POST /api/v1/reconciliations."""
    return run_sync(_reconcile_daily())


async def _sweep_prepaid() -> int:
    c = container()
    return sum([await c.parking.sweep_prepaid(t) for t in await c.billing_scanner.active_tenants()])


@app.task(name="parking.maintenance.sweep_prepaid")
def sweep_prepaid() -> int:
    """Оплатили, но выехали без камеры: закрытие сессий как INFERRED_BY_PAYMENT (A-11)."""
    return run_sync(_sweep_prepaid())


@app.task(name="parking.media.retry_pending")
def retry_pending_media() -> dict[str, int]:
    """Повторная отправка кадров, отложенных при недоступности MinIO (фаза 8 §5)."""
    sent, deferred = run_sync(container().media.retry_pending())
    return {"sent": sent, "deferred": deferred}
