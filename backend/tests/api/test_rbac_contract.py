"""Контрактный RBAC-тест: таблица (endpoint, role) → status строится из OpenAPI (x-permission).

Новый эндпоинт без x-permission/x-public или без sample-запроса здесь — падение теста: добавить эндпоинт без RBAC-теста нельзя.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from parking.modules.identity.application.contracts import Permission, Role, permissions_for
from parking.runtime.container import Container
from tests.conftest import BARRIER_A1_IN, CAM_A1_IN, LOT_A1, TENANT_A, USER_A_ADMIN
from tests.support.api_fixtures import Principal, bearer, ensure_user

# (method, path) → как вызвать с валидным запросом (path-параметры и тело)
SAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/api/v1/auth/me"): {},
    ("GET", "/api/v1/users"): {},
    ("POST", "/api/v1/users"): {
        "json": {
            "email": "rbac-{n}@alpha-parking.uz",
            "password": "correct-horse-battery-staple",
            "role": "VIEWER",
            "display_name": "RBAC",
            "reason": "rbac contract test",
        }
    },
    ("GET", "/api/v1/users/{user_id}"): {"path": {"user_id": "{self_id}"}},
    ("PATCH", "/api/v1/users/{user_id}/role"): {
        "path": {"user_id": "{other_id}"},
        "json": {"role": "VIEWER", "reason": "rbac contract test"},
    },
    ("PATCH", "/api/v1/users/{user_id}"): {
        "path": {"user_id": "{self_id}"},
        "json": {"display_name": "Имя", "reason": "rbac test"},
    },
    ("POST", "/api/v1/users/{user_id}/password"): {  # не self: сброс пароля отзывает токены актора
        "path": {"user_id": "{other_id}"},
        "json": {"password": "correct-horse-battery-staple-2", "reason": "rbac test"},
    },
    ("POST", "/api/v1/users/{user_id}/deactivate"): {
        "path": {"user_id": "{other_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/lots"): {},
    ("GET", "/api/v1/lots/{lot_id}"): {"path": {"lot_id": str(LOT_A1)}},
    ("POST", "/api/v1/lots"): {
        "json": {"code": "RB-{n}", "name": "rbac", "capacity": 10, "reason": "rbac contract test"}
    },
    ("PATCH", "/api/v1/lots/{lot_id}"): {
        "path": {"lot_id": str(LOT_A1)},
        "json": {"name": "ТЦ Mega, P1", "reason": "rbac contract test"},
    },
    ("GET", "/api/v1/tenants/current"): {},
    ("GET", "/api/v1/lots/{lot_id}/tariff-plans"): {"path": {"lot_id": str(LOT_A1)}},
    ("POST", "/api/v1/lots/{lot_id}/tariff-plans"): {
        "path": {"lot_id": str(LOT_A1)},
        "json": {"code": "RB-{n}", "name": "rbac", "reason": "rbac contract test"},
    },
    ("GET", "/api/v1/tariff-plans/{plan_id}/versions"): {"path": {"plan_id": "{plan_id}"}},
    ("POST", "/api/v1/tariff-plans/{plan_id}/versions"): {
        "path": {"plan_id": "{plan_id}"},
        "json": {
            "rules": {
                "grace_minutes": 0,
                "first_hour_price": 1000,
                "price_per_hour": 1000,
                "rounding_mode": "UP",
                "rounding_step_minutes": 60,
            },
            "valid_from": "2030-01-01T00:00:00+05:00",
            "reason": "rbac contract test",
        },
    },
    ("POST", "/api/v1/vehicles/lookup"): {"json": {"plate": "01A123AA"}},
    ("POST", "/api/v1/vehicles"): {"json": {"plate": "01A123AA", "make": "Chevrolet", "reason": "rbac contract test"}},
    ("POST", "/api/v1/sessions/{session_id}/vehicle"): {
        "path": {"session_id": "{session_id}"},
        "json": {"make": "Chevrolet", "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/lots/{lot_id}/manual-entry"): {
        "path": {"lot_id": str(LOT_A1)},
        "json": {"plate": "01R{n}", "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/auth/password"): {  # неверный текущий пароль: общие фикстуры не меняются
        "json": {"current_password": "not-the-password", "new_password": "rbac-new-password-1"}
    },
    ("GET", "/api/v1/invites"): {},
    ("POST", "/api/v1/invites"): {
        "json": {"display_name": "RBAC", "role": "VIEWER", "email": "rbac-inv-{n}@alpha-parking.uz", "reason": "rbac"}
    },
    ("POST", "/api/v1/invites/{invite_id}/revoke"): {
        "path": {"invite_id": "00000000-0000-4000-8000-00000000fe01"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/barriers/{barrier_id}/probe"): {  # несуществующий шлагбаум: 404 без сетевого вызова
        "path": {"barrier_id": "00000000-0000-4000-8000-00000000fe02"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/barrier-commands/{command_id}"): {"path": {"command_id": "00000000-0000-4000-8000-00000000fe03"}},
    ("GET", "/api/v1/lots/{lot_id}/layout"): {"path": {"lot_id": str(LOT_A1)}},
    ("PUT", "/api/v1/lots/{lot_id}/layout"): {  # base_version 0: у схемы A1 уже есть версии — 409, прав хватило
        "path": {"lot_id": str(LOT_A1)},
        "json": {"base_version": 0, "document": {"width": 1200, "height": 800}},
    },
    ("GET", "/api/v1/lots/{lot_id}/layout/versions"): {"path": {"lot_id": str(LOT_A1)}},
    ("GET", "/api/v1/lots/{lot_id}/layout/versions/{version}"): {"path": {"lot_id": str(LOT_A1), "version": "1"}},
    ("GET", "/api/v1/lots/{lot_id}/map"): {"path": {"lot_id": str(LOT_A1)}},
    ("GET", "/api/v1/subscription"): {},
    ("POST", "/api/v1/subscription/plan"): {"json": {"plan_code": "NETWORK", "reason": "rbac contract test"}},
    ("GET", "/api/v1/platform/tenants"): {},
    ("PATCH", "/api/v1/platform/tenants/{tenant_id}"): {
        "path": {"tenant_id": str(TENANT_A)},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/access-rules"): {},
    ("POST", "/api/v1/access-rules"): {"json": {"plate": "01R{n}", "kind": "VIP", "reason": "rbac contract test"}},
    ("POST", "/api/v1/access-rules/{rule_id}/revoke"): {
        "path": {"rule_id": "{rule_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/subscriptions"): {
        "json": {
            "plate": "01S{n}",
            "plan_code": "MONTH",
            "starts_at": "2026-09-01T00:00:00+05:00",
            "ends_at": "2026-10-01T00:00:00+05:00",
            "price_minor": 100,
            "reason": "rbac contract test",
        }
    },
    ("GET", "/api/v1/access/resolve"): {"query": {"lot_id": str(LOT_A1), "plate": "01A123AA"}},
    ("GET", "/api/v1/lots/{lot_id}/sessions"): {"path": {"lot_id": str(LOT_A1)}},
    ("GET", "/api/v1/lots/{lot_id}/occupancy"): {"path": {"lot_id": str(LOT_A1)}},
    ("GET", "/api/v1/sessions/search"): {"query": {"plate": "01A123AA"}},
    ("GET", "/api/v1/sessions/review-queue"): {},
    ("GET", "/api/v1/sessions/{session_id}"): {"path": {"session_id": "{session_id}"}},
    ("GET", "/api/v1/sessions/{session_id}/saga"): {"path": {"session_id": "{session_id}"}},
    ("POST", "/api/v1/sessions/{session_id}/correct-plate"): {
        "path": {"session_id": "{session_id}"},
        "json": {"plate": "01C{n}", "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/sessions/{session_id}/confirm-entry"): {
        "path": {"session_id": "{session_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/sessions/{session_id}/open-exit"): {
        "path": {"session_id": "{session_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/sessions/{session_id}/close"): {
        "path": {"session_id": "{session_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/lots/{lot_id}/sessions/close-stale"): {
        "path": {"lot_id": str(LOT_A1)},
        "json": {"older_than_hours": 8760, "dry_run": True},  # только подсчёт: общий стенд не трогаем
    },
    ("POST", "/api/v1/sessions/{session_id}/orphan"): {
        "path": {"session_id": "{session_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/sessions/{session_id}/images"): {"path": {"session_id": "{session_id}"}},
    ("GET", "/api/v1/sessions/{session_id}/videos"): {"path": {"session_id": "{session_id}"}},
    ("GET", "/api/v1/cameras"): {},
    ("GET", "/api/v1/cameras/{camera_id}"): {"path": {"camera_id": str(CAM_A1_IN)}},
    ("POST", "/api/v1/cameras"): {
        "json": {
            "lot_id": str(LOT_A1),
            "code": "RB{n}",
            "name": "rbac",
            "direction": "IN",
            "reason": "rbac contract test",
        }
    },
    ("PATCH", "/api/v1/cameras/{camera_id}"): {
        "path": {"camera_id": str(CAM_A1_IN)},
        "json": {"name": "Въезд 1", "reason": "rbac contract test"},
    },
    ("GET", "/api/v1/cameras/{camera_id}/keys"): {"path": {"camera_id": str(CAM_A1_IN)}},
    ("POST", "/api/v1/cameras/{camera_id}/keys"): {
        "path": {"camera_id": str(CAM_A1_IN)},
        "json": {"label": "rbac", "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/camera-keys/{key_id}/rotate"): {
        "path": {"key_id": "{key_id}"},
        "json": {"grace_hours": 1, "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/camera-keys/{key_id}/revoke"): {
        "path": {"key_id": "{key_id}"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/barriers"): {},
    ("GET", "/api/v1/barriers/{barrier_id}"): {"path": {"barrier_id": str(BARRIER_A1_IN)}},
    ("POST", "/api/v1/barriers"): {
        "json": {
            "lot_id": str(LOT_A1),
            "name": "rbac",
            "driver": "MODBUS_TCP",
            "endpoint": "10.10.1.99:502",
            "reason": "rbac contract test",
        }
    },
    ("PATCH", "/api/v1/barriers/{barrier_id}"): {
        "path": {"barrier_id": str(BARRIER_A1_IN)},
        "json": {"name": "Шлагбаум въезд 1", "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/barriers/{barrier_id}/open"): {
        "path": {"barrier_id": str(BARRIER_A1_IN)},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/admin/dlq"): {},
    ("POST", "/api/v1/privacy/requests"): {
        "json": {"plate": "01P{n}", "legal_basis": "SUBJECT_REQUEST", "reason": "rbac contract test"}
    },
    ("GET", "/api/v1/privacy/requests"): {},
    ("GET", "/api/v1/privacy/requests/{request_id}"): {"path": {"request_id": str(uuid4())}},
    ("POST", "/api/v1/privacy/requests/{request_id}/verify"): {
        "path": {"request_id": str(uuid4())},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/privacy/requests/{request_id}/process"): {"path": {"request_id": str(uuid4())}},
    ("POST", "/api/v1/privacy/requests/{request_id}/reject"): {
        "path": {"request_id": str(uuid4())},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/admin/dlq/replay"): {"json": {"stream": "anpr", "dry_run": True, "reason": "rbac contract test"}},
    ("POST", "/api/v1/shifts"): {"json": {"lot_id": str(LOT_A1), "opening_float": 0}},
    ("GET", "/api/v1/shifts"): {},
    ("GET", "/api/v1/shifts/current"): {},
    ("POST", "/api/v1/shifts/{shift_id}/close"): {"path": {"shift_id": "{shift_id}"}, "json": {"counted_cash": 0}},
    ("GET", "/api/v1/sessions/{session_id}/payments"): {"path": {"session_id": "{session_id}"}},
    ("GET", "/api/v1/sessions/{session_id}/quote"): {"path": {"session_id": "{session_id}"}},
    ("POST", "/api/v1/sessions/{session_id}/payments"): {"path": {"session_id": "{session_id}"}, "json": {}},
    ("GET", "/api/v1/payments/{payment_id}"): {"path": {"payment_id": "{payment_id}"}},
    ("POST", "/api/v1/payments/{payment_id}/confirm-cash"): {"path": {"payment_id": "{payment_id}"}, "json": {}},
    ("POST", "/api/v1/payments/{payment_id}/checkout"): {
        "path": {"payment_id": "{payment_id}"},
        "json": {"provider": "PAYME"},
    },
    ("POST", "/api/v1/payments/{payment_id}/refunds"): {
        "path": {"payment_id": "{payment_id}"},
        "json": {"amount_minor": 100, "reason": "rbac contract test"},
    },
    ("GET", "/api/v1/payments/{payment_id}/refunds"): {"path": {"payment_id": "{payment_id}"}},
    ("POST", "/api/v1/reconciliations"): {
        "json": {"provider": "PAYME", "period_start": "2026-09-01", "period_end": "2026-09-01"}
    },
    ("GET", "/api/v1/reconciliations/{run_id}"): {"path": {"run_id": "{run_id}"}},
    ("GET", "/api/v1/reconciliation-discrepancies"): {},
    ("POST", "/api/v1/reconciliation-discrepancies/{discrepancy_id}/resolve"): {
        "path": {"discrepancy_id": "{discrepancy_id}"},
        "json": {"status": "RESOLVED", "resolution": "rbac contract test"},
    },
    ("POST", "/api/v1/tariff/quote"): {
        "json": {"lot_id": str(LOT_A1), "entry": "2026-09-14T08:00:00+05:00", "exit": "2026-09-14T10:00:00+05:00"}
    },
    ("POST", "/api/v1/tariff/preview"): {
        "json": {
            "lot_id": str(LOT_A1),
            "rules": {
                "grace_minutes": 15,
                "first_hour_price": 500_000,
                "price_per_hour": 300_000,
                "rounding_mode": "UP",
                "rounding_step_minutes": 15,
            },
        }
    },
    ("GET", "/api/v1/integrations/catalog"): {},
    ("GET", "/api/v1/api-keys"): {},
    ("POST", "/api/v1/api-keys"): {"json": {"name": "rbac", "scopes": ["sessions:read"], "reason": "rbac contract"}},
    ("POST", "/api/v1/api-keys/{api_key_id}/revoke"): {
        "path": {"api_key_id": "00000000-0000-4000-8000-00000000fe04"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/webhooks"): {},
    ("POST", "/api/v1/webhooks"): {  # http + приватный адрес — 422 в prod-настройках, 201 в dev: права хватило
        "json": {"url": "https://hooks.example.com/rbac", "events": ["session.closed"], "reason": "rbac contract"}
    },
    ("PATCH", "/api/v1/webhooks/{endpoint_id}"): {
        "path": {"endpoint_id": "00000000-0000-4000-8000-00000000fe05"},
        "json": {"active": False, "reason": "rbac contract test"},
    },
    ("POST", "/api/v1/webhooks/{endpoint_id}/delete"): {
        "path": {"endpoint_id": "00000000-0000-4000-8000-00000000fe05"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/webhooks/{endpoint_id}/rotate-secret"): {
        "path": {"endpoint_id": "00000000-0000-4000-8000-00000000fe05"},
        "json": {"reason": "rbac contract test"},
    },
    ("POST", "/api/v1/webhooks/{endpoint_id}/test"): {"path": {"endpoint_id": "00000000-0000-4000-8000-00000000fe05"}},
    ("GET", "/api/v1/webhook-deliveries"): {},
    ("POST", "/api/v1/webhook-deliveries/{delivery_id}/redeliver"): {
        "path": {"delivery_id": "00000000-0000-4000-8000-00000000fe06"},
        "json": {"reason": "rbac contract test"},
    },
    ("GET", "/api/v1/integrations/journal"): {},
    ("POST", "/api/v1/integrations/sandbox"): {
        "json": {"camera_id": str(CAM_A1_IN), "vendor": "generic", "payload": "{}"}
    },
    ("POST", "/api/v1/integrations/test-inbox"): {},
    ("GET", "/api/v1/integrations/test-inbox/{token}"): {"path": {"token": "rbac-contract-missing-token"}},
    ("GET", "/api/v1/integrations/openapi"): {},
    ("GET", "/api/v1/dashboard/owner"): {},
    ("GET", "/api/v1/dashboard/platform"): {},
}


def _operations(app: FastAPI) -> list[tuple[str, str, dict[str, Any]]]:
    spec = app.openapi()
    ops = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            ops.append((method.upper(), path, op))
    return ops


def test_every_operation_declares_permission_or_public(app: FastAPI) -> None:
    missing = [f"{m} {p}" for m, p, op in _operations(app) if "x-permission" not in op and not op.get("x-public")]
    assert missing == [], f"эндпоинты без x-permission/x-public: {missing}"
    without_sample = [f"{m} {p}" for m, p, op in _operations(app) if "x-permission" in op and (m, p) not in SAMPLES]
    assert without_sample == [], f"эндпоинты без sample в RBAC-тесте: {without_sample}"


@pytest.mark.parametrize("role", list(Role))
async def test_role_matrix(
    app: FastAPI, client: httpx.AsyncClient, container: Container, users: dict[Role, Principal], role: Role
) -> None:
    principal = users[role]
    headers = await bearer(client, principal)
    # одноразовый пользователь-мишень: смена роли/деактивация не должны ломать общие фикстуры
    other = await ensure_user(
        container,
        TENANT_A,
        USER_A_ADMIN,
        Role.OPERATOR,
        f"rbac-target-{role.value.lower()}-{uuid4().hex[:6]}@alpha-parking.uz",
    )
    counter = 0
    for method, path, op in _operations(app):
        required = op.get("x-permission")
        if required is None:
            continue
        counter += 1
        sample = SAMPLES[(method, path)]
        url = path
        for name, value in sample.get("path", {}).items():
            url = url.replace(
                "{" + name + "}",
                value.replace("{self_id}", str(principal.user_id)).replace("{other_id}", str(other.user_id)),
            )
        json_body = sample.get("json")
        if json_body is not None:
            json_body = {
                k: (v.replace("{n}", f"{role.value.lower()}{counter}{uuid4().hex[:6]}") if isinstance(v, str) else v)
                for k, v in json_body.items()
            }
        response = await client.request(method, url, headers=headers, json=json_body)
        allowed = all(Permission(p) in permissions_for(role) for p in required)
        if allowed:
            assert response.status_code not in (401, 403), (
                f"{role} {method} {path}: {response.status_code} {response.text}"
            )
        else:
            assert response.status_code == 403, (
                f"{role} {method} {path}: expected 403, got {response.status_code} {response.text}"
            )
            assert response.json()["error"]["code"] == "FORBIDDEN"
    assert counter >= 10
