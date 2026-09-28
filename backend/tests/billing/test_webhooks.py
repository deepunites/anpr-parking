"""Вебхуки/колбэки провайдеров через API (фаза 5 §3): подпись, окно времени, дедуп ×5, orphan, двойная оплата."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx

from parking.modules.parking.application.contracts import SagaState
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import LOT_A1, TENANT_A
from tests.support.billing_fixtures import CLICK_SECRET, CLICK_SERVICE_ID, PAYME_KEY, PAYME_MERCHANT, make_obligation

PAYME_URL = f"/api/v1/payments/webhook/payme?merchant={PAYME_MERCHANT}"
CLICK_URL = "/api/v1/payments/webhook/click"


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01W{uuid4().hex[:5].upper()}")


def _payme_auth(key: str = PAYME_KEY) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"Paycom:{key}".encode()).decode()}


async def payme(
    client: httpx.AsyncClient, method: str, params: dict[str, Any], *, key: str = PAYME_KEY, rpc_id: int = 1
) -> dict[str, Any]:
    response = await client.post(
        PAYME_URL, json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}, headers=_payme_auth(key)
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def click_form(
    *,
    click_trans_id: str,
    merchant_trans_id: str,
    amount_minor: int,
    action: int,
    prepare_id: str | None = None,
    error: int = 0,
    secret: str = CLICK_SECRET,
    sign_time: str | None = None,
) -> dict[str, str]:
    sign_time = sign_time or datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    amount = f"{amount_minor / 100:.2f}"
    raw = f"{click_trans_id}{CLICK_SERVICE_ID}{secret}{merchant_trans_id}{prepare_id or '' if action == 1 else ''}{amount}{action}{sign_time}"
    form = {
        "click_trans_id": click_trans_id,
        "service_id": CLICK_SERVICE_ID,
        "click_paydoc_id": "999",
        "merchant_trans_id": merchant_trans_id,
        "amount": amount,
        "action": str(action),
        "error": str(error),
        "error_note": "",
        "sign_time": sign_time,
        "sign_string": hashlib.md5(raw.encode()).hexdigest(),
    }
    if action == 1 and prepare_id is not None:
        form["merchant_prepare_id"] = prepare_id
    return form


async def click(client: httpx.AsyncClient, form: dict[str, str]) -> dict[str, Any]:
    response = await client.post(CLICK_URL, data=form)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_payme_full_flow_and_five_identical_performs(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    """Gate: один вебхук доставлен 5 раз → один эффект (один PAID, одно событие, одинаковые ответы)."""
    ob = await make_obligation(container, owner, _plate())
    account = {"order_id": str(ob.payment_id)}
    check = await payme(client, "CheckPerformTransaction", {"amount": ob.amount, "account": account})
    assert check["result"] == {"allow": True}, check
    wrong_amount = await payme(client, "CheckPerformTransaction", {"amount": ob.amount + 1, "account": account})
    assert wrong_amount["error"]["code"] == -31001
    tx = f"payme-{uuid4().hex[:12]}"
    created = await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": account},
    )
    assert created["result"]["state"] == 1 and created["result"]["transaction"], created
    created_again = await payme(
        client,
        "CreateTransaction",
        {"id": tx, "time": int(time.time() * 1000), "amount": ob.amount, "account": account},
    )
    assert created_again["result"] == created["result"]
    busy = await payme(
        client,
        "CreateTransaction",
        {"id": tx + "-other", "time": int(time.time() * 1000), "amount": ob.amount, "account": account},
    )
    assert busy["error"]["code"] == -31099  # вторая транзакция на тот же заказ
    responses = [await payme(client, "PerformTransaction", {"id": tx}) for _ in range(5)]
    assert all(r["result"]["state"] == 2 for r in responses), responses
    assert len({json.dumps(r["result"], sort_keys=True) for r in responses}) == 1
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
        "SELECT status::text AS status, attempts FROM webhook_inbox WHERE provider = 'PAYME' AND provider_event_id = $1",
        f"PerformTransaction:{tx}",
    )
    assert len(inbox) == 1 and inbox[0]["status"] == "APPLIED"
    status = await payme(client, "CheckTransaction", {"id": tx})
    assert status["result"]["state"] == 2 and status["result"]["perform_time"]
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.BARRIER_OPENING
    statement = await payme(
        client, "GetStatement", {"from": int((time.time() - 3600) * 1000), "to": int((time.time() + 60) * 1000)}
    )
    assert any(t["id"] == tx and t["state"] == 2 for t in statement["result"]["transactions"])
    print("\nGATE webhook ×5: one PAID, one confirmed event, identical responses")


async def test_payme_bad_signature_is_rejected_and_audited(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    before = await owner.fetchval("SELECT count(*) FROM audit_log WHERE action = 'WEBHOOK_REJECTED'")
    bad = await payme(
        client,
        "CheckPerformTransaction",
        {"amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
        key="wrong-key",
    )
    assert bad["error"]["code"] == -32504
    no_auth = await client.post(
        PAYME_URL, json={"method": "CheckPerformTransaction", "params": {"amount": 1, "account": {}}}
    )
    assert no_auth.json()["error"]["code"] == -32504
    assert await owner.fetchval("SELECT count(*) FROM audit_log WHERE action = 'WEBHOOK_REJECTED'") == before + 2
    row = await owner.fetchrow(
        "SELECT reason, actor_type::text AS actor, payload FROM audit_log WHERE action = 'WEBHOOK_REJECTED' ORDER BY created_at DESC LIMIT 1"
    )
    assert row["reason"] == "SIGNATURE" and row["actor"] == "PROVIDER"
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", ob.payment_id) == "PENDING"
    assert (
        await owner.fetchval("SELECT count(*) FROM webhook_inbox WHERE signature_valid = false") == 0
    )  # до inbox не доходит
    print("\nGATE bad signature: -32504, audit WEBHOOK_REJECTED (actor PROVIDER)")


async def test_payme_stale_create_is_replay(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    stale = int((datetime.now(tz=UTC) - timedelta(hours=13)).timestamp() * 1000)
    result = await payme(
        client,
        "CreateTransaction",
        {"id": "old-tx", "time": stale, "amount": ob.amount, "account": {"order_id": str(ob.payment_id)}},
    )
    assert result["error"]["code"] == -31008
    assert await owner.fetchval("SELECT count(*) FROM payment_attempt WHERE payment_id = $1", ob.payment_id) == 0


async def test_click_prepare_complete_and_duplicate_payment_goes_to_refund_queue(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    """§6: оплата прошла дважды (два click_trans_id на один заказ) → второй платёж PAID и в очереди возврата."""
    ob = await make_obligation(container, owner, _plate())
    bad = await click(
        client,
        click_form(
            click_trans_id="c1", merchant_trans_id=str(ob.payment_id), amount_minor=ob.amount, action=0, secret="nope"
        ),
    )
    assert bad["error"] == -1
    prep1 = await click(
        client, click_form(click_trans_id="c1", merchant_trans_id=str(ob.payment_id), amount_minor=ob.amount, action=0)
    )
    assert prep1["error"] == 0 and prep1["merchant_prepare_id"], prep1
    prep2 = await click(
        client, click_form(click_trans_id="c2", merchant_trans_id=str(ob.payment_id), amount_minor=ob.amount, action=0)
    )
    assert prep2["error"] == 0
    done1 = await click(
        client,
        click_form(
            click_trans_id="c1",
            merchant_trans_id=str(ob.payment_id),
            amount_minor=ob.amount,
            action=1,
            prepare_id=str(prep1["merchant_prepare_id"]),
        ),
    )
    assert done1["error"] == 0 and done1["merchant_confirm_id"], done1
    for _ in range(4):  # тот же Complete ещё 4 раза
        assert (
            await click(
                client,
                click_form(
                    click_trans_id="c1",
                    merchant_trans_id=str(ob.payment_id),
                    amount_minor=ob.amount,
                    action=1,
                    prepare_id=str(prep1["merchant_prepare_id"]),
                ),
            )
        )["error"] == 0
    assert (
        await owner.fetchval("SELECT count(*) FROM payment WHERE session_id = $1 AND status = 'PAID'", ob.session_id)
        == 1
    )
    done2 = await click(
        client,
        click_form(
            click_trans_id="c2",
            merchant_trans_id=str(ob.payment_id),
            amount_minor=ob.amount,
            action=1,
            prepare_id=str(prep2["merchant_prepare_id"]),
        ),
    )
    assert done2["error"] == 0  # деньги уже списаны Click'ом — принимаем, но в очередь возврата
    paid = await owner.fetch(
        "SELECT id, idempotency_key FROM payment WHERE session_id = $1 AND status = 'PAID' ORDER BY created_at",
        ob.session_id,
    )
    assert len(paid) == 2 and paid[0]["id"] == ob.payment_id
    disc = await owner.fetchrow(
        "SELECT kind::text AS kind, status::text AS status, payment_id, details FROM reconciliation_discrepancy WHERE payment_id = $1",
        paid[1]["id"],
    )
    assert disc is not None and disc["kind"] == "DUPLICATE_REMOTE" and disc["status"] == "OPEN"
    assert json.loads(disc["details"])["original_payment_id"] == str(ob.payment_id)
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.reconciliation.discrepancy_opened.v1'"
        )
        >= 1
    )
    print("\nGATE duplicate provider payment: second PAID payment + DUPLICATE_REMOTE discrepancy (refund queue)")


async def test_click_orphan_webhook_applied_after_payment_appears(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    """Gate: вебхук пришёл до сохранения платежа → ORPHAN в inbox → применён джобом после появления платежа."""
    future_payment = uuid4()
    ob = await make_obligation(container, owner, _plate())  # реальная сессия/сага для будущего платежа
    early = await click(
        client,
        click_form(
            click_trans_id="c-early",
            merchant_trans_id=str(future_payment),
            amount_minor=ob.amount,
            action=1,
            prepare_id="1",
        ),
    )
    assert early["error"] == -5  # заказ ещё не существует
    inbox = await owner.fetchrow(
        "SELECT status::text AS status, attempts FROM webhook_inbox WHERE provider_event_id = 'c-early:1'"
    )
    assert inbox["status"] == "ORPHAN" and inbox["attempts"] == 1
    assert await container.webhooks.replay_orphans(TENANT_A) == 0
    # платёж «докоммитился»: обязательство с тем самым id (на практике — гонка INSERT payment ↔ вебхук)
    await owner.execute(
        """INSERT INTO payment (id, tenant_id, lot_id, session_id, saga_id, amount, status, method, calculation_snapshot, idempotency_key)
           SELECT $1::uuid, tenant_id, lot_id, session_id, NULL, amount, 'PENDING', NULL, calculation_snapshot, sha256(convert_to($3, 'UTF8'))
           FROM payment WHERE id = $2""",
        future_payment,
        ob.payment_id,
        str(future_payment),
    )
    assert await container.webhooks.replay_orphans(TENANT_A) == 1
    assert await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", future_payment) == "PAID"
    assert (
        await owner.fetchval("SELECT status::text FROM webhook_inbox WHERE provider_event_id = 'c-early:1'")
        == "APPLIED"
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.payment.confirmed.v1' AND aggregate_id = $1",
            future_payment,
        )
        == 1
    )
    print("\nGATE orphan webhook: ORPHAN → APPLIED after payment appeared, one confirmed event")


async def test_click_replay_window(
    client: httpx.AsyncClient, container: Container, owner: asyncpg.Connection, provider_accounts: None, clean_lot: None
) -> None:
    ob = await make_obligation(container, owner, _plate())
    old = (datetime.now(tz=UTC) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    stale = await click(
        client,
        click_form(
            click_trans_id="c-old",
            merchant_trans_id=str(ob.payment_id),
            amount_minor=ob.amount,
            action=0,
            sign_time=old,
        ),
    )
    assert stale["error"] == -8
    assert await owner.fetchval("SELECT count(*) FROM webhook_inbox WHERE provider_event_id = 'c-old:0'") == 0


async def test_unknown_provider_and_merchant(client: httpx.AsyncClient, provider_accounts: None) -> None:
    assert (await client.post("/api/v1/payments/webhook/stripe", json={})).status_code == 404
    response = await client.post(
        "/api/v1/payments/webhook/payme?merchant=nobody",
        json={"method": "CheckPerformTransaction", "params": {}},
        headers=_payme_auth(),
    )
    assert response.json()["error"]["code"] == -32504
    assert UUID(int=0) is not None and LOT_A1 is not None
