"""Фаза 7 §1: correlation_id проходит через Celery — заголовок при публикации, contextvars внутри задачи."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog

from parking.shared.correlation import correlation_id_var, correlation_scope, get_correlation_id
from parking.workers.celery_app import _attach_correlation, app

CID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _fresh_context() -> Iterator[None]:
    token = correlation_id_var.set(None)
    structlog.contextvars.clear_contextvars()
    yield
    correlation_id_var.reset(token)


@app.task(name="tests.echo_correlation")
def _echo_correlation() -> dict[str, object]:
    return {"cid": get_correlation_id(), "bound": dict(structlog.contextvars.get_contextvars())}


def test_publish_attaches_current_correlation_id() -> None:
    headers: dict[str, object] = {}
    with correlation_scope(CID):
        _attach_correlation(headers=headers)
    assert headers["correlation_id"] == CID
    kept = {"correlation_id": "explicit"}
    _attach_correlation(headers=kept)
    assert kept["correlation_id"] == "explicit", "явный заголовок не перезаписывается"


def test_task_runs_inside_correlation_from_headers() -> None:
    app.conf.task_always_eager = True
    try:
        result = _echo_correlation.apply(headers={"correlation_id": CID}).get()
    finally:
        app.conf.task_always_eager = False
    assert result["cid"] == CID
    assert result["bound"]["correlation_id"] == CID and result["bound"]["celery_task"] == "tests.echo_correlation"
    assert correlation_id_var.get() is None, "после задачи контекст воркера восстановлен"
    assert "celery_task" not in structlog.contextvars.get_contextvars()


def test_task_without_header_gets_fresh_correlation_id() -> None:
    app.conf.task_always_eager = True
    try:
        first = _echo_correlation.apply().get()["cid"]
        second = _echo_correlation.apply().get()["cid"]
    finally:
        app.conf.task_always_eager = False
    assert first != second and len(str(first)) == 36
