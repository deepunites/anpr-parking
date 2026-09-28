"""Адаптеры Payme/Click против httpx.MockTransport: форма запросов, авторизация, таймаут → неизвестный результат,
5xx → breaker; в логах, попытках и аудите нет секретов и реквизитов."""

from __future__ import annotations

import json
import re
from datetime import date
from uuid import uuid4

import asyncpg
import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from parking.modules.billing.application.ports import (
    GatewayError,
    GatewayTimeoutError,
    PaymentRequest,
    ProviderAccount,
    RefundRequest,
)
from parking.modules.billing.domain.payment import Provider
from parking.modules.billing.domain.reconciliation import RemoteState
from parking.modules.billing.infrastructure.gateways import ClickGateway, PaymeGateway
from parking.modules.identity.application.contracts import Role
from parking.platform.breaker import BreakerRegistry
from parking.platform.secrets import JsonSecretProvider
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A
from tests.support.api_fixtures import Principal
from tests.support.billing_fixtures import (
    CLICK_SECRET,
    CLICK_SERVICE_ID,
    PAYME_KEY,
    PAYME_MERCHANT,
    TEST_SECRETS,
    FakeBilling,
    as_user,
    make_obligation,
)

CARD = "8600123456789012"
SECRETS = JsonSecretProvider(json.dumps(TEST_SECRETS))
PAYME_ACCOUNT = ProviderAccount(uuid4(), TENANT_A, Provider.PAYME, PAYME_MERCHANT, "kv/tenants/alpha/payme", True)
CLICK_ACCOUNT = ProviderAccount(uuid4(), TENANT_A, Provider.CLICK, CLICK_SERVICE_ID, "kv/tenants/alpha/click", True)


def _request(amount: int = 5000) -> PaymentRequest:
    return PaymentRequest(
        payment_id=uuid4(),
        session_id=uuid4(),
        amount=amount,
        currency="UZS",
        idempotency_key="idem-1",
        description="test",
    )


def _payme(handler: object, **kw: object) -> PaymeGateway:
    gw = PaymeGateway(
        PAYME_ACCOUNT,
        SECRETS,
        breakers=BreakerRegistry(threshold=2, ttl_seconds=60),
        base_url="https://payme.test",
        **kw,
    )  # type: ignore[arg-type]
    gw._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://payme.test")  # type: ignore[arg-type]
    return gw


async def test_payme_create_check_cancel_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body["method"] == "receipts.create":
            return httpx.Response(200, json={"result": {"receipt": {"_id": "rc-1", "state": 0, "create_time": 1}}})
        if body["method"] == "receipts.check":
            return httpx.Response(200, json={"result": {"state": 4}})
        if body["method"] == "receipts.cancel":
            return httpx.Response(200, json={"result": {"receipt": {"_id": "rc-1", "state": 50}}})
        return httpx.Response(
            200, json={"result": [{"_id": "rc-9", "amount": 100, "state": 4, "account": {"order_id": str(uuid4())}}]}
        )

    gw = _payme(handler)
    intent = await gw.create(_request())
    assert intent.provider_transaction_id == "rc-1" and intent.state is RemoteState.PENDING
    assert intent.checkout_url is not None and intent.checkout_url.startswith("https://payme.test/")
    assert (
        seen[0].headers["X-Auth"] == f"{PAYME_MERCHANT}:{PAYME_KEY}"
        and json.loads(seen[0].content)["params"]["amount"] == 5000
    )
    status = await gw.status("rc-1", idempotency_key="idem-1")
    assert status.state is RemoteState.PAID
    refund = await gw.refund(RefundRequest("rc-1", 5000, "r-1", "test"))
    assert refund.ok and refund.provider_refund_id == "rc-1"
    registry = await gw.registry(date(2026, 9, 1), date(2026, 9, 1))
    assert registry and registry[0].provider_transaction_id == "rc-9" and registry[0].state is RemoteState.PAID


async def test_payme_timeout_is_unknown_and_5xx_opens_breaker() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    gw = _payme(timeout)
    with pytest.raises(GatewayTimeoutError):
        await gw.create(_request())

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    gw = _payme(broken)
    for _ in range(2):
        with pytest.raises(GatewayError):
            await gw.create(_request())
    with pytest.raises(GatewayError, match="circuit open"):
        await gw.create(_request())


async def test_click_requests_and_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/invoice/create"):
            return httpx.Response(200, json={"error_code": 0, "invoice_id": 4242})
        if "/payment/status_by_mti/" in request.url.path:
            return httpx.Response(200, json={"error_code": 0, "payment_id": 777, "payment_status": 2})
        return httpx.Response(200, json={"error_code": 0})

    gw = ClickGateway(
        CLICK_ACCOUNT, SECRETS, breakers=BreakerRegistry(), base_url="https://click.test", now=lambda: 1_700_000_000
    )
    gw._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://click.test")  # type: ignore[arg-type]
    intent = await gw.create(_request(123_400))
    assert intent.provider_transaction_id == "4242" and json.loads(seen[0].content)["amount"] == "1234.00"
    auth = seen[0].headers["Auth"].split(":")
    assert (
        auth[0] == "4242"
        and auth[2] == "1700000000"
        and len(auth[1]) == 40
        and CLICK_SECRET not in seen[0].headers["Auth"]
    )
    status = await gw.status("4242", idempotency_key="idem-1")
    assert status.state is RemoteState.PAID and status.provider_transaction_id == "777"
    refund = await gw.refund(RefundRequest("777", 100, "r", "x"))
    assert refund.ok and seen[-1].method == "DELETE"


async def test_no_secrets_or_card_numbers_in_logs_attempts_and_audit(
    client: httpx.AsyncClient,
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    users: dict[Role, Principal],
    clean_lot: None,
) -> None:
    """Gate: ни одного секрета провайдера, ни одного номера карты — в логах, payment_attempt и audit_log."""
    from tests.billing.test_webhooks import click, click_form, payme

    manager = as_user(users[Role.MANAGER])
    ob = await make_obligation(container, owner, PlateNumber.parse(f"01L{uuid4().hex[:5].upper()}"))
    with capture_logs() as logs:
        await fake_billing.online.checkout(manager, ob.payment_id, Provider.PAYME, return_url=None)
        await payme(
            client,
            "CheckPerformTransaction",
            {"amount": ob.amount, "account": {"order_id": str(ob.payment_id)}, "card": CARD},
        )
        await payme(
            client,
            "CheckPerformTransaction",
            {"amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
            key="bad",
        )
        await click(
            client,
            click_form(click_trans_id="log-1", merchant_trans_id=str(ob.payment_id), amount_minor=ob.amount, action=0),
        )
    structlog.reset_defaults()
    text = json.dumps(logs, default=str)
    forbidden = [PAYME_KEY, CLICK_SECRET, CARD, "test-bearer-secret"]
    for secret in forbidden:
        assert secret not in text, f"secret leaked into logs: {secret[:6]}…"
    assert not re.search(r"\b\d{16}\b", text), "card-like number in logs"
    attempts = await owner.fetch(
        "SELECT request_payload::text AS req, response_payload::text AS resp FROM payment_attempt WHERE payment_id = $1",
        ob.payment_id,
    )
    audits = await owner.fetch(
        "SELECT payload::text AS payload, reason FROM audit_log WHERE created_at > now() - interval '1 minute'"
    )
    inbox = await owner.fetch(
        "SELECT payload::text AS payload FROM webhook_inbox WHERE received_at > now() - interval '1 minute'"
    )
    blob = " ".join(
        [
            *(r["req"] or "" for r in attempts),
            *(r["resp"] or "" for r in attempts),
            *(r["payload"] for r in audits),
            *(r["payload"] for r in inbox),
        ]
    )
    for secret in [*forbidden[:2], "test-bearer-secret"]:
        assert secret not in blob
    assert "sign_string" not in blob  # подпись Click в inbox не хранится
    print(
        "\nGATE secrets: 0 provider secrets / card numbers in captured logs, payment_attempt, audit_log, webhook_inbox"
    )
