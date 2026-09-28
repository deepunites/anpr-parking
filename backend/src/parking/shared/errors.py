"""Иерархия доменных ошибок и их отображение в HTTP (таблица в api/errors.py).

Сообщения ошибок не содержат ПДн и секретов: номер — только маскированный, ключи — никогда.
"""

from __future__ import annotations


class DomainError(Exception):
    """Базовая ошибка домена: машинный `code` + человекочитаемое `message`."""

    code: str = "DOMAIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}


class ValidationError(DomainError):
    code = "VALIDATION_ERROR"


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ConflictError(DomainError):
    code = "CONFLICT"


class AuthenticationError(DomainError):
    code = "UNAUTHENTICATED"


class AuthorizationError(DomainError):
    code = "FORBIDDEN"


class RateLimitedError(DomainError):
    code = "RATE_LIMITED"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class ExternalServiceError(DomainError):
    code = "EXTERNAL_SERVICE_ERROR"


class ServiceUnavailableError(DomainError):
    code = "SERVICE_UNAVAILABLE"
