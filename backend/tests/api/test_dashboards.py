"""v2 этап 7: дашборды на реальных действиях — оплата наличными двигает выручку, смена с недостачей даёт сигнал
«расхождение кассы», ручные открытия видны по оператору, история статусов камер даёт доступность;
сводка владельца платформы и здоровье из Prometheus."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from parking.modules.reports.domain.dashboard import manual_open_signal
from parking.modules.reports.infrastructure.prometheus import PrometheusHealth
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import BARRIER_A1_IN, CAM_A1_OUT, LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.billing_fixtures import as_user, ensure_open_shift, make_obligation
from tests.support.camera_fixtures import unique_plate


async def _owner(client: httpx.AsyncClient, headers: dict[str, str], **params: str) -> dict[str, Any]:
    resp = await client.get("/api/v1/dashboard/owner", headers=headers, params={"lot_id": str(LOT_A1), **params})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _opens(board: dict[str, Any], user_id: str) -> int:
    return sum(o["opens"] for o in board["manual"] if o["user_id"] == user_id)


async def test_owner_dashboard_reflects_cash_shift_and_manual_opens(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    before = await _owner(client, admin)
    assert before["period_end"] >= before["period_start"] and len(before["revenue_by_day"]) == 7
    assert len(before["load_by_hour"]) == 24 and before["timezone"]

    # выручка: обязательство саги оплачено наличными в открытой смене кассира
    cashier = as_user(users[Role.CASHIER])
    shift = await ensure_open_shift(container, cashier)
    obligation = await make_obligation(container, owner, PlateNumber.parse(unique_plate("01")), hours=3)
    await container.cashier.confirm_cash(cashier, obligation.payment_id, declared_amount=None, reason=None)
    # смена закрыта с недостачей: пересчитали 0 при ненулевой ожидаемой сумме
    closed = await container.cashier.close_shift(cashier, shift.id, counted_cash=0, notes="dashboard test")
    assert closed.discrepancy is not None and closed.discrepancy < 0

    # ручные открытия оператором
    operator = await bearer(client, users[Role.OPERATOR])
    for _ in range(6):
        r = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/open", headers=operator, json={"reason": "застрял"})
        assert r.status_code == 202, r.text

    after = await _owner(client, admin)
    assert after["revenue"]["value"] - before["revenue"]["value"] == obligation.amount
    assert after["revenue_by_method"].get("CASH", 0) - before["revenue_by_method"].get("CASH", 0) == obligation.amount
    assert after["payments"] == before["payments"] + 1
    assert sum(d["total"] for d in after["revenue_by_day"]) >= obligation.amount
    lot = next(row for row in after["lots"] if row["lot_id"] == str(LOT_A1))
    assert lot["revenue"] >= obligation.amount

    mine = [s for s in after["shifts"] if s["shift_id"] == str(shift.id)]
    assert mine and mine[0]["discrepancy"] == closed.discrepancy and mine[0]["cashier_name"]
    codes = {s["code"] for s in after["signals"]}
    assert "CASH_DISCREPANCY" in codes

    operator_id = str(users[Role.OPERATOR].user_id)
    assert _opens(after, operator_id) - _opens(before, operator_id) == 6
    opens_total = sum(o["opens"] for o in after["manual"])
    expected = manual_open_signal(opens_total, after["exits"], None, 0) is not None
    assert ("MANY_MANUAL_OPENS" in codes) is expected
    assert {lim["kind"] for lim in after["limits"]} == {"lots", "places", "cameras"}


async def test_camera_availability_from_status_history(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection
) -> None:
    original = await owner.fetchval("SELECT status::text FROM camera WHERE id = $1", CAM_A1_OUT)
    logged = await owner.fetchval("SELECT count(*) FROM camera_status_log WHERE camera_id = $1", CAM_A1_OUT)
    try:
        await owner.execute("UPDATE camera SET status = 'ONLINE' WHERE id = $1", CAM_A1_OUT)
        await owner.execute("UPDATE camera SET status = 'OFFLINE' WHERE id = $1", CAM_A1_OUT)
        await owner.execute("UPDATE camera SET status = 'OFFLINE' WHERE id = $1", CAM_A1_OUT)  # не переход
    finally:
        await owner.execute("UPDATE camera SET status = CAST($2 AS camera_status) WHERE id = $1", CAM_A1_OUT, original)
    after = await owner.fetchval("SELECT count(*) FROM camera_status_log WHERE camera_id = $1", CAM_A1_OUT)
    assert after - logged == (2 if original == "OFFLINE" else 3)
    board = await _owner(client, await bearer(client, users[Role.TENANT_ADMIN]))
    cam = next(c for c in board["cameras"] if c["camera_id"] == str(CAM_A1_OUT))
    assert cam["availability"] is not None and 0.0 <= cam["availability"] <= 1.0


async def test_owner_dashboard_validation_and_roles(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    bad = await client.get("/api/v1/dashboard/owner", headers=admin, params={"from": "2026-01-01", "to": "2026-09-01"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_PERIOD"
    month = await client.get(
        "/api/v1/dashboard/owner", headers=admin, params={"from": "2026-08-01", "to": "2026-08-31"}
    )
    assert month.status_code == 200 and len(month.json()["revenue_by_day"]) == 31
    last30 = await client.get("/api/v1/dashboard/owner", headers=admin, params={"days": "30"})
    assert last30.status_code == 200 and len(last30.json()["revenue_by_day"]) == 30
    owner_role = await bearer(client, users[Role.OWNER])
    assert (await client.get("/api/v1/dashboard/owner", headers=owner_role)).status_code == 200
    for role in (Role.OPERATOR, Role.CASHIER, Role.VIEWER, Role.PLATFORM_OWNER):
        resp = await client.get("/api/v1/dashboard/owner", headers=await bearer(client, users[role]))
        assert resp.status_code == 403, role


async def test_platform_dashboard_aggregates(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    platform = await bearer(client, users[Role.PLATFORM_OWNER])
    resp = await client.get("/api/v1/dashboard/platform", headers=platform)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sum(body["clients"].values()) >= 2 and body["mrr"] >= 0
    assert body["health_source"] == "local" and body["local"]["cameras_total"] >= 1
    assert all(a["reasons"] for a in body["attention"])
    assert "plate" not in json.dumps(body).lower()  # только агрегаты, без данных водителей
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    assert (await client.get("/api/v1/dashboard/platform", headers=admin)).status_code == 403


PROM_VALUES = {
    "count(up": "2",
    "histogram_quantile(0.95, sum(rate(http_request": "0.087",
    "sum(rate(http_request_duration_seconds_count{": "0.002",
    "histogram_quantile(0.95, sum(rate(exit_end": "NaN",
    "max(outbox": "1.5",
}


async def _prom(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    query = httpx.URL(head.split(" ")[1]).params.get("query", "")
    if query.startswith("ALERTS"):
        result: list[dict[str, Any]] = [{"metric": {"alertname": "CameraOffline"}, "value": [0, "1"]}]
    else:
        value = next(v for k, v in PROM_VALUES.items() if query.startswith(k))
        result = [{"metric": {}, "value": [0, value]}]
    body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    await writer.drain()
    writer.close()


@pytest.fixture
async def prometheus_url() -> AsyncIterator[str]:
    server = await asyncio.start_server(_prom, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    server.close()


async def test_prometheus_health_snapshot(prometheus_url: str) -> None:
    health = PrometheusHealth(prometheus_url)
    try:
        snap = await health.snapshot()
    finally:
        await health.aclose()
    assert snap is not None
    assert (snap.api_up, snap.api_p95_ms, snap.api_error_rate) == (2, 87.0, 0.002)
    assert snap.exit_p95_ms is None and snap.outbox_lag_seconds == 1.5
    assert snap.firing_alerts == ["CameraOffline"]
    down = PrometheusHealth("http://127.0.0.1:9")
    try:
        assert await down.snapshot() is None  # недоступен — дашборд переходит на локальные признаки
    finally:
        await down.aclose()
    assert await PrometheusHealth(None).snapshot() is None
