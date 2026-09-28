from __future__ import annotations

import httpx

from parking.platform.boundaries import TransactionBoundaryViolation, in_transaction


class GuardedTransport(httpx.AsyncBaseTransport):
    """Оборачивает любой транспорт httpx; бросает TransactionBoundaryViolation внутри UoW."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if in_transaction():
            raise TransactionBoundaryViolation(
                f"outbound HTTP {request.method} {request.url.host} inside an open database transaction (I-9)"
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def make_async_client(
    *,
    base_url: str = "",
    timeout: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
    follow_redirects: bool = False,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Фабрика клиентов: таймаут обязателен, редиректы выключены, транспорт всегда защищён."""
    inner = transport or httpx.AsyncHTTPTransport(retries=0)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=GuardedTransport(inner),
        follow_redirects=follow_redirects,
        headers=headers,
    )


class GuardedSyncTransport(httpx.BaseTransport):
    """Синхронный аналог GuardedTransport: для клиентов, живущих вне event loop (VaultKeyProvider при старте)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if in_transaction():
            raise TransactionBoundaryViolation(
                f"outbound HTTP {request.method} {request.url.host} inside an open database transaction (I-9)"
            )
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def make_sync_client(
    *,
    base_url: str = "",
    timeout: float = 5.0,
    transport: httpx.BaseTransport | None = None,
    verify: bool | str = True,
) -> httpx.Client:
    """Синхронная фабрика с теми же правилами: таймаут, без редиректов, защищённый транспорт (I-9)."""
    inner = transport or httpx.HTTPTransport(retries=0, verify=verify)
    return httpx.Client(
        base_url=base_url, timeout=timeout, transport=GuardedSyncTransport(inner), follow_redirects=False
    )
