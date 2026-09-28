"""Адаптеры провайдеров за портом PaymentGateway (Q1: Payme, Click). Написаны по публичной документации
(Payme Subscribe API receipts.*, Click Merchant API) — **требуют подтверждения на тестовых мерчант-аккаунтах**.
Общее: таймаут 5 с, circuit breaker на провайдера, метрики, ретрай create() запрещён (результат неизвестен →
GatewayTimeoutError → status()), секреты и реквизиты не попадают ни в лог, ни в request_payload попытки."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from parking.modules.billing.application.ports import (
    GatewayError,
    GatewayTimeoutError,
    PaymentGateway,
    PaymentIntent,
    PaymentRequest,
    PaymentStatusResult,
    ProviderAccount,
    RefundRequest,
    RefundResult,
)
from parking.modules.billing.domain.payment import Provider
from parking.modules.billing.domain.reconciliation import RemoteState, RemoteTx
from parking.platform import metrics
from parking.platform.breaker import BreakerRegistry, CircuitOpenError
from parking.platform.http.client import make_async_client
from parking.platform.logging import get_logger
from parking.platform.secrets import SecretProvider

log = get_logger("billing.gateway")

PAYME_STATE = {
    0: RemoteState.PENDING,
    1: RemoteState.PENDING,
    2: RemoteState.PENDING,
    3: RemoteState.PENDING,
    4: RemoteState.PAID,
}
PAYME_CANCELLED = {20, 21, 30, 50}
CLICK_PAID = 2


def _tx(payment_id: UUID | str) -> str:
    return str(payment_id)


class _HttpGateway:
    def __init__(self, *, breakers: BreakerRegistry, timeout: float, base_url: str, name: str) -> None:
        self._client = make_async_client(base_url=base_url, timeout=timeout)
        self._breaker = breakers.get(name)
        self._name = name

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self, method: str, url: str, *, json_body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        """Один HTTP-вызов под breaker'ом. Таймаут/обрыв → GatewayTimeoutError (результат неизвестен); прочее → GatewayError."""
        try:
            self._breaker.acquire()
        except CircuitOpenError as exc:
            metrics.gateway_calls_total.labels(provider=self._name, result="circuit_open").inc()
            raise GatewayError(f"{self._name}: circuit open, retry after {exc.retry_after:.0f}s") from exc
        started = time.perf_counter()
        try:
            response = await self._client.request(method, url, json=json_body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            self._breaker.record_failure()
            metrics.gateway_calls_total.labels(provider=self._name, result="timeout").inc()
            raise GatewayTimeoutError(f"{self._name}: {type(exc).__name__}") from exc
        finally:
            metrics.gateway_seconds.labels(provider=self._name).observe(time.perf_counter() - started)
        if response.status_code >= 500:
            self._breaker.record_failure()
            metrics.gateway_calls_total.labels(provider=self._name, result="5xx").inc()
            raise GatewayError(f"{self._name}: HTTP {response.status_code}")
        self._breaker.record_success()
        metrics.gateway_calls_total.labels(provider=self._name, result="ok").inc()
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayError(f"{self._name}: malformed response") from exc


class PaymeGateway(_HttpGateway):
    """Payme Subscribe API (JSON-RPC, X-Auth: merchant_id:key): receipts.create / receipts.check / receipts.cancel /
    receipts.get_all. Ссылка на оплату — стандартный checkout по base64-параметрам (m, ac.order_id, a)."""

    def __init__(
        self,
        account: ProviderAccount,
        secrets: SecretProvider,
        *,
        breakers: BreakerRegistry,
        timeout: float = 5.0,
        base_url: str = "https://checkout.paycom.uz",
    ) -> None:
        super().__init__(breakers=breakers, timeout=timeout, base_url=base_url, name="payme")
        self._merchant = account.merchant_id
        self._key = json.loads(secrets.get(account.secret_ref))["key"]
        self._base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"X-Auth": f"{self._merchant}:{self._key}", "Content-Type": "application/json"}

    async def _rpc(self, method: str, params: dict[str, Any], request_id: str) -> Any:
        body = {"id": request_id, "method": method, "params": params}
        data = await self._call("POST", "/api", json_body=body, headers=self._headers())
        if not isinstance(data, dict):
            raise GatewayError("payme: malformed response")
        if data.get("error"):
            code = str(data["error"].get("code"))
            metrics.gateway_calls_total.labels(provider="payme", result="error").inc()
            raise GatewayError(f"payme: error {code}")
        return data.get("result") or {}

    def checkout_url(self, payment_id: UUID, amount: int, return_url: str | None) -> str:
        params = f"m={self._merchant};ac.order_id={payment_id};a={amount}"
        if return_url:
            params += f";c={return_url}"
        return f"{self._base_url}/{base64.b64encode(params.encode()).decode()}"

    async def create(self, req: PaymentRequest) -> PaymentIntent:
        result = await self._rpc(
            "receipts.create",
            {"amount": req.amount, "account": {"order_id": str(req.payment_id)}, "description": req.description},
            req.idempotency_key,
        )
        receipt = result.get("receipt") or {}
        return PaymentIntent(
            provider_transaction_id=str(receipt.get("_id")) if receipt.get("_id") else None,
            checkout_url=self.checkout_url(req.payment_id, req.amount, req.return_url),
            state=PAYME_STATE.get(int(receipt.get("state", 0)), RemoteState.PENDING),
            raw={"state": receipt.get("state"), "create_time": receipt.get("create_time")},
        )

    async def status(self, provider_tx_id: str | None, *, idempotency_key: str) -> PaymentStatusResult:
        if provider_tx_id is None:
            return PaymentStatusResult(state=None, provider_transaction_id=None, amount=None)
        try:
            result = await self._rpc("receipts.check", {"id": provider_tx_id}, idempotency_key)
        except GatewayError as exc:
            if "-31601" in str(exc) or "-32602" in str(exc):  # чек не найден
                return PaymentStatusResult(state=None, provider_transaction_id=provider_tx_id, amount=None)
            raise
        state_raw = int(result.get("state", -1))
        state = RemoteState.CANCELLED if state_raw in PAYME_CANCELLED else PAYME_STATE.get(state_raw)
        return PaymentStatusResult(
            state=state, provider_transaction_id=provider_tx_id, amount=None, raw={"state": state_raw}
        )

    async def refund(self, req: RefundRequest) -> RefundResult:
        try:
            result = await self._rpc("receipts.cancel", {"id": req.provider_transaction_id}, req.idempotency_key)
        except GatewayTimeoutError:
            raise
        except GatewayError as exc:
            return RefundResult(ok=False, error=str(exc))
        receipt = result.get("receipt") or {}
        return RefundResult(
            ok=True,
            provider_refund_id=str(receipt.get("_id") or req.provider_transaction_id),
            raw={"state": receipt.get("state")},
        )

    async def registry(self, start: date, end: date) -> list[RemoteTx]:
        from_ms = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
        to_ms = int((datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)).timestamp() * 1000)
        receipts: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = await self._rpc(
                "receipts.get_all", {"count": 50, "from": from_ms, "to": to_ms, "offset": offset}, f"reg-{offset}"
            )
            page: list[dict[str, Any]] = list(result if isinstance(result, list) else result.get("receipts") or [])
            receipts.extend(page)
            if len(page) < 50:
                break
            offset += 50
        out: list[RemoteTx] = []
        for r in receipts:
            state_raw = int(r.get("state", 0))
            order = (r.get("account") or {}).get("order_id")
            out.append(
                RemoteTx(
                    provider_transaction_id=str(r.get("_id")),
                    amount=int(r.get("amount", 0)),
                    state=RemoteState.CANCELLED
                    if state_raw in PAYME_CANCELLED
                    else PAYME_STATE.get(state_raw, RemoteState.PENDING),
                    payment_id=UUID(order) if order and _is_uuid(order) else None,
                    raw={"state": state_raw},
                )
            )
        return out


class ClickGateway(_HttpGateway):
    """Click Merchant API (Auth: merchant_user_id:sha1(timestamp+secret_key):timestamp): invoice/create,
    payment/status_by_mti, payment/reversal. Суммы Click — в сумах с копейками; у нас — тийины (minor)."""

    def __init__(
        self,
        account: ProviderAccount,
        secrets: SecretProvider,
        *,
        breakers: BreakerRegistry,
        timeout: float = 5.0,
        base_url: str = "https://api.click.uz/v2/merchant",
        now: Any = None,
    ) -> None:
        super().__init__(breakers=breakers, timeout=timeout, base_url=base_url, name="click")
        cfg = json.loads(secrets.get(account.secret_ref))
        self._service_id = str(cfg["service_id"])
        self._merchant_id = account.merchant_id
        self._user_id = str(cfg["merchant_user_id"])
        self._secret = str(cfg["secret_key"])
        self._now = now or (lambda: int(time.time()))

    def _headers(self) -> dict[str, str]:
        ts = str(self._now())
        digest = hashlib.sha1((ts + self._secret).encode()).hexdigest()  # noqa: S324 # nosec B324
        return {
            "Auth": f"{self._user_id}:{digest}:{ts}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def create(self, req: PaymentRequest) -> PaymentIntent:
        data = await self._call(
            "POST",
            "/invoice/create",
            json_body={
                "service_id": self._service_id,
                "amount": f"{req.amount / 100:.2f}",
                "phone_number": "",
                "merchant_trans_id": str(req.payment_id),
            },
            headers=self._headers(),
        )
        if int(data.get("error_code", -1)) != 0:
            raise GatewayError(f"click: error {data.get('error_code')}")
        invoice = str(data.get("invoice_id"))
        url = f"https://my.click.uz/services/pay?service_id={self._service_id}&merchant_id={self._merchant_id}&amount={req.amount / 100:.2f}&transaction_param={req.payment_id}"
        return PaymentIntent(
            provider_transaction_id=invoice, checkout_url=url, state=RemoteState.PENDING, raw={"invoice_id": invoice}
        )

    async def status(self, provider_tx_id: str | None, *, idempotency_key: str) -> PaymentStatusResult:
        day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        data = await self._call(
            "GET", f"/payment/status_by_mti/{self._service_id}/{idempotency_key}/{day}", headers=self._headers()
        )
        code = int(data.get("error_code", -1))
        if code != 0:
            return PaymentStatusResult(
                state=None, provider_transaction_id=provider_tx_id, amount=None, raw={"error_code": code}
            )
        status = int(data.get("payment_status", 0))
        state = (
            RemoteState.PAID if status == CLICK_PAID else (RemoteState.CANCELLED if status < 0 else RemoteState.PENDING)
        )
        return PaymentStatusResult(
            state=state,
            provider_transaction_id=str(data.get("payment_id") or provider_tx_id),
            amount=None,
            raw={"payment_status": status},
        )

    async def refund(self, req: RefundRequest) -> RefundResult:
        data = await self._call(
            "DELETE", f"/payment/reversal/{self._service_id}/{req.provider_transaction_id}", headers=self._headers()
        )
        code = int(data.get("error_code", -1))
        if code != 0:
            return RefundResult(ok=False, error=f"click: error {code}", raw={"error_code": code})
        return RefundResult(ok=True, provider_refund_id=req.provider_transaction_id, raw={"error_code": code})

    async def registry(self, start: date, end: date) -> list[RemoteTx]:
        """У Click нет пакетной выгрузки в Merchant API: реестр собирается по нашим же транзакциям (status_by_mti),
        поэтому MISSING_LOCAL для Click невыявляем — см. A-35."""
        return []


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


class HttpGatewayRegistry:
    def __init__(
        self,
        secrets: SecretProvider,
        *,
        breakers: BreakerRegistry,
        timeout: float = 5.0,
        base_urls: dict[str, str] | None = None,
    ) -> None:
        self._secrets = secrets
        self._breakers = breakers
        self._timeout = timeout
        self._base_urls = base_urls or {}
        self._cache: dict[UUID, PaymentGateway] = {}

    def gateway_for(self, account: ProviderAccount) -> PaymentGateway:
        cached = self._cache.get(account.id)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {"breakers": self._breakers, "timeout": self._timeout}
        if account.provider.value in self._base_urls:
            kwargs["base_url"] = self._base_urls[account.provider.value]
        gateway: PaymentGateway
        if account.provider is Provider.PAYME:
            gateway = PaymeGateway(account, self._secrets, **kwargs)
        elif account.provider is Provider.CLICK:
            gateway = ClickGateway(account, self._secrets, **kwargs)
        else:
            raise GatewayError(f"no gateway for provider {account.provider.value}")
        self._cache[account.id] = gateway
        return gateway
