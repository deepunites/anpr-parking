"""§7: межтенантный доступ по КАЖДОМУ эндпоинту с ресурсом. Ресурсы tenant A создаются реально; tenant B (TENANT_ADMIN)
вызывает каждый метод каждого пути с {id} из docs/openapi.json с валидным телом → только 403/404, никогда 2xx/5xx.
Новый эндпоинт с ресурсом автоматически попадает в матрицу; для него нужен генератор id в RESOURCE_IDS."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from tests.conftest import BARRIER_A1_IN, CAM_A1_IN, LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer
from tests.support.billing_fixtures import make_obligation
from tests.support.camera_fixtures import unique_plate
from tests.support.parking_fixtures import plate

ROOT = Path(__file__).resolve().parents[3]
SPEC = json.loads((ROOT / "docs" / "openapi.json").read_text())
ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
SKIP_PATHS = {
    "/api/v1/payments/webhook/{provider}",  # публичный колбэк провайдера: подпись, не JWT
    "/api/v1/signup/{signup_id}/verify",  # публичная заявка на регистрацию: ещё нет клиента
    "/api/v1/join/{token}",  # публичная ссылка приглашения: клиент определяется самим токеном
    "/api/v1/hooks/inbox/{token}",  # публичный тестовый приёмник вебхуков: клиент определяется самим токеном
    "/api/v1/platform/tenants/{tenant_id}",  # владелец платформы адресует клиента явно (tenants:manage), не ресурс клиента
}
# доменная валидация тела идёт раньше поиска ресурса: тело должно быть осмысленным, иначе 422 маскирует проверку
BODY_OVERRIDES: dict[str, dict[str, Any]] = {
    "ChangeRoleRequest": {"role": "VIEWER"},
    "SaveLayoutRequest": {"base_version": 0, "document": {"width": 1200, "height": 800}},
    "ResetPasswordRequest": {"password": "correct-horse-battery-staple-2", "reason": "matrix"},
    "UpdateUserRequest": {"display_name": "Имя", "reason": "matrix"},
    "ManualEntryRequest": {"plate": "01A123AA", "reason": "matrix"},
    "CheckoutRequest": {"provider": "PAYME"},
    "ResolveRequest": {"status": "RESOLVED"},
    "CreatePlanRequest": {"code": "PROBE1"},
    "PublishVersionRequest": {
        "rules": {
            "grace_minutes": 0,
            "first_hour_price": 1,
            "price_per_hour": 1,
            "rounding_mode": "UP",
            "rounding_step_minutes": 60,
        },
        "valid_from": "2030-01-01T00:00:00+00:00",
    },
}


@pytest.fixture
async def resource_ids(
    client: httpx.AsyncClient,
    container: Container,
    owner: asyncpg.Connection,
    users: dict[Role, Principal],
    clean_lot: None,
) -> dict[str, str]:
    """Один живой ресурс tenant A на каждый path-параметр OpenAPI."""
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    rule = await client.post(
        "/api/v1/access-rules",
        json={"plate": unique_plate("01"), "kind": "BLACKLIST", "reason": "matrix"},
        headers=headers,
    )
    assert rule.status_code == 201, rule.text
    obligation = await make_obligation(container, owner, plate(f"01M{uuid4().int % 900 + 100}MM"))
    issued = await container.devices.issue_key(ADMIN, CAM_A1_IN, label="matrix", expires_in=None, reason="matrix test")
    shift = await container.cashier.current_shift(ADMIN) or await container.cashier.open_shift(
        ADMIN, LOT_A1, opening_float=0, notes="matrix"
    )
    plans = await container.tariffs.list_plans(ADMIN, LOT_A1)
    run_id, disc_id = uuid4(), uuid4()
    async with __import__("parking.platform.db.tenant", fromlist=["tenant_transaction"]).tenant_transaction(
        owner, TENANT_A
    ):
        await owner.execute(
            "INSERT INTO payment_reconciliation (id, tenant_id, provider, period_start, period_end) VALUES ($1, $2, 'PAYME', $3, $3)",
            run_id,
            TENANT_A,
            date.today(),
        )
        await owner.execute(
            "INSERT INTO reconciliation_discrepancy (id, tenant_id, reconciliation_id, kind, payment_id, local_amount) VALUES ($1, $2, $3, 'AMOUNT_MISMATCH', $4, 1)",
            disc_id,
            TENANT_A,
            run_id,
            obligation.payment_id,
        )
    privacy = await client.post(
        "/api/v1/privacy/requests",
        json={"plate": unique_plate("01"), "legal_basis": "SUBJECT_REQUEST", "reason": "matrix"},
        headers=headers,
    )
    assert privacy.status_code == 201, privacy.text
    invite = await client.post(
        "/api/v1/invites",
        json={
            "display_name": "Matrix",
            "role": "VIEWER",
            "email": f"matrix.{uuid4().hex[:8]}@alpha-parking.uz",
            "reason": "matrix",
        },
        headers=headers,
    )
    assert invite.status_code == 201, invite.text
    opened = await client.post(f"/api/v1/barriers/{BARRIER_A1_IN}/open", json={"reason": "matrix"}, headers=headers)
    assert opened.status_code == 202, opened.text
    api_key = await container.api_keys.create(
        ADMIN, name="matrix", scopes=["sessions:read"], lot_scope=frozenset(), expires_in_days=None, reason="matrix"
    )
    hook = await container.outgoing_webhooks.create(
        ADMIN, url="https://hooks.example.com/matrix", events=["session.closed"], description=None, reason="matrix"
    )
    await container.outgoing_webhooks.enqueue(
        TENANT_A, "parking.session.closed.v1", uuid4(), datetime.now(tz=UTC), {"session_id": str(uuid4())}
    )
    # выключенный адрес: диспетчер других тестов не будет слать на внешний хост
    await container.outgoing_webhooks.update(ADMIN, hook.endpoint.id, active=False, reason="matrix")
    delivery = (await container.outgoing_webhooks.deliveries(ADMIN, endpoint_id=hook.endpoint.id, limit=1))[0]
    await container.redis.delete(f"inbox:create:{TENANT_A}")  # фикстура на каждый путь: квота 10/ч (A-98) не мешает
    inbox = await client.post("/api/v1/integrations/test-inbox", headers=headers)
    assert inbox.status_code == 200, inbox.text
    return {
        "command_id": opened.json()["command_id"],
        "api_key_id": str(api_key.key.id),
        "endpoint_id": str(hook.endpoint.id),
        "delivery_id": str(delivery.id),
        "token": inbox.json()["token"],
        "version": "1",
        "invite_id": invite.json()["invite"]["id"],
        "rule_id": rule.json()["id"],
        "request_id": privacy.json()["id"],
        "barrier_id": str(BARRIER_A1_IN),
        "key_id": str(issued.key.id),
        "camera_id": str(CAM_A1_IN),
        "lot_id": str(LOT_A1),
        "payment_id": str(obligation.payment_id),
        "discrepancy_id": str(disc_id),
        "run_id": str(run_id),
        "session_id": str(obligation.session_id),
        "shift_id": str(shift.id),
        "plan_id": str(plans[0].id),
        "user_id": str(users[Role.VIEWER].user_id),
    }


def _example(schema: dict[str, Any], name: str = "") -> Any:  # noqa: PLR0911, PLR0912 — таблица типов JSON-схемы
    """Минимально валидное значение по JSON-схеме: ровно то, что нужно, чтобы дойти до проверки tenant'а."""
    if "$ref" in schema:
        return _example(SPEC["components"]["schemas"][schema["$ref"].split("/")[-1]], name)
    if "anyOf" in schema:
        return _example(next(s for s in schema["anyOf"] if s.get("type") != "null"), name)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _example(prop, key)
            for key, prop in schema.get("properties", {}).items()
            if key in schema.get("required", [])
        }
    if kind == "string":
        if schema.get("format") == "uuid":
            return str(uuid4())
        if schema.get("format") == "date-time":
            return datetime.now(tz=UTC).isoformat()
        if name == "plate":
            return "01A123BC"
        if name in {"role"}:
            return "VIEWER"
        return ("cross-tenant probe " * 3)[: max(schema.get("minLength", 3), 20)]
    if kind == "integer":
        return max(schema.get("minimum", 1), 1)
    if kind == "number":
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    return None


def _requests() -> list[tuple[str, str, dict[str, Any] | None]]:
    out = []
    for path, ops in SPEC["paths"].items():
        if "{" not in path or path in SKIP_PATHS:
            continue
        for method, op in ops.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            body_schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            body = None
            if body_schema:
                body = _example(body_schema)
                body.update(BODY_OVERRIDES.get(body_schema.get("$ref", "").split("/")[-1], {}))
            out.append((method, path, body))
    return out


REQUESTS = _requests()


@pytest.mark.parametrize(("method", "path", "body"), REQUESTS, ids=[f"{m.upper()} {p}" for m, p, _ in REQUESTS])
async def test_tenant_b_cannot_touch_tenant_a_resource(
    client: httpx.AsyncClient,
    tenant_b_admin: Principal,
    resource_ids: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    url = path
    for name, value in resource_ids.items():
        url = url.replace("{" + name + "}", value)
    assert "{" not in url, f"нет генератора ресурса для {path}: добавьте в RESOURCE_IDS"
    headers = await bearer(client, tenant_b_admin)
    resp = await client.request(method.upper(), url, headers=headers, json=body)
    assert resp.status_code in {403, 404}, f"{method.upper()} {path} → {resp.status_code}: {resp.text[:200]}"
    if resp.status_code == 422:  # pragma: no cover — диагностика генератора тел
        pytest.fail(resp.text)


async def test_every_resource_path_is_covered_by_matrix() -> None:
    covered = {p for _m, p, _b in REQUESTS}
    expected = {p for p in SPEC["paths"] if "{" in p} - SKIP_PATHS
    assert covered == expected


async def test_tenant_a_can_read_its_own_resources(
    client: httpx.AsyncClient, users: dict[Role, Principal], resource_ids: dict[str, str]
) -> None:
    """Контроль: те же id для владельца — 200 (иначе 404 в матрице ничего не доказывает)."""
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    for path in (
        "/api/v1/sessions/{session_id}",
        "/api/v1/payments/{payment_id}",
        "/api/v1/cameras/{camera_id}",
        "/api/v1/lots/{lot_id}",
        "/api/v1/users/{user_id}",
        "/api/v1/reconciliations/{run_id}",
    ):
        url = path
        for name, value in resource_ids.items():
            url = url.replace("{" + name + "}", value)
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, (url, resp.text[:200])
