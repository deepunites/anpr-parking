"""Гонки вебхука провайдера и сохранения платежа (фаза 8 §3): один и тот же PerformTransaction одновременно
из нескольких воркеров провайдера; вебхук одновременно с подтверждением наличных кассиром. Реальные транзакции,
реальный API: ровно один PAID-эффект, одинаковые ответы, ни одного 500."""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import asyncpg
import httpx

from parking.modules.identity.application.contracts import CurrentUser
from parking.runtime.container import Container
from parking.shared.errors import ConflictError, DomainError
from parking.shared.plate import PlateNumber
from tests.billing.test_webhooks import payme
from tests.support.billing_fixtures import Obligation, make_obligation

PARALLEL = 8


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01R{uuid4().int % 900 + 100}RR")


async def _created_tx(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection
) -> tuple[str, int, Obligation]:
    ob = await make_obligation(container, owner, _plate())
    tx = f"payme-race-{uuid4().hex[:10]}"
    created = await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
    )
    assert created["result"]["state"] == 1, created
    return tx, ob.amount, ob


async def test_concurrent_identical_performs_apply_once(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    """Провайдер доставил PerformTransaction параллельно (ретраи с нескольких узлов), а не 5 раз подряд."""
    tx, _amount, ob = await _created_tx(client, container, owner)
    responses = await asyncio.gather(
        *(payme(client, "PerformTransaction", {"id": tx}, rpc_id=i) for i in range(PARALLEL))
    )
    assert all("result" in r and r["result"]["state"] == 2 for r in responses), responses
    assert len({json.dumps(r["result"], sort_keys=True) for r in responses}) == 1, "все ответы одинаковые"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PAID"
    assert (
        await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1 AND status = 'PAID'", ob.session_id)
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.confirmed.v1' AND aggregate_id = $1",
            ob.payment_id,
        )
        == 1
    )
    inbox = await owner.fetch(
        "SELECT status::text AS status FROM webhook_inbox WHERE provider = 'PAYME' AND provider_event_id = $1",
        f"PerformTransaction:{tx}",
    )
    assert len(inbox) == 1 and inbox[0]["status"] == "APPLIED"


async def test_perform_races_with_cash_confirmation(
    client: httpx.AsyncClient,
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    cashier: CurrentUser,
    clean_lot: None,
) -> None:
    """Клиент платит онлайн, а кассир в ту же секунду подтверждает наличные по тому же обязательству:
    платёж переходит в PAID один раз и одним методом; проигравшая сторона получает честный отказ (кассиру — конфликт,
    провайдеру — код ошибки Payme) либо дубль уходит в очередь возврата (DUPLICATE_REMOTE), но никогда не 500 и
    никогда не два подтверждения одного обязательства."""
    tx, amount, ob = await _created_tx(client, container, owner)

    async def cash() -> str:
        try:
            paid = await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=amount, reason=None)
        except (ConflictError, DomainError) as exc:
            return f"refused:{type(exc).__name__}"
        return f"paid:{paid.method.value if paid.method else None}"

    async def webhook() -> str:
        response = await payme(client, "PerformTransaction", {"id": tx}, rpc_id=7)
        return "performed" if "result" in response else f"error:{response['error']['code']}"

    cash_outcome, webhook_outcome = await asyncio.gather(cash(), webhook())

    row = await owner.fetchrow(
        "SELECT status::text AS status, method::text AS method FROM payment WHERE id = $1",
        ob.payment_id,
    )
    assert row is not None and row["status"] == "PAID" and row["method"] in {"CASH", "ONLINE", "CARD"}, row
    confirmed = await owner.fetchval(
        "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.confirmed.v1' AND aggregate_id = $1",
        ob.payment_id,
    )
    assert confirmed == 1, "обязательство подтверждено ровно один раз"
    paid_for_session = await owner.fetchval(
        "SELECT count(*) FROM payment WHERE session_id = $1 AND status = 'PAID'",
        ob.session_id,
    )
    if paid_for_session == 2:  # оба списания реальны: второй платёж — в очереди возврата, как при двойной оплате
        duplicate = await owner.fetchrow(
            "SELECT kind::text AS kind, status::text AS status FROM reconciliation_discrepancy "
            "WHERE payment_id <> $1 AND details::jsonb ->> 'original_payment_id' = $2",
            ob.payment_id,
            str(ob.payment_id),
        )
        assert duplicate is not None and duplicate["kind"] == "DUPLICATE_REMOTE" and duplicate["status"] == "OPEN"
    else:
        assert paid_for_session == 1
        assert (cash_outcome.startswith("paid") and row["method"] == "CASH") or (
            webhook_outcome == "performed" and row["method"] != "CASH"
        ), (cash_outcome, webhook_outcome, row)
    print(f"\nGATE webhook vs cash race: cash={cash_outcome} webhook={webhook_outcome} method={row['method']}")
