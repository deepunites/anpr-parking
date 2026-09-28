"""v2 этап 3 «Продажа»: самостоятельная регистрация (код, попытки, занятый email), пробный период, лимиты тарифа
(ограничивают только настройку, не проезд), смена тарифа, управление клиентами владельцем платформы,
подхват ключей нового клиента другим процессом."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from parking.platform.config import DevSettings
from parking.platform.db.engine import Database
from parking.runtime.container import Container, build_container
from tests.support.api_fixtures import Principal, bearer, login

SIGNUP_PASSWORD = "signup-password-2026"


def _ip() -> dict[str, str]:
    """Свой адрес на каждый тест: лимит заявок с IP живёт в Redis между прогонами."""
    return {"X-Forwarded-For": f"10.{secrets.randbelow(250)}.{secrets.randbelow(250)}.{secrets.randbelow(250) + 1}"}


def _signup_body(email: str, plan: str = "START") -> dict[str, object]:
    return {
        "company": "ООО Тест Паркинг",
        "name": "Азиз Каримов",
        "email": email,
        "phone": "+998 90 123-45-67",
        "password": SIGNUP_PASSWORD,
        "plan_code": plan,
        "locale": "ru",
        "accept_terms": True,
    }


async def _signed_up(client: httpx.AsyncClient, plan: str = "START") -> tuple[str, dict[str, str]]:
    email = f"owner.{uuid4().hex[:10]}@new-client.uz"
    started = await client.post("/api/v1/signup", json=_signup_body(email, plan), headers=_ip())
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["dev_code"] and len(body["dev_code"]) == 6
    done = await client.post(f"/api/v1/signup/{body['signup_id']}/verify", json={"code": body["dev_code"]})
    assert done.status_code == 200, done.text
    token = await login(client, email, SIGNUP_PASSWORD)
    assert token.status_code == 200, token.text
    return done.json()["tenant_id"], {"Authorization": f"Bearer {token.json()['access_token']}"}


async def _lot(client: httpx.AsyncClient, admin: dict[str, str], capacity: int = 50) -> httpx.Response:
    return await client.post(
        "/api/v1/lots",
        headers=admin,
        json={"code": f"L{uuid4().hex[:6]}", "name": "Площадка", "capacity": capacity, "reason": "запуск"},
    )


async def test_plans_are_public(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/plans")
    assert resp.status_code == 200, resp.text
    plans = {p["code"]: p for p in resp.json()}
    assert {"START", "BUSINESS", "NETWORK"} <= set(plans)
    assert plans["START"]["max_lots"] == 1 and plans["START"]["trial_days"] == 14
    assert plans["NETWORK"]["max_lots"] is None


async def test_signup_creates_trial_tenant_with_admin_and_working_keys(client: httpx.AsyncClient) -> None:
    tenant_id, admin = await _signed_up(client)
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert me["tenant_id"] == tenant_id
    assert me["role"] == "TENANT_ADMIN"
    assert me["tenant_status"] == "TRIAL" and me["plan_code"] == "START"
    assert me["trial_days_left"] == 14 and me["blocked_reason"] is None
    assert me["tenant_name"] == "ООО Тест Паркинг"

    lot = await _lot(client, admin)
    assert lot.status_code == 201, lot.text
    # ключи шифрования номеров нового клиента уже работают: ручной въезд шифрует номер его ключом
    entry = await client.post(
        f"/api/v1/lots/{lot.json()['id']}/manual-entry",
        headers=admin,
        json={"plate": "01A123BC", "reason": "камера не распознала"},
    )
    assert entry.status_code == 201, entry.text
    lookup = await client.post("/api/v1/vehicles/lookup", headers=admin, json={"plate": "01A123BC"})
    assert lookup.json()["active_session_id"] == entry.json()["id"]


async def test_wrong_code_counts_attempts_then_request_dies(client: httpx.AsyncClient) -> None:
    started = await client.post(
        "/api/v1/signup", json=_signup_body(f"x.{uuid4().hex[:8]}@new-client.uz"), headers=_ip()
    )
    signup_id = started.json()["signup_id"]
    wrong = "000000" if started.json()["dev_code"] != "000000" else "111111"
    first = await client.post(f"/api/v1/signup/{signup_id}/verify", json={"code": wrong})
    assert first.status_code == 422
    assert first.json()["error"]["code"] == "WRONG_CODE" and first.json()["error"]["details"]["attempts_left"] == 4
    for _ in range(4):
        await client.post(f"/api/v1/signup/{signup_id}/verify", json={"code": wrong})
    blocked = await client.post(f"/api/v1/signup/{signup_id}/verify", json={"code": started.json()["dev_code"]})
    assert blocked.status_code in (404, 429)


async def test_signup_rejects_taken_email_and_missing_consent(
    client: httpx.AsyncClient, users: dict[Role, Principal]
) -> None:
    taken = await client.post("/api/v1/signup", json=_signup_body(users[Role.TENANT_ADMIN].email), headers=_ip())
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "EMAIL_TAKEN"
    no_terms = {**_signup_body(f"y.{uuid4().hex[:8]}@new-client.uz"), "accept_terms": False}
    resp = await client.post("/api/v1/signup", json=no_terms, headers=_ip())
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "TERMS_NOT_ACCEPTED"
    unknown = await client.post(
        "/api/v1/signup", json=_signup_body(f"z.{uuid4().hex[:8]}@new-client.uz", plan="GOLD"), headers=_ip()
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "UNKNOWN_PLAN"


async def test_signup_rate_limited_per_ip(client: httpx.AsyncClient, api_settings: DevSettings) -> None:
    ip = _ip()
    codes = []
    for _ in range(api_settings.signup_rate_per_hour + 1):
        resp = await client.post("/api/v1/signup", json=_signup_body(f"r.{uuid4().hex[:8]}@new-client.uz"), headers=ip)
        codes.append(resp.status_code)
    assert codes[:-1] == [201] * api_settings.signup_rate_per_hour
    assert codes[-1] == 429


async def test_plan_limits_restrict_configuration(client: httpx.AsyncClient) -> None:
    _, admin = await _signed_up(client, plan="START")
    lot = await _lot(client, admin, capacity=80)
    assert lot.status_code == 201
    second = await _lot(client, admin)
    assert second.status_code == 409 and second.json()["error"]["code"] == "PLAN_LIMIT_REACHED"
    assert second.json()["error"]["details"]["resource"] == "lots"
    too_big = await client.patch(
        f"/api/v1/lots/{lot.json()['id']}", headers=admin, json={"capacity": 150, "reason": "расширение"}
    )
    assert too_big.status_code == 409 and too_big.json()["error"]["details"]["resource"] == "places"

    for i in range(2):
        cam = await client.post(
            "/api/v1/cameras",
            headers=admin,
            json={
                "lot_id": lot.json()["id"],
                "code": f"C{i}",
                "name": f"Камера {i}",
                "direction": "IN",
                "reason": "монтаж",
            },
        )
        assert cam.status_code == 201, cam.text
    third = await client.post(
        "/api/v1/cameras",
        headers=admin,
        json={"lot_id": lot.json()["id"], "code": "C3", "name": "Камера 3", "direction": "OUT", "reason": "монтаж"},
    )
    assert third.status_code == 409 and third.json()["error"]["details"]["resource"] == "cameras"

    sub = (await client.get("/api/v1/subscription", headers=admin)).json()
    assert sub["usage"] == {"lots": 1, "places": 80, "cameras": 2}
    assert sub["status"] == "TRIAL" and sub["trial_days_left"] == 14

    up = await client.post("/api/v1/subscription/plan", headers=admin, json={"plan_code": "BUSINESS", "reason": "рост"})
    assert up.status_code == 204, up.text
    assert (await _lot(client, admin)).status_code == 201
    down = await client.post(
        "/api/v1/subscription/plan", headers=admin, json={"plan_code": "START", "reason": "эконом"}
    )
    assert down.status_code == 409 and down.json()["error"]["code"] == "PLAN_TOO_SMALL"


async def test_platform_owner_manages_tenants(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    tenant_id, admin = await _signed_up(client)
    owner = await bearer(client, users[Role.PLATFORM_OWNER])
    listed = await client.get("/api/v1/platform/tenants", headers=owner)
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json() if r["id"] == tenant_id)
    assert row["status"] == "TRIAL" and row["users"] == 1 and row["contact_phone"]
    # клиент не видит список клиентов платформы
    assert (await client.get("/api/v1/platform/tenants", headers=admin)).status_code == 403

    expired = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=owner,
        json={"trial_ends_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(), "reason": "проверка окончания"},
    )
    assert expired.status_code == 204, expired.text
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert me["blocked_reason"] == "TRIAL_EXPIRED" and me["trial_days_left"] == 0
    blocked = await _lot(client, admin)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "SUBSCRIPTION_INACTIVE"

    paid = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        headers=owner,
        json={
            "status": "ACTIVE",
            "paid_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "reason": "оплата наличными, квитанция 17",
        },
    )
    assert paid.status_code == 204
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert me["tenant_status"] == "ACTIVE" and me["blocked_reason"] is None and me["trial_days_left"] is None
    assert (await _lot(client, admin)).status_code == 201

    suspended = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}", headers=owner, json={"status": "SUSPENDED", "reason": "неоплата"}
    )
    assert suspended.status_code == 204
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    assert me["blocked_reason"] == "SUSPENDED"


async def test_new_tenant_keys_reach_other_process(
    client: httpx.AsyncClient, api_settings: DevSettings, database: Database, container: Container
) -> None:
    tenant_id, _ = await _signed_up(client)
    other = build_container(api_settings, database)
    with pytest.raises(KeyError):
        other.layered_keys.keys_for(UUID(tenant_id))
    assert await other.refresh_tenant_keys() >= 1
    assert other.layered_keys.keys_for(UUID(tenant_id)) == container.layered_keys.keys_for(UUID(tenant_id))


async def test_signup_reports_unavailable_code_channel(
    client: httpx.AsyncClient, container: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 этап 8 (RUNBOOK sms-gateway-down): канал кодов лежит — 503 с понятным кодом, не 500."""

    async def broken(**_: object) -> None:
        raise ConnectionError("sms gateway down")

    monkeypatch.setattr(container.signup._sender, "send", broken)
    resp = await client.post("/api/v1/signup", json=_signup_body(f"s.{uuid4().hex[:8]}@new-client.uz"), headers=_ip())
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
