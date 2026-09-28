"""API-тесты: приложение на тестовой БД (роли app_*), клиент httpx через ASGI, пользователи всех ролей."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from parking.api.app import create_app
from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.identity.application.user_service import CreateUserCommand
from parking.modules.identity.domain.permissions import Role
from parking.platform.config import DevSettings
from parking.platform.crypto import EnvKeyProvider
from parking.platform.db.engine import Database, build_database
from parking.runtime.container import Container, build_container
from tests.conftest import LOT_A1, TENANT_A, TENANT_B, USER_A_ADMIN, USER_B_ADMIN, _with_db

PASSWORD = "correct-horse-battery-staple"
# v2 этап 3: постоянный KEK тестов — строки tenant_key в parking_test переживают прогоны и должны разворачиваться
TEST_KEK = base64.b64encode(bytes(range(32))).decode()


def _sqla(dsn: str, role: str) -> str:
    return _with_db(dsn, "parking_test", user=role).replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis для тестов: PARKING_TEST_REDIS_URL (CI) или локальный redis_dev на порту 6390."""
    external = os.environ.get("PARKING_TEST_REDIS_URL")
    if external:
        return external
    import subprocess
    import sys

    from tests.conftest import BACKEND

    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "redis_dev.py"), "start", "6390"], check=True, capture_output=True
    )
    return "redis://127.0.0.1:6390/1"


def _test_secrets() -> dict[str, str]:
    from tests.support.billing_fixtures import TEST_SECRETS

    return TEST_SECRETS


def _dev_env() -> dict[str, str]:
    """backend/.env.dev — креды локального MinIO (scripts/minio_dev.py credentials())."""
    from tests.conftest import BACKEND

    env: dict[str, str] = {}
    path = BACKEND / ".env.dev"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


@pytest.fixture(scope="session")
def api_settings(test_dsn: str, redis_url: str) -> DevSettings:
    keys = {str(TENANT_A): EnvKeyProvider.generate_entry(), str(TENANT_B): EnvKeyProvider.generate_entry()}
    os.environ.pop("PARKING_ENV", None)
    dev_env = _dev_env()
    return DevSettings(
        _env_file=None,  # type: ignore[call-arg]
        database_url_critical=_sqla(test_dsn, "app_critical"),  # type: ignore[arg-type]
        database_url_bulk=_sqla(test_dsn, "app_bulk"),  # type: ignore[arg-type]
        database_url_maintenance=_sqla(test_dsn, "app_maintenance"),  # type: ignore[arg-type]
        tenant_keys_json=json.dumps(keys),  # type: ignore[arg-type]
        key_wrapping_key=TEST_KEK,  # type: ignore[arg-type]
        signup_dev_codes=True,
        redis_url=redis_url,  # type: ignore[arg-type]
        cors_origins=["http://localhost:5173"],
        access_token_ttl_seconds=900,
        s3_access_key=dev_env.get("PARKING_S3_ACCESS_KEY", ""),  # type: ignore[arg-type]
        s3_secret_key=dev_env.get("PARKING_S3_SECRET_KEY", ""),  # type: ignore[arg-type]
        secrets_json=json.dumps(_test_secrets()),  # type: ignore[arg-type]
        provider_timeout_seconds=1.0,
        redis_socket_timeout_seconds=1.0,  # шумный CI/песочница: 0.2 с (prod) давал ложные TimeoutError под чужой нагрузкой
        provider_base_urls={
            "PAYME": "http://127.0.0.1:9",
            "CLICK": "http://127.0.0.1:9",
        },  # тесты никогда не ходят к провайдерам
        otel_entry_sample_ratio=0.0,  # детерминизм: ENTRY-трассы не экспортируются, EXIT — всегда (по атрибуту)
        otel_api_sample_ratio=0.0,
    )


@pytest.fixture(scope="session")
def minio() -> str:
    """Локальный MinIO для media-тестов: PARKING_TEST_S3_ENDPOINT (CI) или scripts/minio_dev.py setup (9000)."""
    external = os.environ.get("PARKING_TEST_S3_ENDPOINT")
    if external:
        return external
    import subprocess
    import sys

    from tests.conftest import BACKEND

    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "minio_dev.py"), "setup"], check=True, capture_output=True
    )
    return "http://127.0.0.1:9000"


@pytest.fixture(autouse=True)
async def _clean_login_attempts(request: pytest.FixtureRequest) -> None:
    """Троттлинг считает неудачи по (email, ip): тесты не должны влиять друг на друга. Только для тестов с API/БД —
    чистые доменные тесты (mutation testing) не должны поднимать PostgreSQL."""
    if not ({"client", "app", "container", "database"} & set(request.fixturenames)):
        return
    import asyncpg

    conn = await asyncpg.connect(request.getfixturevalue("test_dsn"))
    try:
        await conn.execute("DELETE FROM login_attempt")
    finally:
        await conn.close()


@pytest.fixture
async def database(api_settings: DevSettings) -> AsyncIterator[Database]:
    db = build_database(api_settings)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def container(api_settings: DevSettings, database: Database) -> Container:
    return build_container(api_settings, database)


@pytest.fixture
async def app(api_settings: DevSettings, database: Database, container: Container) -> FastAPI:
    application = create_app(api_settings, database=database)
    application.state.database = database
    application.state.container = container
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: UUID
    tenant_id: UUID
    role: Role
    email: str


async def ensure_user(
    container: Container, tenant_id: UUID, admin_id: UUID, role: Role, email: str, lots: frozenset[UUID] = frozenset()
) -> Principal:
    actor = CurrentUser(user_id=admin_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN, lot_scope=frozenset())
    async with container.uow(tenant_id) as uow:
        from parking.modules.identity.infrastructure.repositories import SqlUserRepository

        existing = await SqlUserRepository(uow.session).lookup_by_email(email)
    if existing is not None:
        return Principal(user_id=existing.user_id, tenant_id=tenant_id, role=role, email=email)
    user = await container.users.create(
        actor,
        CreateUserCommand(
            email=email, password=PASSWORD, role=role, display_name=email.split("@", maxsplit=1)[0], lot_scope=lots
        ),
        reason="test fixture",
    )
    return Principal(user_id=user.id, tenant_id=tenant_id, role=role, email=email)


@pytest.fixture
async def users(container: Container) -> dict[Role, Principal]:
    result: dict[Role, Principal] = {}
    for role in Role:
        result[role] = await ensure_user(
            container, TENANT_A, USER_A_ADMIN, role, f"role.{role.value.lower()}@alpha-parking.uz"
        )
    return result


@pytest.fixture
async def tenant_b_admin(container: Container) -> Principal:
    return await ensure_user(container, TENANT_B, USER_B_ADMIN, Role.TENANT_ADMIN, "role.tenant_admin@beta-garage.uz")


@pytest.fixture
async def scoped_operator(container: Container) -> Principal:
    return await ensure_user(
        container, TENANT_A, USER_A_ADMIN, Role.OPERATOR, "role.scoped_operator@alpha-parking.uz", frozenset({LOT_A1})
    )


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def bearer(client: httpx.AsyncClient, principal: Principal) -> dict[str, str]:
    response = await login(client, principal.email)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
