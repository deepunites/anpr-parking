"""§4: полный путь на поднятом стенде — въезд по событию камеры → сессия → шлагбаум открыт (фейковый контроллер) →
выезд → расчёт тарифа → оплата наличными → шлагбаум → сессия закрыта → outbox опубликован → данные видны в API.
Варианты: абонемент, VIP, whitelist со скидкой, blacklist, низкая уверенность (MANUAL_REVIEW), ручная коррекция
номера, осиротевшая сессия."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from tests.e2e.conftest import Stack

RECENT = timedelta(minutes=9)  # моложе late_after (10 мин): обычный въезд с открытием шлагбаума


@pytest.fixture(scope="session", autouse=True)
def zero_grace_tariff(stack: Stack) -> None:
    """Сидовый тариф площадки стенда: грейс 10 мин — въезд «9 минут назад» был бы бесплатным. Публикуем версию
    с грейсом 0 (валидна с текущего момента), чтобы выезд требовал оплаты."""
    with httpx.Client(timeout=15) as http:
        admin = stack.token(http, "tenant_admin")
        plans = http.get(f"{stack.api}/api/v1/lots/{stack.state['lot_id']}/tariff-plans", headers=admin)
        assert plans.status_code == 200, plans.text
        plan = next(p for p in plans.json() if p["is_default"])
        published = http.post(
            f"{stack.api}/api/v1/tariff-plans/{plan['id']}/versions",
            headers=admin,
            json={
                "rules": {
                    "grace_minutes": 0,
                    "first_hour_price": 1000000,
                    "price_per_hour": 500000,
                    "daily_max": 8000000,
                    "rounding_mode": "UP",
                    "rounding_step_minutes": 30,
                },
                "valid_from": (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat(),
                "reason": "e2e: zero grace",
            },
        )
        assert published.status_code in {201, 409}, published.text


def plate(prefix: str = "01") -> str:
    return f"{prefix}{uuid4().hex[:6].upper()}"


def event(stack: Stack, kind: str, pl: str, *, at: datetime | None = None, confidence: int = 95) -> dict[str, Any]:
    camera = "ENTRY_01" if kind == "ENTRY" else "EXIT_01"
    return {
        "camera_code": camera,
        "event_type": kind,
        "plate_number": pl,
        "confidence": confidence,
        "event_time": (at or datetime.now(tz=UTC)).isoformat(),
        "direction": "IN" if kind == "ENTRY" else "OUT",
        "recognition": {"provider": "e2e", "version": "1"},
    }


def send(stack: Stack, http: httpx.Client, kind: str, pl: str, **kw: Any) -> dict[str, Any]:
    key = stack.state["camera_keys"]["entry" if kind == "ENTRY" else "exit"]
    resp = http.post(
        f"{stack.api}/api/v1/camera/events", json=event(stack, kind, pl, **kw), headers={"X-Camera-Key": key}
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def session(stack: Stack, http: httpx.Client, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    resp = http.get(f"{stack.api}/api/v1/sessions/{session_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def saga(stack: Stack, http: httpx.Client, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    resp = http.get(f"{stack.api}/api/v1/sessions/{session_id}/saga", headers=headers)
    assert resp.status_code == 200, resp.text
    return dict(resp.json() or {})  # null, пока сага не создана


def wait_barrier_open(stack: Stack, command_id: str | None) -> None:
    if stack.barrier is None:
        pytest.skip("нет не-loopback адреса для фейкового контроллера шлагбаума")
    assert command_id is not None
    stack.wait(lambda: any(c["action"] == "open" and c["command_id"] == command_id for c in stack.commands()))


def outbox_rows(stack: Stack, aggregate_id: str) -> list[asyncpg.Record]:
    async def query() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(stack.state["db_url"])
        try:
            return await conn.fetch(
                "SELECT event_type, status::text AS status FROM outbox WHERE aggregate_id = $1 ORDER BY created_at",
                aggregate_id,
            )
        finally:
            await conn.close()

    return asyncio.run(query())


def test_full_path_cash_payment(stack: Stack, http: httpx.Client) -> None:
    operator = stack.token(http, "operator")
    pl = plate()
    entered = send(stack, http, "ENTRY", pl, at=datetime.now(tz=UTC) - RECENT)
    assert entered["status"] == "ENTRY_CONFIRMED" and entered["session_id"]
    sid = entered["session_id"]
    assert session(stack, http, operator, sid)["status"] == "ACTIVE"
    wait_barrier_open(stack, entered["barrier_command_id"])  # шлагбаум въезда открыт контроллером

    exited = send(stack, http, "EXIT", pl)
    assert exited["status"] == "PAYMENT_REQUIRED" and exited["session_id"] == sid
    # обязательство создаёт шаг саги (consumer): ждём payment_id, затем котировка и наличные через кассу
    stack.wait(lambda: saga(stack, http, operator, sid).get("payment_id") is not None, limit=60)
    pending = saga(stack, http, operator, sid)
    assert pending["state"] == "PAYMENT_REQUIRED" and pending["amount_due"] > 0
    obligation = http.get(f"{stack.api}/api/v1/payments/{pending['payment_id']}", headers=operator)
    assert obligation.status_code == 200 and obligation.json()["status"] == "PENDING", obligation.text
    amount = obligation.json()["amount_minor"]
    assert amount == pending["amount_due"] > 0
    calc = pending["calculation"]
    assert calc["amount"] == amount and calc["billing_minutes"] >= 9, "расчёт тарифа сохранён в саге (I-7 snapshot)"
    cashier = stack.token(http, "cashier")
    shift = http.post(
        f"{stack.api}/api/v1/shifts", headers=cashier, json={"lot_id": stack.state["lot_id"], "opening_float": 0}
    )
    assert shift.status_code in {200, 201, 409}, shift.text
    paid = http.post(
        f"{stack.api}/api/v1/payments/{pending['payment_id']}/confirm-cash",
        headers=cashier,
        json={"amount_minor": amount, "reason": "e2e cash"},
    )
    assert paid.status_code in {200, 201}, paid.text
    assert paid.json()["status"] == "PAID" and paid.json()["amount_minor"] == amount and paid.json()["method"] == "CASH"

    # сага после завершения из API не отдаётся (эндпоинт — только активная): ждём закрытия сессии
    stack.wait(lambda: session(stack, http, operator, sid)["status"] == "CLOSED", limit=60)
    closed = session(stack, http, operator, sid)
    assert closed["exit_kind"] == "CAMERA", closed  # выезд завершён потоком камеры после оплаты, не вручную
    opened = [c for c in stack.commands() if c["action"] == "open"]
    assert len(opened) >= 2, "второе открытие шлагбаума — на выезде после оплаты"
    assert closed["status"] == "CLOSED" and closed["exit_time"] and closed["billing_minutes"] >= 9

    rows = outbox_rows(stack, sid)
    types = [r["event_type"] for r in rows]
    assert "parking.session.created.v1" in types and "parking.session.closed.v1" in types
    stack.wait(lambda: all(r["status"] == "PUBLISHED" for r in outbox_rows(stack, sid)), limit=30)
    payments = http.get(f"{stack.api}/api/v1/sessions/{sid}/payments", headers=operator)
    assert payments.status_code == 200 and payments.json()[0]["status"] == "PAID"
    listed = http.get(
        f"{stack.api}/api/v1/lots/{stack.state['lot_id']}/sessions", headers=operator, params={"status": "CLOSED"}
    )
    assert listed.status_code == 200 and any(s["id"] == sid for s in listed.json()["items"]), listed.text[:300]
    print(
        f"\nGATE e2e full path: entry → barrier open → exit → quote {amount} → cash → saga COMPLETED → barrier open → CLOSED → outbox PUBLISHED × {len(rows)} → visible in API"
    )


@pytest.mark.parametrize("kind", ["SUBSCRIPTION", "VIP"])
def test_privileged_vehicle_exits_without_payment(stack: Stack, http: httpx.Client, kind: str) -> None:
    admin, operator = stack.token(http, "tenant_admin"), stack.token(http, "operator")
    pl = plate()
    now = datetime.now(tz=UTC)
    if kind == "SUBSCRIPTION":
        created = http.post(
            f"{stack.api}/api/v1/subscriptions",
            headers=admin,
            json={
                "plate": pl,
                "plan_code": "MONTHLY",
                "starts_at": (now - timedelta(days=1)).isoformat(),
                "ends_at": (now + timedelta(days=30)).isoformat(),
                "price_minor": 30000000,
                "lot_id": stack.state["lot_id"],
                "reason": "e2e",
            },
        )
    else:
        created = http.post(
            f"{stack.api}/api/v1/access-rules",
            headers=admin,
            json={
                "plate": pl,
                "kind": "VIP",
                "lot_id": stack.state["lot_id"],
                "valid_from": (now - timedelta(hours=1)).isoformat(),
                "reason": "e2e",
            },
        )
    assert created.status_code == 201, created.text
    entered = send(stack, http, "ENTRY", pl, at=now - RECENT)
    assert entered["status"] == "ENTRY_CONFIRMED"
    sid = entered["session_id"]
    exited = send(stack, http, "EXIT", pl)
    assert exited["status"] == "EXIT_CONFIRMED", exited
    stack.wait(lambda: session(stack, http, operator, sid)["status"] == "CLOSED", limit=60)
    payments = http.get(f"{stack.api}/api/v1/sessions/{sid}/payments", headers=operator)
    assert payments.status_code == 200
    assert [(p["method"], p["amount_minor"]) for p in payments.json()] == [(kind, 0)], (
        "льгота: нулевая запись со ссылкой на источник"
    )


def test_whitelist_discount_halves_the_quote(stack: Stack, http: httpx.Client) -> None:
    admin, operator = stack.token(http, "tenant_admin"), stack.token(http, "operator")
    pl, normal = plate(), plate()
    rule = http.post(
        f"{stack.api}/api/v1/access-rules",
        headers=admin,
        json={
            "plate": pl,
            "kind": "WHITELIST",
            "discount_percent": 50,
            "lot_id": stack.state["lot_id"],
            "valid_from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
            "reason": "e2e",
        },
    )
    assert rule.status_code == 201, rule.text
    entry_at = datetime.now(tz=UTC) - RECENT
    discounted = send(stack, http, "ENTRY", pl, at=entry_at)
    full = send(stack, http, "ENTRY", normal, at=entry_at)
    exit_discounted = send(stack, http, "EXIT", pl)
    exit_full = send(stack, http, "EXIT", normal)
    assert exit_discounted["status"] == exit_full["status"] == "PAYMENT_REQUIRED"
    for sid in (discounted["session_id"], full["session_id"]):
        stack.wait(lambda sid=sid: saga(stack, http, operator, sid).get("payment_id") is not None, limit=60)
    q_disc = saga(stack, http, operator, discounted["session_id"])["amount_due"]
    q_full = saga(stack, http, operator, full["session_id"])["amount_due"]
    assert q_full > 0 and abs(q_disc * 2 - q_full) <= 1, (q_disc, q_full)


def test_blacklisted_vehicle_never_opens_barrier(stack: Stack, http: httpx.Client) -> None:
    admin, operator = stack.token(http, "tenant_admin"), stack.token(http, "operator")
    pl = plate()
    assert (
        http.post(
            f"{stack.api}/api/v1/access-rules",
            headers=admin,
            json={
                "plate": pl,
                "kind": "BLACKLIST",
                "valid_from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
                "reason": "e2e stolen",
            },
        ).status_code
        == 201
    )
    before = len(stack.commands())
    denied = send(stack, http, "ENTRY", pl)
    assert denied["status"] == "ENTRY_DENIED" and denied["barrier_command_id"] is None
    assert session(stack, http, operator, denied["session_id"])["status"] == "MANUAL_REVIEW"
    import time

    time.sleep(2)
    assert len(stack.commands()) == before, "контроллер не получал команд"
    queue = http.get(f"{stack.api}/api/v1/sessions/review-queue", headers=operator)
    assert queue.status_code == 200
    items = queue.json()["items"] if isinstance(queue.json(), dict) else queue.json()
    assert any(s["id"] == denied["session_id"] for s in items)


def test_low_confidence_goes_to_review_and_operator_confirms(stack: Stack, http: httpx.Client) -> None:
    operator = stack.token(http, "operator")
    pl = plate()
    review = send(stack, http, "ENTRY", pl, confidence=70)  # между review_min (60) и min (80)
    assert review["status"] == "MANUAL_REVIEW" and review["barrier_command_id"] is None
    sid = review["session_id"]
    confirmed = http.post(
        f"{stack.api}/api/v1/sessions/{sid}/confirm-entry", headers=operator, json={"reason": "номер сверен по фото"}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "ACTIVE"
    stack.wait(lambda: any(c["action"] == "open" for c in stack.commands()[-5:]), limit=30) if stack.barrier else None
    rejected = http.post(
        f"{stack.api}/api/v1/camera/events",
        json=event(stack, "ENTRY", plate(), confidence=30),
        headers={"X-Camera-Key": stack.state["camera_keys"]["entry"]},
    )
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "LOW_CONFIDENCE"


def test_manual_plate_correction_then_exit_matches(stack: Stack, http: httpx.Client) -> None:
    operator = stack.token(http, "operator")
    wrong, right = plate(), plate()
    entered = send(stack, http, "ENTRY", wrong, at=datetime.now(tz=UTC) - RECENT)
    sid = entered["session_id"]
    corrected = http.post(
        f"{stack.api}/api/v1/sessions/{sid}/correct-plate",
        headers=operator,
        json={"plate": right, "reason": "камера перепутала символы"},
    )
    assert corrected.status_code == 200, corrected.text
    exited = send(stack, http, "EXIT", right)
    assert exited["session_id"] == sid and exited["status"] == "PAYMENT_REQUIRED"
    stranger = send(stack, http, "EXIT", wrong)
    assert stranger["session_id"] != sid, "исходный (ошибочный) номер больше не привязан к сессии"


def test_orphan_session_is_marked_and_listed(stack: Stack, http: httpx.Client) -> None:
    operator = stack.token(http, "operator")
    pl = plate()
    entered = send(stack, http, "ENTRY", pl, at=datetime.now(tz=UTC) - RECENT)
    sid = entered["session_id"]
    orphaned = http.post(
        f"{stack.api}/api/v1/sessions/{sid}/orphan", headers=operator, json={"reason": "машина эвакуирована"}
    )
    assert orphaned.status_code == 200 and orphaned.json()["status"] == "ORPHAN", orphaned.text
    listed = http.get(
        f"{stack.api}/api/v1/lots/{stack.state['lot_id']}/sessions", headers=operator, params={"status": "ORPHAN"}
    )
    assert listed.status_code == 200 and any(s["id"] == sid for s in listed.json()["items"])
    assert "parking.session.orphaned.v1" in {r["event_type"] for r in outbox_rows(stack, sid)}
