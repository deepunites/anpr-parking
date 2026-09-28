"""I-9. Открытая транзакция PostgreSQL никогда не ждёт внешний HTTP.

Механизм: (1) статически — httpx разрешён только в platform.http и */infrastructure, application-слой не импортирует
platform.http (tests/architecture); (2) динамически — все клиенты создаются make_async_client() с GuardedTransport,
который бросает TransactionBoundaryViolation при запросе внутри UnitOfWork (transaction_depth > 0). Драйверы
шлагбаумов и платёжные шлюзы вызываются исполнителем/сагой вне транзакции (команда сохраняется, транзакция
закрывается, затем HTTP).
При нарушении: пул critical-соединений и блокировки строк удерживаются на время таймаута провайдера (до 5–30 с) —
каскадная деградация въезда/выезда при медленном контроллере или банке.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from parking.platform.boundaries import TransactionBoundaryViolation, in_transaction
from parking.platform.http.client import make_async_client
from parking.runtime.container import Container
from tests.conftest import TENANT_A

SRC = Path(__file__).resolve().parents[2] / "src" / "parking"


def test_every_httpx_client_is_created_through_guarded_factory() -> None:
    """httpx.AsyncClient(...) напрямую — только в фабрике; иначе GuardedTransport можно обойти."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "client.py" and path.parent.name == "http":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {"AsyncClient", "Client"}:
                    value = func.value
                    if isinstance(value, ast.Name) and value.id == "httpx":
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], offenders


async def test_http_inside_unit_of_work_is_rejected(container: Container) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = make_async_client(base_url="http://barrier.local", transport=httpx.MockTransport(handler))
    try:
        assert not in_transaction()
        assert (await client.post("/open")).status_code == 200, "вне транзакции — работает"
        async with container.uow(TENANT_A):
            assert in_transaction()
            with pytest.raises(TransactionBoundaryViolation, match="I-9"):
                await client.post("/open")
        assert not in_transaction()
        assert (await client.post("/open")).status_code == 200, "после выхода из UoW — снова работает"
    finally:
        await client.aclose()


async def test_nested_units_of_work_keep_the_guard(container: Container) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = make_async_client(transport=httpx.MockTransport(handler))
    try:
        async with container.uow(TENANT_A), container.uow(TENANT_A):
            with pytest.raises(TransactionBoundaryViolation):
                await client.get("http://x/")
    finally:
        await client.aclose()
