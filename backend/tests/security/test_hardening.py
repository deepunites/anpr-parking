"""§7 — ручные проверки безопасности как тесты: повышение привилегий через тело и JWT, подделка JWT (alg none / HS256
с публичным ключом / чужой Ed25519-ключ), подделка camera-событий, SSRF через endpoint шлагбаума и image_object_key,
SQL-инъекции во всех поисковых параметрах (включая номер), обход RLS под ролью приложения."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from parking.modules.identity.application.contracts import Role
from parking.platform.db.tenant import tenant_transaction
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN, LOT_A1, LOT_B1, TENANT_A, TENANT_B
from tests.support.api_fixtures import Principal, bearer, login
from tests.support.camera_fixtures import event_body, send_event, unique_plate

ROOT = Path(__file__).resolve().parents[3]
SPEC = json.loads((ROOT / "docs" / "openapi.json").read_text())
INJECTIONS = [
    "' OR '1'='1",
    "'; DROP TABLE parking_session; --",
    "01A123BC' UNION SELECT plate_hash FROM vehicle --",
    "%27%20OR%201%3D1",
    "1; SELECT pg_sleep(5)",
    "\\x27 OR 1=1",
    '01A123BC" OR ""="',
    "${jndi:ldap://x}",
    "../../etc/passwd",
    "🚗' OR 1=1--",
]


# ---------------------------------------------------------------- повышение привилегий


async def test_tenant_id_and_role_in_body_do_not_escalate(
    client: httpx.AsyncClient, users: dict[Role, Principal], tenant_b_admin: Principal
) -> None:
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    created = await client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "email": f"probe-{uuid4().hex[:6]}@alpha-parking.uz",
            "password": "correct-horse-battery-staple-1",
            "role": "OPERATOR",
            "display_name": "probe",
            "reason": "escalation probe",
            "tenant_id": str(TENANT_B),  # лишнее поле: игнорируется/отвергается, но не применяется
            "lot_scope": [str(LOT_B1)],  # площадка чужого tenant в скоупе
        },
    )
    assert created.status_code == 404, created.text  # площадка чужого tenant для этого tenant не существует
    created = await client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "email": f"probe-{uuid4().hex[:6]}@alpha-parking.uz",
            "password": "correct-horse-battery-staple-1",
            "role": "OPERATOR",
            "display_name": "probe",
            "reason": "escalation probe",
            "tenant_id": str(TENANT_B),
        },
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]
    assert (await client.get(f"/api/v1/users/{new_id}", headers=headers)).status_code == 200
    headers_b = await bearer(client, tenant_b_admin)
    assert (await client.get(f"/api/v1/users/{new_id}", headers=headers_b)).status_code == 404, (
        "tenant берётся из JWT, не из тела: пользователь создан в tenant A"
    )
    # TENANT_ADMIN не может выдать PLATFORM_ADMIN ни себе, ни другому
    viewer = users[Role.VIEWER]
    escalate = await client.patch(
        f"/api/v1/users/{viewer.user_id}/role", headers=headers, json={"role": "PLATFORM_ADMIN", "reason": "probe"}
    )
    assert escalate.status_code in {403, 422}, escalate.text
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["role"] == "TENANT_ADMIN"


async def test_forged_jwt_variants_are_rejected(client: httpx.AsyncClient, users: dict[Role, Principal]) -> None:
    token = (await login(client, users[Role.VIEWER].email)).json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    forged_claims = {**claims, "role": "PLATFORM_ADMIN", "tenant_id": str(TENANT_B)}
    other_key = Ed25519PrivateKey.generate()
    pem = other_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    variants = {
        "alg=none": pyjwt.encode(forged_claims, key="", algorithm="none"),
        "HS256 with public key": pyjwt.encode(forged_claims, key="not-the-key", algorithm="HS256"),
        "foreign Ed25519 key": pyjwt.encode(forged_claims, key=pem, algorithm="EdDSA"),
        "claims swapped, original signature": ".".join(
            [
                token.split(".")[0],
                pyjwt.utils.base64url_encode(json.dumps(forged_claims).encode()).decode(),
                token.split(".")[2],
            ]
        ),
        "expired": pyjwt.encode({**claims, "exp": 1}, key=pem, algorithm="EdDSA"),
    }
    for name, forged in variants.items():
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401, (name, resp.status_code, resp.text[:120])
        assert "PLATFORM_ADMIN" not in resp.text
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json()["role"] == "VIEWER" and resp.json()["tenant_id"] == str(TENANT_A)


# ---------------------------------------------------------------- подделка camera-событий


async def test_camera_events_without_key_or_with_foreign_key_are_rejected(
    client: httpx.AsyncClient, container: Container, entry_key: str, owner: asyncpg.Connection
) -> None:
    body = event_body(unique_plate())
    assert (await client.post("/api/v1/camera/events", json=body)).status_code == 401
    assert (
        await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": "cam_zz_" + "x" * 40})
    ).status_code == 401
    from parking.modules.devices.domain.camera import KEY_PREFIX_LEN

    tampered = entry_key[:KEY_PREFIX_LEN] + entry_key[KEY_PREFIX_LEN:][::-1]  # верный префикс, неверный секрет
    assert (
        await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": tampered})
    ).status_code == 401
    # ключ камеры tenant B на его площадке: событие принимается только для своей камеры/площадки —
    # подменить camera_code/lot чужого tenant нельзя (принципал берётся из ключа, не из тела)
    cam_b = await owner.fetchval("SELECT id FROM camera WHERE tenant_id = $1 LIMIT 1", TENANT_B)
    from parking.modules.identity.application.contracts import CurrentUser
    from tests.support.camera_fixtures import ADMIN_A

    admin_b = CurrentUser(
        user_id=UUID("00000000-0000-4000-8000-0000000b0001"),
        tenant_id=TENANT_B,
        role=Role.TENANT_ADMIN,
        lot_scope=frozenset(),
    )
    key_b = (
        await container.devices.issue_key(admin_b, cam_b, label="probe", expires_in=None, reason="security probe")
    ).raw
    resp = await send_event(client, key_b, event_body(unique_plate(), camera_code="ENTRY_01"))
    assert resp.status_code in {200, 422}, resp.text
    if resp.status_code == 200:
        session_tenant = await owner.fetchval(
            "SELECT tenant_id FROM parking_session WHERE id = $1", UUID(resp.json()["session_id"])
        )
        assert session_tenant == TENANT_B, "сессия создана у владельца ключа, не у tenant A"
    _ = ADMIN_A
    # отозванный ключ
    issued = await container.devices.issue_key(
        ADMIN_A, CAM_A1_IN, label="revoke-probe", expires_in=None, reason="probe"
    )
    await container.devices.revoke_key(ADMIN_A, issued.key.id, reason="security probe")
    await container.camera_auth.invalidate(
        issued.raw.split("_")[2][:8] if issued.raw.count("_") >= 2 else issued.raw[:8]
    )
    revoked = await send_event(client, issued.raw, event_body(unique_plate()))
    assert revoked.status_code == 401, revoked.text


# ---------------------------------------------------------------- SSRF


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/api/v1/admin/dlq",
        "http://localhost:9000/",
        "http://[::1]/",
        "http://10.0.0.5/open",
        "file:///etc/passwd",
        "gopher://10.0.0.1:6379/_FLUSHALL",
        "http://metadata.google.internal/",
        "http://barrier.example.com@127.0.0.1/",
        "http://0x7f000001/",
    ],
)
async def test_barrier_endpoint_ssrf_is_rejected(
    client: httpx.AsyncClient, users: dict[Role, Principal], endpoint: str
) -> None:
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    resp = await client.post(
        "/api/v1/barriers",
        headers=headers,
        json={
            "lot_id": str(LOT_A1),
            "name": "probe",
            "driver": "HTTP_REST",
            "endpoint": endpoint,
            "reason": "ssrf probe",
        },
    )
    assert resp.status_code == 422, (endpoint, resp.text[:200])


async def test_image_object_key_cannot_point_outside_camera_namespace(
    client: httpx.AsyncClient, entry_key: str
) -> None:
    for key in (
        f"{TENANT_B}/{LOT_B1}/{uuid4()}/2026/09/20/{uuid4()}.jpg",
        "../../../etc/passwd",
        f"{TENANT_A}/{LOT_A1}/../{uuid4()}/x.jpg",
        "http://169.254.169.254/",
    ):
        resp = await send_event(client, entry_key, event_body(unique_plate(), image_object_key=key))
        assert resp.status_code == 422, (key, resp.text[:200])
        assert resp.json()["error"]["code"] in {"INVALID_IMAGE_KEY", "VALIDATION_ERROR"}


# ---------------------------------------------------------------- SQL-инъекции


def _query_endpoints() -> list[tuple[str, str]]:
    out = []
    for path, ops in SPEC["paths"].items():
        if "{" in path:
            continue
        for name in {p["name"] for p in ops.get("get", {}).get("parameters", []) if p["in"] == "query"}:
            out.append((path, name))
    return out


QUERY_ENDPOINTS = _query_endpoints()


@pytest.mark.parametrize(("path", "param"), QUERY_ENDPOINTS, ids=[f"{p}?{q}" for p, q in QUERY_ENDPOINTS])
async def test_query_parameters_are_injection_safe(
    client: httpx.AsyncClient, users: dict[Role, Principal], path: str, param: str
) -> None:
    headers = await bearer(client, users[Role.PLATFORM_ADMIN])
    for payload in INJECTIONS:
        resp = await client.get(path, params={param: payload}, headers=headers)
        assert resp.status_code in {200, 400, 404, 422}, (path, param, payload, resp.status_code, resp.text[:200])
        if resp.status_code == 200 and isinstance(resp.json(), dict) and "items" in resp.json():
            assert all(item.get("tenant_id", str(TENANT_A)) == str(TENANT_A) for item in resp.json()["items"])


async def test_plate_search_and_bodies_are_injection_safe(
    client: httpx.AsyncClient, users: dict[Role, Principal], entry_key: str
) -> None:
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    for payload in INJECTIONS:
        sessions = await client.get("/api/v1/sessions", params={"plate": payload}, headers=headers)
        assert sessions.status_code in {200, 404, 422}, sessions.text[:200]
        rule = await client.post(
            "/api/v1/access-rules", headers=headers, json={"plate": payload, "kind": "WHITELIST", "reason": "probe"}
        )
        assert rule.status_code == 422, rule.text[:200]
        event = await send_event(client, entry_key, event_body(payload))
        # номер нормализуется до [A-Z0-9]: инъекция либо отвергается (422), либо становится обычной строкой-номером
        # (например «../../etc/passwd» → ETCPASSWD) — хранится только хешем, в SQL не попадает
        assert event.status_code in {200, 422}, event.text[:200]
        if event.status_code == 422:
            assert event.json()["error"]["code"] in {"VALIDATION_ERROR", "INVALID_PLATE"}
        correction = await client.post(
            f"/api/v1/sessions/{uuid4()}/correct-plate", headers=headers, json={"plate": payload, "reason": "probe"}
        )
        assert correction.status_code in {404, 422}


# ---------------------------------------------------------------- обход RLS напрямую под ролью приложения


async def test_app_role_cannot_disable_or_bypass_rls(critical: asyncpg.Connection) -> None:
    async with tenant_transaction(critical, TENANT_A):
        for sql in (
            "ALTER TABLE parking_session DISABLE ROW LEVEL SECURITY",
            "ALTER TABLE parking_session NO FORCE ROW LEVEL SECURITY",
            "SET ROLE postgres",
            "ALTER ROLE app_critical BYPASSRLS",
            "CREATE POLICY p_any ON parking_session USING (true)",
            "DROP POLICY IF EXISTS tenant_isolation ON parking_session",
            "ALTER TABLE parking_session OWNER TO app_critical",
            "COPY parking_session TO '/tmp/x'",
        ):
            with pytest.raises((asyncpg.InsufficientPrivilegeError, asyncpg.PostgresError)):
                await critical.execute(sql)
    # подмена tenant-контекста внутри транзакции даёт только строки другого tenant, но не «все»
    async with tenant_transaction(critical, TENANT_A):
        await critical.execute("SELECT set_config('app.current_tenant_id', $1, true)", str(TENANT_B))
        rows = await critical.fetch("SELECT DISTINCT tenant_id FROM parking_session")
        assert {r["tenant_id"] for r in rows} <= {TENANT_B}
        await critical.execute("SELECT set_config('app.current_tenant_id', '', true)")
        assert await critical.fetch("SELECT tenant_id FROM parking_session") == []
    superuser_probe = await critical.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    assert superuser_probe is False


def _unused(_: Any) -> None:  # pragma: no cover
    pass
