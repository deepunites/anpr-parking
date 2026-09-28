"""Класс ошибки → HTTP-код → код в ответе. Stack trace клиенту не уходит никогда; в лог — целиком."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from parking.modules.anpr.infrastructure.repositories import is_connectivity_error
from parking.platform.logging import get_logger
from parking.shared.correlation import get_correlation_id
from parking.shared.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DomainError,
    ExternalServiceError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationError,
)
from parking.shared.plate import InvalidPlateError

log = get_logger("api.errors")

HTTP_STATUS: dict[type[DomainError], int] = {
    ValidationError: 422,
    NotFoundError: 404,
    ConflictError: 409,
    AuthenticationError: 401,
    AuthorizationError: 403,
    RateLimitedError: 429,
    ExternalServiceError: 502,
    ServiceUnavailableError: 503,
}


def error_body(code: str, message: str, details: dict[str, object] | None = None) -> dict[str, object]:
    body: dict[str, object] = {"code": code, "message": message, "correlation_id": get_correlation_id()}
    if details:
        body["details"] = details
    return {"error": body}


def status_for(exc: DomainError) -> int:
    for cls in type(exc).__mro__:
        if cls in HTTP_STATUS:
            return HTTP_STATUS[cls]
    return 400


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        status = status_for(exc)
        headers = {}
        if isinstance(exc, RateLimitedError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        log.info("domain_error", code=exc.code, status=status)
        return JSONResponse(status_code=status, content=error_body(exc.code, exc.message, exc.details), headers=headers)

    @app.exception_handler(InvalidPlateError)
    async def invalid_plate(_: Request, exc: InvalidPlateError) -> JSONResponse:
        return JSONResponse(status_code=422, content=error_body("INVALID_PLATE", str(exc)))

    @app.exception_handler(RequestValidationError)
    async def request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details: dict[str, object] = {
            "errors": [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        }
        return JSONResponse(
            status_code=422, content=error_body("VALIDATION_ERROR", "request validation failed", details)
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            429: "RATE_LIMITED",
        }.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code, content=error_body(code, str(exc.detail)), headers=dict(exc.headers or {})
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        if is_connectivity_error(exc) or isinstance(exc, RedisConnectionError | RedisTimeoutError):
            # PostgreSQL/Redis недоступны: честный 503 без трассировки на каждый запрос (runbook 1/3 фазы 10)
            log.warning("dependency_unavailable", error_type=type(exc).__name__)
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "5"},
                content=error_body("DEPENDENCY_UNAVAILABLE", "a backing service is unavailable, retry later"),
            )
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(status_code=500, content=error_body("INTERNAL_ERROR", "internal error"))
