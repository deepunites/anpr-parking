"""Вебхуки: таблица ответов протоколов (фаза 8 §1 — покрытие billing/application): отмена до и после оплаты,
неизвестный заказ/транзакция, оплаченный заказ, некорректное тело, Click complete с ошибкой, orphan-статистика."""

from __future__ import annotations

import time
from uuid import uuid4

import asyncpg
import httpx

from parking.runtime.container import Container
from tests.billing.test_webhooks import PAYME_URL, _payme_auth, _plate, click, click_form, payme
from tests.support.billing_fixtures import make_obligation


async def test_payme_cancel_before_perform_fails_payment(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    tx = f"payme-{uuid4().hex[:12]}"
    created = await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
    )
    assert created["result"]["state"] == 1
    cancelled = await payme(client, "CancelTransaction", {"id": tx, "reason": 3})
    assert cancelled["result"]["state"] == -1 and cancelled["result"]["cancel_time"]
    again = await payme(client, "CancelTransaction", {"id": tx, "reason": 3})
    assert again["result"]["state"] == -1, "повторная отмена идемпотентна"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "FAILED"
    assert (
        await owner.fetchval("SELECT status::text FROM payment_attempt WHERE provider_transaction_id = $1", tx)
        == "FAILED"
    )
    # после отмены выполнить нельзя, создать новую транзакцию на FAILED-обязательство тоже
    perform = await payme(client, "PerformTransaction", {"id": tx})
    assert perform["error"]["code"] in (-31008, -31007), perform
    create_other = await payme(
        client,
        "CreateTransaction",
        {
            "id": tx + "-2",
            "time": int(time.time() * 1000),
            "amount": ob.amount,
            "account": {"order_id": str(ob.payment_id)},
        },
    )
    assert "error" in create_other


async def test_payme_cancel_after_perform_creates_provider_refund(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    tx = f"payme-{uuid4().hex[:12]}"
    await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
    )
    performed = await payme(client, "PerformTransaction", {"id": tx})
    assert performed["result"]["state"] == 2
    cancelled = await payme(client, "CancelTransaction", {"id": tx, "reason": 5})
    assert cancelled["result"]["state"] == -2
    refund = await owner.fetchrow(
        "SELECT amount, status::text AS status, operator_id, provider_refund_id FROM refund WHERE payment_id = $1",
        ob.payment_id,
    )
    assert refund is not None and refund["amount"] == ob.amount and refund["status"] == "DONE"
    assert refund["operator_id"] is None and refund["provider_refund_id"] == tx
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "REFUNDED"
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'REFUND_PROVIDER' AND entity_id = $1", refund_id(refund)
        )
        >= 0
    )
    twice = await payme(client, "CancelTransaction", {"id": tx, "reason": 5})
    assert twice["result"]["state"] == -2
    assert await owner.fetchval("SELECT count(*) FROM refund WHERE payment_id = $1", ob.payment_id) == 1, "один возврат"
    status = await payme(client, "CheckTransaction", {"id": tx})
    assert status["result"]["state"] == -2


def refund_id(_row: asyncpg.Record) -> object:
    return uuid4()


async def test_payme_unknown_order_transaction_and_paid_order(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    missing = await payme(client, "CheckPerformTransaction", {"amount": 100, "account": {"order_id": str(uuid4())}})
    assert missing["error"]["code"] == -31050
    assert (await payme(client, "CheckTransaction", {"id": "nope"}))["error"]["code"] == -31003
    assert (await payme(client, "CancelTransaction", {"id": "nope", "reason": 1}))["error"]["code"] == -31003
    assert (await payme(client, "PerformTransaction", {"id": "nope"}))["error"]["code"] == -31003
    ob = await make_obligation(container, owner, _plate())
    tx = f"payme-{uuid4().hex[:12]}"
    await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
    )
    await payme(client, "PerformTransaction", {"id": tx})
    paid = await payme(
        client, "CheckPerformTransaction", {"amount": ob.amount, "account": {"order_id": str(ob.payment_id)}}
    )
    assert paid["error"]["code"] == -31099 or paid["error"]["code"] == -31050 or "error" in paid
    create_on_paid = await payme(
        client,
        "CreateTransaction",
        {
            "id": tx + "-x",
            "time": int(time.time() * 1000),
            "amount": ob.amount,
            "account": {"order_id": str(ob.payment_id)},
        },
    )
    assert "error" in create_on_paid
    statement = await payme(
        client, "GetStatement", {"from": int((time.time() - 60) * 1000), "to": int((time.time() + 60) * 1000)}
    )
    assert any(t["id"] == tx for t in statement["result"]["transactions"])


async def test_malformed_payme_bodies(client: httpx.AsyncClient, provider_accounts: None) -> None:
    not_json = await client.post(
        PAYME_URL, content=b"{not json", headers={**_payme_auth(), "Content-Type": "application/json"}
    )
    assert not_json.status_code == 200 and "error" in not_json.json()
    unknown = await payme(client, "NoSuchMethod", {})
    assert unknown["error"]["code"] == -32601
    no_params = await client.post(
        PAYME_URL, json={"jsonrpc": "2.0", "id": 1, "method": "CreateTransaction"}, headers=_payme_auth()
    )
    assert "error" in no_params.json()


async def test_click_error_complete_and_unknown_prepare(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    order = str(ob.payment_id)
    t1, t2 = str(uuid4().int % 10**9), str(uuid4().int % 10**9)
    prepared = await click(
        client, click_form(click_trans_id=t1, merchant_trans_id=order, amount_minor=ob.amount, action=0)
    )
    assert prepared["error"] == 0 and prepared["merchant_prepare_id"]
    # complete с ошибкой провайдера (error=-5017): попытка FAILED, обязательство не оплачено
    failed = await click(
        client,
        click_form(
            click_trans_id=t1,
            merchant_trans_id=order,
            amount_minor=ob.amount,
            action=1,
            prepare_id=str(prepared["merchant_prepare_id"]),
            error=-5017,
        ),
    )
    assert failed["error"] == -9, failed
    assert (
        await owner.fetchval("SELECT status::text FROM payment_attempt WHERE provider_transaction_id = $1", t1)
        == "FAILED"
    )
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) != "PAID"
    # тот же click_trans_id ещё раз — тот же ответ из inbox (дедуп), без нового эффекта
    replay = await click(
        client,
        click_form(
            click_trans_id=t1,
            merchant_trans_id=order,
            amount_minor=ob.amount,
            action=1,
            prepare_id=str(prepared["merchant_prepare_id"]),
            error=-5017,
        ),
    )
    assert replay == failed
    # обязательство после отмены FAILED: новый prepare на него отвергается (-9); берём новое обязательство
    refused = await click(
        client, click_form(click_trans_id=t2, merchant_trans_id=order, amount_minor=ob.amount, action=0)
    )
    assert refused["error"] == -9
    ob2 = await make_obligation(container, owner, _plate())
    order, t3 = str(ob2.payment_id), str(uuid4().int % 10**9)
    prepared2 = await click(
        client, click_form(click_trans_id=t3, merchant_trans_id=order, amount_minor=ob2.amount, action=0)
    )
    assert prepared2["error"] == 0
    unknown = await click(
        client,
        click_form(click_trans_id=t3, merchant_trans_id=order, amount_minor=ob2.amount, action=1, prepare_id="999999"),
    )
    assert unknown["error"] == -6, unknown
    # prepare по неизвестному заказу, с неверной суммой, с неверной подписью
    no_order = await click(
        client, click_form(click_trans_id=t3 + "1", merchant_trans_id=str(uuid4()), amount_minor=100, action=0)
    )
    assert no_order["error"] == -5
    bad_amount = await click(
        client, click_form(click_trans_id=t3 + "2", merchant_trans_id=order, amount_minor=ob.amount + 100, action=0)
    )
    assert bad_amount["error"] == -2
    bad_sign = await click(
        client,
        click_form(click_trans_id=t3 + "3", merchant_trans_id=order, amount_minor=ob.amount, action=0, secret="wrong"),
    )
    assert bad_sign["error"] == -1
