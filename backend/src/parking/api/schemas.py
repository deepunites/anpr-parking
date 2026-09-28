"""Общие схемы ответов."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

Reason = Annotated[
    str, Field(min_length=3, max_length=500, description="Обязательное обоснование ручного действия (I-15)")
]


class MoneyOut(BaseModel):
    minor: int
    currency: str
    formatted: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
