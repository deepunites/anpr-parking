"""Зависимости FastAPI: контейнер, текущий пользователь, `require(Permission)`, данные клиента."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from parking.modules.identity.application.auth_service import ClientInfo
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.integrations.application.contracts import looks_like_api_key
from parking.runtime.container import Container
from parking.shared.correlation import tenant_id_var, user_id_var
from parking.shared.errors import AuthenticationError, AuthorizationError, RateLimitedError

_bearer = HTTPBearer(auto_error=False)


log = structlog.get_logger(__name__)


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("container is not initialised (lifespan not started)")
    return container


def client_info(request: Request) -> ClientInfo:
    forwarded = request.headers.get("x-forwarded-for")
    raw = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    return ClientInfo(ip=_ip_or_none(raw), user_agent=(request.headers.get("user-agent") or "")[:512] or None)


def _ip_or_none(value: str | None) -> str | None:
    """Только валидный IPv4/IPv6 (колонка inet): мусор из X-Forwarded-For не должен ронять логин."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


API_KEY_FAIL_LIMIT = 20  # v2 этап 8: неверных API-ключей с одного адреса за окно — дальше 429 без запроса в БД
API_KEY_FAIL_WINDOW_SECONDS = 300


async def _api_key_user(container: Container, request: Request, token: str) -> CurrentUser:
    ip = client_info(request).ip or "unknown"
    key = f"apikey:fail:{ip}"
    try:
        failures = int(await container.redis.get(key) or 0)
    except Exception:  # Redis недоступен — проверка ключа важнее ограничителя
        failures = 0
    if failures >= API_KEY_FAIL_LIMIT:
        raise RateLimitedError("too many invalid api keys", retry_after_seconds=API_KEY_FAIL_WINDOW_SECONDS)
    try:
        return await container.api_keys.authenticate(token)
    except AuthenticationError:
        try:
            if int(await container.redis.incr(key)) == 1:
                await container.redis.expire(key, API_KEY_FAIL_WINDOW_SECONDS)
        except Exception:
            log.warning("api_key_limiter_unavailable")
        raise


async def current_user(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("missing bearer token")
    token = credentials.credentials
    if looks_like_api_key(token):  # v2 этап 6: ключ внешней системы — права только из его scopes
        user = await _api_key_user(container, request, token)
    else:
        user = await container.auth.authenticate(token)
    tenant_id_var.set(user.tenant_id)
    user_id_var.set(user.user_id)
    structlog.contextvars.bind_contextvars(tenant_id=str(user.tenant_id), user_id=str(user.user_id))
    return user


def require(*permissions: Permission) -> Callable[..., Coroutine[Any, Any, CurrentUser]]:
    """Зависимость: аутентифицированный пользователь со всеми перечисленными разрешениями, иначе 403."""

    async def dependency(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        if user.must_change_password:  # v2 этап 4: временный пароль — сначала смена (/auth/password), потом работа
            raise AuthorizationError("password change required", code="PASSWORD_CHANGE_REQUIRED")
        missing = [p.value for p in permissions if not user.has(p)]
        if missing:
            raise AuthorizationError("insufficient permissions", details={"missing": missing})
        return user

    return dependency


def permission_extra(*permissions: Permission) -> dict[str, Any]:
    """OpenAPI-расширение x-permission: контрактный RBAC-тест генерирует таблицу (endpoint, role) из него."""
    return {"x-permission": [p.value for p in permissions]}


PUBLIC = {"x-public": True}
