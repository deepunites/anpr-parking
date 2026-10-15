"""I-15. Любое ручное действие оператора содержит user_id, reason и запись в AuditLog.

Механизм: во всех командах оператора параметр reason обязателен (keyword-only, API — Reason ≥ 3 символов → 422),
CurrentUser даёт user_id; каждая операция пишет AuditEntry(actor_type=USER, user_id, reason) в той же транзакции;
БД: CHECK ck_audit_user_reason (USER-запись без user_id/reason отвергается), audit_log — append-only (I-11).
При нарушении: невозможно установить, кто и почему открыл шлагбаум/закрыл сессию — споры с водителями и
внутренние злоупотребления без следа.
"""

from __future__ import annotations

import inspect
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.parking.application.services import ParkingService
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer
from tests.support.parking_fixtures import entry_cmd

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
MANUAL_METHODS = ("correct_plate", "confirm_entry", "manual_open_exit", "close_manual", "mark_orphan", "match_exit")


def test_every_manual_operation_requires_actor_and_reason() -> None:
    for name in MANUAL_METHODS:
        signature = inspect.signature(getattr(ParkingService, name))
        params = signature.parameters
        assert "actor" in params and params["actor"].annotation in {"CurrentUser", CurrentUser}, name
        reason = params["reason"]
        assert reason.kind is inspect.Parameter.KEYWORD_ONLY and reason.default is inspect.Parameter.empty, (
            f"{name}: reason должен быть обязательным keyword-only параметром"
        )


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("confirm_entry", "ENTRY_CONFIRMED_MANUAL"),
        ("close_manual", "SESSION_CLOSED_MANUAL"),
        ("mark_orphan", "SESSION_ORPHANED"),
    ],
)
async def test_manual_action_writes_audit_with_user_and_reason(
    container: Container, owner: asyncpg.Connection, clean_lot: None, method: str, action: str
) -> None:
    # свой номер на каждый случай: тот же номер через секунды после выезда — эхо выезда, а не новый въезд
    plate = PlateNumber.parse(f"01R{uuid4().int % 900 + 100}RR")
    entry = await container.parking.process_entry(entry_cmd(plate, requires_review=True))
    assert entry.session_id is not None
    reason = f"invariant I-15 {method}"
    await getattr(container.parking, method)(ADMIN, entry.session_id, reason=reason)
    rows = await owner.fetch(
        "SELECT actor_type::text AS actor_type, user_id, reason, action FROM audit_log WHERE session_id = $1 AND reason = $2",
        entry.session_id,
        reason,
    )
    assert rows, f"{method}: нет записи audit_log"
    assert all(r["actor_type"] == "USER" and r["user_id"] == USER_A_ADMIN for r in rows), rows
    actions = {r["action"] for r in rows}
    assert actions, actions


async def test_api_rejects_manual_action_without_reason(
    client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], clean_lot: None
) -> None:
    entry = await container.parking.process_entry(entry_cmd(PlateNumber.parse("01R666RR"), requires_review=True))
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    for path, body in (
        (f"/api/v1/sessions/{entry.session_id}/confirm-entry", {}),
        (f"/api/v1/sessions/{entry.session_id}/confirm-entry", {"reason": ""}),
        (f"/api/v1/sessions/{entry.session_id}/confirm-entry", {"reason": "ok"}),  # короче 3 символов
        (f"/api/v1/sessions/{entry.session_id}/correct-plate", {"plate": "01R667RR"}),
        (f"/api/v1/sessions/{entry.session_id}/close", {}),
        (f"/api/v1/sessions/{entry.session_id}/orphan", {"reason": "  "}),
    ):
        resp = await client.post(path, headers=headers, json=body)
        assert resp.status_code == 422, (path, body, resp.text)


async def test_database_rejects_user_audit_row_without_reason(owner: asyncpg.Connection) -> None:
    from parking.platform.db.tenant import tenant_transaction

    async def insert(**fields: Any) -> None:
        async with tenant_transaction(owner, TENANT_A):
            await owner.execute(
                """
                INSERT INTO audit_log (tenant_id, actor_type, actor_id, user_id, action, entity_type, entity_id, reason, payload, correlation_id)
                VALUES ($1, 'USER', $2, $3, 'MANUAL_TEST', 'parking_session', gen_random_uuid(), $4, '{}', gen_random_uuid())
                """,
                TENANT_A,
                fields.get("user_id"),
                fields.get("user_id"),
                fields.get("reason"),
            )

    with pytest.raises(asyncpg.CheckViolationError):
        await insert(user_id=USER_A_ADMIN, reason=None)
    with pytest.raises(asyncpg.CheckViolationError):
        await insert(user_id=None, reason="valid reason")
    await insert(user_id=USER_A_ADMIN, reason="valid reason")
