"""Граница транзакции (I-9): пока открыт UoW, внешние HTTP-вызовы запрещены.

Contextvar живёт здесь, чтобы `platform/http` и `platform/db` не зависели друг от друга.
"""

from __future__ import annotations

from contextvars import ContextVar

transaction_depth: ContextVar[int] = ContextVar("transaction_depth", default=0)


class TransactionBoundaryViolation(RuntimeError):  # noqa: N818 — имя задано фазой 3 (I-9)
    """Внешний вызов внутри открытой транзакции PostgreSQL."""


def in_transaction() -> bool:
    return transaction_depth.get() > 0
