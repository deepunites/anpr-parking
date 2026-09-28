"""Сверка (фаза 5 §5): пять классов расхождений на подготовленных данных, полный отчёт, действия по умолчанию, решение."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from parking.modules.billing.application.contracts import (
    DiscrepancyStatus,
    PaymentStatus,
    Provider,
    RemoteState,
    RemoteTx,
)
from parking.modules.billing.domain.reconciliation import DiscrepancyKind, LocalTx, classify
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber
from tests.conftest import TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.billing_fixtures import FakeBilling, as_user, make_obligation


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01Q{uuid4().hex[:5].upper()}")


def test_classify_pure_all_five_kinds() -> None:
    s1, s2, s3, s4 = uuid4(), uuid4(), uuid4(), uuid4()
    p1, p2, p3, p4 = uuid4(), uuid4(), uuid4(), uuid4()
    local = [
        LocalTx(p1, s1, "tx-ok", 1000, PaymentStatus.PAID),
        LocalTx(p2, s2, "tx-amount", 1000, PaymentStatus.PAID),
        LocalTx(p3, s3, "tx-status", 1000, PaymentStatus.PAID),
        LocalTx(p4, s4, "tx-missing-remote", 1000, PaymentStatus.PAID),
    ]
    remote = [
        RemoteTx("tx-ok", 1000, RemoteState.PAID, s1),
        RemoteTx("tx-amount", 1500, RemoteState.PAID, s2),
        RemoteTx("tx-status", 1000, RemoteState.CANCELLED, s3),
        RemoteTx("tx-missing-local", 700, RemoteState.PAID, uuid4()),
        RemoteTx("tx-dup", 1000, RemoteState.PAID, s1),
    ]
    kinds = {(d.kind, d.provider_transaction_id) for d in classify(local, remote)}
    assert kinds == {
        (DiscrepancyKind.AMOUNT_MISMATCH, "tx-amount"),
        (DiscrepancyKind.STATUS_MISMATCH, "tx-status"),
        (DiscrepancyKind.MISSING_LOCAL, "tx-missing-local"),
        (DiscrepancyKind.DUPLICATE_REMOTE, "tx-dup"),
        (DiscrepancyKind.MISSING_REMOTE, "tx-missing-remote"),
    }


async def test_reconciliation_finds_all_kinds_and_stores_full_report(
    container: Container,
    owner: asyncpg.Connection,
    provider_accounts: None,
    fake_billing: FakeBilling,
    cashier: CurrentUser,
    users: dict[Role, Principal],
    client: httpx.AsyncClient,
    clean_lot: None,
) -> None:
    """Gate: сверка находит все 5 классов расхождений на подготовленных данных."""
    manager = as_user(users[Role.MANAGER])
    obs = [await make_obligation(container, owner, _plate()) for _ in range(4)]
    txs = ["r-ok", "r-amount", "r-status", "r-missing-remote"]
    for ob, tx in zip(obs, txs, strict=True):
        await fake_billing.online.checkout(manager, ob.payment_id, Provider.PAYME, return_url=None)
        await owner.execute(
            "UPDATE payment_attempt SET provider_transaction_id = $2, status = 'SUCCEEDED' WHERE payment_id = $1",
            ob.payment_id,
            tx,
        )
        await owner.execute(
            "UPDATE payment SET status = 'PAID', method = 'ONLINE', provider = 'PAYME', paid_at = now() WHERE id = $1",
            ob.payment_id,
        )
    extra = await make_obligation(container, owner, _plate())  # PENDING без транзакции — не участвует (не PAID)
    today = datetime.now(tz=ZoneInfo("Asia/Tashkent")).date()  # реестры провайдеров — по местным суткам
    remote = [
        RemoteTx("r-ok", obs[0].amount, RemoteState.PAID, payment_id=obs[0].payment_id),
        RemoteTx("r-amount", obs[1].amount + 500, RemoteState.PAID, payment_id=obs[1].payment_id),
        RemoteTx("r-status", obs[2].amount, RemoteState.CANCELLED, payment_id=obs[2].payment_id),
        RemoteTx("r-missing-local", extra.amount, RemoteState.PAID, session_id=extra.session_id),
        RemoteTx("r-dup", obs[0].amount, RemoteState.PAID, payment_id=obs[0].payment_id),
    ]
    run = await fake_billing.reconciliation.run(
        TENANT_A, Provider.PAYME, today, today, created_by=manager.user_id, remote=remote
    )
    assert run.local_count >= 4 and run.remote_count == 5 and run.matched_count == 3 and run.discrepancy_count >= 5
    rows = await owner.fetch(
        "SELECT kind::text AS kind, status::text AS status, provider_transaction_id AS tx, details FROM reconciliation_discrepancy WHERE reconciliation_id = $1",
        run.id,
    )
    assert {r["kind"] for r in rows} == {
        "MISSING_LOCAL",
        "MISSING_REMOTE",
        "AMOUNT_MISMATCH",
        "STATUS_MISMATCH",
        "DUPLICATE_REMOTE",
    }
    report = await owner.fetchval("SELECT report FROM payment_reconciliation WHERE id = $1", run.id)
    import json

    report = json.loads(report) if isinstance(report, str) else report
    assert (
        len(report["local"]) >= 4 and len(report["remote"]) == 5 and len(report["discrepancies"]) >= 5
    )  # отчёт целиком
    mine = [
        d
        for d in report["discrepancies"]
        if d["tx"] in {"r-amount", "r-status", "r-missing-local", "r-dup", "r-missing-remote"}
    ]
    assert {d["kind"] for d in mine} == {
        "MISSING_LOCAL",
        "MISSING_REMOTE",
        "AMOUNT_MISMATCH",
        "STATUS_MISMATCH",
        "DUPLICATE_REMOTE",
    }
    # действия по умолчанию
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM payment WHERE session_id = $1 AND status = 'PAID' AND provider = 'PAYME'",
            extra.session_id,
        )
        == 1
    )  # MISSING_LOCAL → платёж создан
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", obs[2].payment_id) == "REFUNDED"
    )  # STATUS_MISMATCH → статус провайдера подтянут компенсирующим возвратом
    assert (
        await owner.fetchval("SELECT status::text FROM payment WHERE id = $1", obs[3].payment_id) == "PAID"
    )  # MISSING_REMOTE → не отменяем
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM outbox WHERE event_type = 'billing.reconciliation.completed.v1' AND aggregate_id = $1",
            run.id,
        )
        == 1
    )
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'RECONCILIATION_RUN' AND entity_id = $1", run.id
        )
        == 1
    )
    # решение оператора через API
    headers = await bearer(client, users[Role.MANAGER])
    listed = await client.get("/api/v1/reconciliation-discrepancies", params={"status": "OPEN"}, headers=headers)
    assert listed.status_code == 200 and len(listed.json()) >= 5
    dup = next(d for d in listed.json() if d["kind"] == "DUPLICATE_REMOTE" and d["reconciliation_id"] == str(run.id))
    resolved = await client.post(
        f"/api/v1/reconciliation-discrepancies/{dup['id']}/resolve",
        json={"status": "RESOLVED", "resolution": "возврат оформлен вручную"},
        headers=headers,
    )
    assert resolved.status_code == 200 and resolved.json()["status"] == "RESOLVED"
    got = await client.get(f"/api/v1/reconciliations/{run.id}", headers=headers)
    assert got.status_code == 200 and got.json()["discrepancy_count"] >= 5
    forbidden = await client.get(
        f"/api/v1/reconciliations/{run.id}", headers=await bearer(client, users[Role.OPERATOR])
    )
    assert forbidden.status_code == 403
    assert DiscrepancyStatus.RESOLVED.value == "RESOLVED"
    print(
        "\nGATE reconciliation: 5 kinds found, full report stored, MISSING_LOCAL→payment created, STATUS_MISMATCH→pulled, MISSING_REMOTE kept"
    )
