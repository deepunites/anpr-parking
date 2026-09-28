"""Единственная точка создания исходящих HTTP-клиентов (I-9: транспорт с проверкой границы транзакции)."""

from parking.platform.http.client import GuardedTransport, make_async_client

__all__ = ["GuardedTransport", "make_async_client"]
