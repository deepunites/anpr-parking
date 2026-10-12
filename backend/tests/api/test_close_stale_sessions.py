"""Закрытие старых сессий площадки: машины уезжали незамеченными (не было камеры выезда) — незакрытые сессии старше
N часов переводятся в «Осиротевшая» без начисления, место освобождается. Сначала подсчёт (ничего не меняет), потом —
только с причиной, в аудите по каждой сессии. Свежие сессии и сессии, по которым идёт выезд, не трогаются;
кассир без права sessions:write закрыть не может."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
import httpx

from parking.modules.identity.domain.permissions import Role
from tests.conftest import LOT_A1
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate

URL = f"/api/v1/lots/{LOT_A1}/sessions/close-stale"
REASON = "камеры выезда не было — машины уезжали незамеченными"


async def _enter(client: httpx.AsyncClient, entry_key: str, at: datetime) -> str:
    entered = await send_event(client, entry_key, event_body(unique_plate(), event_time=at))
    assert entered.status_code == 200 and entered.json()["session_id"], entered.text
    return str(entered.json()["session_id"])


async def _status(owner: asyncpg.Connection, session_id: str) -> str:
    return str(await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", UUID(session_id)))


async def test_close_stale_orphans_old_open_sessions_only(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection, entry_key: str
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    now = datetime.now(UTC)
    # «поздние» события (старше 10 мин) принимаются в окне площадки 24 ч — старая сессия с въездом 20 ч назад
    old = await _enter(client, entry_key, now - timedelta(hours=20))
    fresh = await _enter(client, entry_key, now - timedelta(minutes=5))

    preview = await client.post(URL, headers=admin, json={"older_than_hours": 12, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["found"] >= 1 and preview.json()["closed"] == 0
    assert await _status(owner, old) == "ACTIVE"  # подсчёт ничего не меняет

    no_reason = await client.post(URL, headers=admin, json={"older_than_hours": 12})
    assert no_reason.status_code == 422

    done = await client.post(URL, headers=admin, json={"older_than_hours": 12, "reason": REASON})
    assert done.status_code == 200, done.text
    assert done.json()["closed"] >= 1
    assert await _status(owner, old) == "ORPHAN"
    assert await _status(owner, fresh) == "ACTIVE"
    session = await client.get(f"/api/v1/sessions/{old}", headers=admin)
    assert session.json()["closed_reason"] == "STALE_MANUAL" and session.json()["billing_minutes"] is None
    audit = await owner.fetchval(
        "SELECT reason FROM audit_log WHERE action = 'SESSION_ORPHANED_MANUAL' AND session_id = $1", UUID(old)
    )
    assert audit == REASON

    again = await client.post(URL, headers=admin, json={"older_than_hours": 12, "dry_run": True})
    assert again.json()["found"] == preview.json()["found"] - done.json()["closed"]


async def test_close_stale_requires_sessions_write_and_sane_hours(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    viewer = await bearer(client, users[Role.VIEWER])
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    denied = await client.post(URL, headers=viewer, json={"older_than_hours": 12, "dry_run": True})
    assert denied.status_code == 403
    zero = await client.post(URL, headers=admin, json={"older_than_hours": 0, "dry_run": True})
    assert zero.status_code == 422  # «старше 0 часов» закрыло бы машины, которые стоят прямо сейчас


async def test_single_orphan_and_close_accept_long_reason(
    client: httpx.AsyncClient, users: dict[Role, Principal], owner: asyncpg.Connection, entry_key: str
) -> None:
    """Причина сотрудника — до 500 символов (Reason); в сессии — код, полная причина — в аудите."""
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    long_reason = "машина уехала, когда камера въезда была выключена после подключения второй камеры"
    assert len(long_reason) > 40
    first = await _enter(client, entry_key, datetime.now(UTC) - timedelta(minutes=2))
    orphaned = await client.post(f"/api/v1/sessions/{first}/orphan", headers=admin, json={"reason": long_reason})
    assert orphaned.status_code == 200 and orphaned.json()["closed_reason"] == "ORPHANED_MANUAL", orphaned.text
    second = await _enter(client, entry_key, datetime.now(UTC) - timedelta(minutes=1))
    closed = await client.post(f"/api/v1/sessions/{second}/close", headers=admin, json={"reason": long_reason})
    assert closed.status_code == 200 and closed.json()["closed_reason"] == "CLOSED_MANUAL", closed.text
    reasons = await owner.fetch(
        "SELECT reason FROM audit_log WHERE session_id = ANY($1::uuid[]) AND action LIKE 'SESSION_%_MANUAL'",
        [UUID(first), UUID(second)],
    )
    assert [r["reason"] for r in reasons] == [long_reason, long_reason]
