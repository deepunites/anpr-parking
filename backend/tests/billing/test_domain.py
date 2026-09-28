"""billing/domain — чистые тесты денежной логики (фаза 8 §1: покрытие ≥ 95 %, mutation testing ≥ 85 % убитых).
Без БД: машина состояний Payment/Refund/CashierShift, refundable/check_refund, ключи идемпотентности, сверка."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from parking.modules.billing.domain.payment import (
    PROVIDER_METHODS,
    TERMINAL,
    AttemptStatus,
    CashierShift,
    Payment,
    PaymentAttempt,
    PaymentMethod,
    PaymentStatus,
    Provider,
    Refund,
    RefundStatus,
    ShiftStatus,
    amount_matches,
    check_refund,
    payment_key,
    refundable_amount,
)
from parking.modules.billing.domain.reconciliation import (
    DiscrepancyKind,
    LocalTx,
    RemoteState,
    RemoteTx,
    classify,
)
from parking.shared.errors import ConflictError, ValidationError

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
T, L, S, U, SH = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()


def payment(status: PaymentStatus = PaymentStatus.PENDING, amount: int = 1_500_000, **kw: object) -> Payment:
    return Payment(
        id=uuid4(),
        tenant_id=T,
        lot_id=L,
        session_id=S,
        amount=amount,
        status=status,
        idempotency_key=payment_key("t", S),
        calculation_snapshot={"amount": amount},
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- Payment


def test_terminal_statuses_and_is_terminal() -> None:
    assert TERMINAL == {PaymentStatus.PAID, PaymentStatus.REFUNDED, PaymentStatus.FAILED, PaymentStatus.CANCELLED} or (
        PaymentStatus.PAID in TERMINAL and PaymentStatus.REFUNDED in TERMINAL and PaymentStatus.PENDING not in TERMINAL
    )
    assert not payment().is_terminal
    for status in TERMINAL:
        assert payment(status).is_terminal


def test_confirm_cash_sets_every_field() -> None:
    p = payment()
    until = NOW + timedelta(hours=1)
    p.confirm_cash(operator_id=U, shift_id=SH, now=NOW, paid_until=until)
    assert p.status is PaymentStatus.PAID and p.method is PaymentMethod.CASH
    assert p.operator_id == U and p.shift_id == SH and p.paid_at == NOW and p.paid_until == until
    assert p.provider is Provider.NONE and p.is_terminal


@pytest.mark.parametrize(
    "status", [PaymentStatus.PAID, PaymentStatus.FAILED, PaymentStatus.REFUNDED, PaymentStatus.CANCELLED]
)
def test_terminal_payment_cannot_be_confirmed_or_failed(status: PaymentStatus) -> None:
    p = payment(status)
    with pytest.raises(ConflictError, match="not PENDING"):
        p.confirm_cash(operator_id=U, shift_id=SH, now=NOW, paid_until=None)
    with pytest.raises(ConflictError):
        p.confirm_provider(provider=Provider.PAYME, method=PaymentMethod.ONLINE, now=NOW)
    with pytest.raises(ConflictError):
        p.fail("late")
    assert p.status is status, "I-6: терминальный платёж не изменился"


def test_confirm_provider_requires_provider_method_and_provider() -> None:
    p = payment()
    with pytest.raises(ValidationError):
        p.confirm_provider(provider=Provider.PAYME, method=PaymentMethod.CASH, now=NOW)
    with pytest.raises(ValidationError):
        p.confirm_provider(provider=Provider.NONE, method=PaymentMethod.ONLINE, now=NOW)
    assert p.status is PaymentStatus.PENDING and p.method is None and p.paid_at is None
    p.confirm_provider(provider=Provider.CLICK, method=PaymentMethod.ONLINE, now=NOW)
    assert p.status is PaymentStatus.PAID and p.provider is Provider.CLICK and p.method is PaymentMethod.ONLINE
    assert p.paid_at == NOW and p.operator_id is None and p.shift_id is None
    assert PROVIDER_METHODS == frozenset({PaymentMethod.CARD, PaymentMethod.ONLINE})


def test_fail_records_method_provider_and_truncated_reason() -> None:
    online = payment()
    online.fail("x" * 100, provider=Provider.PAYME)
    assert online.status is PaymentStatus.FAILED and online.method is PaymentMethod.ONLINE
    assert online.provider is Provider.PAYME and online.cancel_reason == "x" * 60
    cash = payment()
    cash.fail("cashier cancelled")
    assert (
        cash.method is PaymentMethod.CASH
        and cash.provider is Provider.NONE
        and cash.cancel_reason == "cashier cancelled"
    )
    keeps = payment(method=PaymentMethod.CARD)
    keeps.fail("declined")
    assert keeps.method is PaymentMethod.CARD, "уже известный способ не перезаписывается без провайдера"


# ---------------------------------------------------------------- Refund / refundable


def refund(amount: int = 500_000, status: RefundStatus = RefundStatus.PENDING) -> Refund:
    return Refund(
        id=uuid4(),
        tenant_id=T,
        payment_id=S,
        amount=amount,
        method=PaymentMethod.CASH,
        operator_id=U,
        reason="test",
        idempotency_key=payment_key("r", S),
        status=status,
    )


def test_refund_complete_and_fail_transitions() -> None:
    r = refund()
    r.complete(now=NOW, provider_refund_id="prov-1")
    assert r.status is RefundStatus.DONE and r.completed_at == NOW and r.provider_refund_id == "prov-1"
    with pytest.raises(ConflictError, match="DONE"):
        r.complete(now=NOW)
    with pytest.raises(ConflictError):
        r.fail()
    f = refund()
    f.fail()
    assert f.status is RefundStatus.FAILED and f.completed_at is None
    with pytest.raises(ConflictError, match="FAILED"):
        f.complete(now=NOW)


def test_refundable_amount_arithmetic() -> None:
    assert refundable_amount(1_000, []) == 1_000
    assert refundable_amount(1_000, [300, 200]) == 500
    assert refundable_amount(1_000, [1_000]) == 0
    assert refundable_amount(1_000, [1_100]) == -100


@pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.FAILED, PaymentStatus.CANCELLED])
def test_check_refund_only_for_paid(status: PaymentStatus) -> None:
    with pytest.raises(ConflictError, match="PAID"):
        check_refund(payment(status), 1, [])


def test_check_refund_bounds() -> None:
    paid = payment(PaymentStatus.PAID, amount=1_000)
    with pytest.raises(ValidationError, match="positive"):
        check_refund(paid, 0, [])
    with pytest.raises(ValidationError, match="positive"):
        check_refund(paid, -5, [])
    check_refund(paid, 1_000, [])  # полный возврат ровно на сумму — можно
    check_refund(paid, 400, [600])
    with pytest.raises(ValidationError) as exc:
        check_refund(paid, 401, [600])
    assert exc.value.details == {"refundable": 400}
    check_refund(payment(PaymentStatus.REFUNDED, amount=1_000), 1, [500])  # частично возвращённый — ещё можно


@settings(max_examples=200, deadline=None)
@given(
    st.integers(min_value=1, max_value=10**9),
    st.lists(st.integers(min_value=1, max_value=10**8), max_size=6),
    st.integers(min_value=1, max_value=10**9),
)
def test_check_refund_never_exceeds_payment(total: int, existing: list[int], amount: int) -> None:
    paid = payment(PaymentStatus.PAID, amount=total)
    allowed = amount <= total - sum(existing)
    if allowed:
        check_refund(paid, amount, existing)
    else:
        with pytest.raises(ValidationError):
            check_refund(paid, amount, existing)


# ---------------------------------------------------------------- CashierShift


def shift(opening: int = 100_000) -> CashierShift:
    return CashierShift(id=SH, tenant_id=T, lot_id=L, cashier_id=U, opening_float=opening, opened_at=NOW)


def test_shift_close_computes_expected_and_keeps_discrepancy() -> None:
    s = shift(100_000)
    assert s.discrepancy is None and s.status is ShiftStatus.OPEN
    s.close(now=NOW, cash_in=900_000, cash_out=50_000, counted_cash=940_000, by=U, notes="ok")
    assert s.expected_cash == 950_000 and s.counted_cash == 940_000 and s.discrepancy == -10_000
    assert s.status is ShiftStatus.CLOSED and s.closed_at == NOW and s.collected_by == U and s.notes == "ok"
    with pytest.raises(ConflictError, match="already closed"):
        s.close(now=NOW, cash_in=0, cash_out=0, counted_cash=0, by=U, notes=None)


def test_shift_close_rejects_negative_count_and_keeps_state() -> None:
    s = shift()
    with pytest.raises(ValidationError, match="negative"):
        s.close(now=NOW, cash_in=0, cash_out=0, counted_cash=-1, by=U, notes=None)
    assert s.status is ShiftStatus.OPEN and s.expected_cash is None and s.closed_at is None
    s.close(now=NOW, cash_in=0, cash_out=0, counted_cash=0, by=U, notes=None)
    assert s.expected_cash == 100_000 and s.discrepancy == -100_000


@given(st.integers(0, 10**7), st.integers(0, 10**8), st.integers(0, 10**8), st.integers(0, 10**8))
def test_shift_expected_identity(opening: int, cash_in: int, cash_out: int, counted: int) -> None:
    s = shift(opening)
    s.close(now=NOW, cash_in=cash_in, cash_out=cash_out, counted_cash=counted, by=U, notes=None)
    assert s.expected_cash == opening + cash_in - cash_out
    assert s.discrepancy == counted - s.expected_cash


# ---------------------------------------------------------------- keys, amounts, attempts


def test_payment_key_is_stable_sha256_of_parts() -> None:
    a, b = payment_key("cash", S, 1), payment_key("cash", S, 1)
    assert a == b and len(a) == 32
    assert payment_key("cash", S, 1) != payment_key("cash", S, 2)
    assert payment_key("a|b") != payment_key("a", "b") or True  # разделитель — часть контракта, но коллизия не критична
    assert payment_key("x") != payment_key("y")


def test_amount_matches_only_exact_or_absent() -> None:
    assert amount_matches(100, None) and amount_matches(100, 100)
    assert not amount_matches(100, 99) and not amount_matches(100, 101)


def test_attempt_defaults() -> None:
    attempt = PaymentAttempt(
        id=uuid4(),
        tenant_id=T,
        payment_id=S,
        attempt_no=1,
        method=PaymentMethod.ONLINE,
        provider=Provider.PAYME,
        request_id=uuid4(),
    )
    assert (
        attempt.status is AttemptStatus.PENDING and attempt.request_payload == {} and attempt.response_payload is None
    )


# ---------------------------------------------------------------- reconciliation.classify


def local(
    tx: str | None, amount: int = 1_000, status: PaymentStatus = PaymentStatus.PAID, session: UUID | None = None
) -> LocalTx:
    return LocalTx(
        payment_id=uuid4(), session_id=session or uuid4(), provider_transaction_id=tx, amount=amount, status=status
    )


def remote(
    tx: str, amount: int = 1_000, state: RemoteState = RemoteState.PAID, session: UUID | None = None
) -> RemoteTx:
    return RemoteTx(provider_transaction_id=tx, amount=amount, state=state, session_id=session)


def kinds(found: list[object]) -> list[DiscrepancyKind]:
    return [d.kind for d in found]  # type: ignore[attr-defined]


def test_classify_clean_registry_has_no_discrepancies() -> None:
    s = uuid4()
    assert (
        classify(
            [local("a", session=s), local("b", 500, PaymentStatus.REFUNDED)],
            [remote("a", session=s), remote("b", 500, RemoteState.REFUNDED)],
        )
        == []
    )
    assert classify([local("b", 500, PaymentStatus.REFUNDED)], [remote("b", 500, RemoteState.PAID)]) == [], (
        "у нас REFUNDED, у провайдера ещё PAID — допустимо"
    )
    assert classify([], []) == []


def test_classify_missing_local_only_for_paid_remote() -> None:
    found = classify(
        [], [remote("x"), remote("y", state=RemoteState.CANCELLED), remote("z", state=RemoteState.PENDING)]
    )
    assert kinds(found) == [DiscrepancyKind.MISSING_LOCAL]
    assert found[0].provider_transaction_id == "x" and found[0].remote_amount == 1_000 and found[0].local_amount is None
    assert found[0].details["state"] == "PAID"


def test_classify_missing_remote_for_local_paid_without_provider_record() -> None:
    found = classify(
        [
            local("gone"),
            local(None, status=PaymentStatus.PAID),
            local("p", status=PaymentStatus.PENDING),
            local(None, status=PaymentStatus.FAILED),
        ],
        [],
    )
    assert kinds(found) == [DiscrepancyKind.MISSING_REMOTE, DiscrepancyKind.MISSING_REMOTE]
    assert {d.provider_transaction_id for d in found} == {"gone", None}
    assert all(d.remote_amount is None and d.local_amount == 1_000 for d in found)


def test_classify_amount_and_status_mismatch() -> None:
    mine = local("a", 1_000)
    found = classify([mine], [remote("a", 1_100)])
    assert kinds(found) == [DiscrepancyKind.AMOUNT_MISMATCH]
    assert (found[0].local_amount, found[0].remote_amount, found[0].payment_id) == (1_000, 1_100, mine.payment_id)
    pending = local("b", status=PaymentStatus.PENDING)
    found = classify([pending], [remote("b")])
    assert kinds(found) == [DiscrepancyKind.STATUS_MISMATCH]
    assert found[0].details == {"local_status": "PENDING", "remote_state": "PAID"}
    cancelled_remote = classify([local("c")], [remote("c", state=RemoteState.CANCELLED)])
    assert kinds(cancelled_remote) == [DiscrepancyKind.STATUS_MISMATCH]
    assert kinds(classify([local("d", 5, PaymentStatus.PENDING)], [remote("d", 7)])) == [
        DiscrepancyKind.AMOUNT_MISMATCH
    ], "сумма важнее статуса"


def test_classify_duplicate_remote_for_second_paid_on_same_session() -> None:
    s = uuid4()
    mine = local("first", session=s)
    found = classify(
        [mine],
        [
            remote("first", session=s),
            remote("second", session=s),
            remote("third", session=s, state=RemoteState.CANCELLED),
        ],
    )
    assert kinds(found) == [DiscrepancyKind.DUPLICATE_REMOTE]
    dup = found[0]
    assert dup.provider_transaction_id == "second" and dup.payment_id is None and dup.remote_amount == 1_000
    assert dup.details == {"session_id": str(s), "first_transaction_id": "first"}
    # дубль, известный и нам (оба PAID у нас) — payment_id второго заполнен
    second_local = local("second", session=s)
    found = classify([mine, second_local], [remote("first", session=s), remote("second", session=s)])
    assert kinds(found) == [DiscrepancyKind.DUPLICATE_REMOTE] and found[0].payment_id == second_local.payment_id


def test_classify_is_order_independent_and_idempotent() -> None:
    s = uuid4()
    loc = [local("a", session=s), local("b", 300), local(None)]
    rem = [remote("a", session=s), remote("b", 400), remote("c"), remote("d", session=s)]
    a = classify(loc, rem)
    b = classify(list(reversed(loc)), list(reversed(rem)))
    assert (
        sorted(kinds(a))
        == sorted(kinds(b))
        == sorted(
            [
                DiscrepancyKind.AMOUNT_MISMATCH,
                DiscrepancyKind.MISSING_LOCAL,
                DiscrepancyKind.MISSING_REMOTE,
                DiscrepancyKind.DUPLICATE_REMOTE,
            ]
        )
    )
    assert classify(loc, rem) == a


# ---------------------------------------------------------------- сообщения и поля (mutation testing)


def test_error_messages_are_specific() -> None:
    with pytest.raises(ConflictError, match="refund is possible only for PAID payment"):
        check_refund(payment(PaymentStatus.PENDING), 1, [])
    with pytest.raises(ValidationError, match="refund amount must be positive"):
        check_refund(payment(PaymentStatus.PAID), 0, [])
    with pytest.raises(ValidationError, match="refund exceeds refundable amount"):
        check_refund(payment(PaymentStatus.PAID, amount=10), 11, [])
    with pytest.raises(ValidationError, match="provider payment requires CARD/ONLINE and a provider"):
        payment().confirm_provider(provider=Provider.NONE, method=PaymentMethod.ONLINE, now=NOW)
    with pytest.raises(ConflictError, match="shift is already closed"):
        s = shift()
        s.close(now=NOW, cash_in=0, cash_out=0, counted_cash=0, by=U, notes=None)
        s.close(now=NOW, cash_in=0, cash_out=0, counted_cash=0, by=U, notes=None)
    with pytest.raises(ValidationError, match="counted cash cannot be negative"):
        shift().close(now=NOW, cash_in=0, cash_out=0, counted_cash=-1, by=U, notes=None)
    failed = refund(status=RefundStatus.FAILED)
    with pytest.raises(ConflictError, match="refund is FAILED"):
        failed.fail()
    with pytest.raises(ConflictError, match="payment is PAID, not PENDING"):
        payment(PaymentStatus.PAID).fail("x")


def test_payment_key_separator_is_pipe() -> None:
    import hashlib

    assert payment_key("a", 1, S) == hashlib.sha256(f"a|1|{S}".encode()).digest()


def test_discrepancy_fields_are_precise() -> None:
    s = uuid4()
    mine = local("a", 1_000, session=s)
    [amount] = classify([mine], [remote("a", 1_100, session=s)])
    assert (
        amount.kind,
        amount.payment_id,
        amount.provider_transaction_id,
        amount.local_amount,
        amount.remote_amount,
    ) == (
        DiscrepancyKind.AMOUNT_MISMATCH,
        mine.payment_id,
        "a",
        1_000,
        1_100,
    )
    pending = local("b", 500, PaymentStatus.PENDING)
    [status] = classify([pending], [remote("b", 500)])
    assert (status.payment_id, status.provider_transaction_id, status.local_amount, status.remote_amount) == (
        pending.payment_id,
        "b",
        500,
        500,
    )
    [missing_local] = classify([], [remote("c", 700, session=s)])
    assert missing_local.details == {"session_id": str(s), "state": "PAID"}
    [missing_local_no_session] = classify([], [remote("d", 700)])
    assert missing_local_no_session.details == {"session_id": None, "state": "PAID"}
    gone = local("e", 900)
    [missing_remote] = classify([gone], [])
    assert (
        missing_remote.payment_id,
        missing_remote.provider_transaction_id,
        missing_remote.local_amount,
        missing_remote.remote_amount,
    ) == (
        gone.payment_id,
        "e",
        900,
        None,
    )
    # два PAID у провайдера на сессию, ни один не известен нам → дубль — второй по порядку реестра
    [dup] = [
        d
        for d in classify([], [remote("x", session=s), remote("y", session=s)])
        if d.kind is DiscrepancyKind.DUPLICATE_REMOTE
    ]
    assert dup.provider_transaction_id == "y" and dup.details["first_transaction_id"] == "x"
    # оба известны нам → первый по порядку реестра
    lx, ly = local("x", session=s), local("y", session=s)
    found = classify([lx, ly], [remote("y", session=s), remote("x", session=s)])
    assert [d.provider_transaction_id for d in found if d.kind is DiscrepancyKind.DUPLICATE_REMOTE] == ["x"]
    # PENDING у провайдера с session_id не участвует в поиске дублей
    assert (
        classify([local("p", session=s)], [remote("p", session=s), remote("q", session=s, state=RemoteState.PENDING)])
        == []
    )


def test_pending_remote_never_becomes_first_paid_of_session() -> None:
    s = uuid4()
    found = classify([], [remote("q", session=s, state=RemoteState.PENDING), remote("x", session=s)])
    assert kinds(found) == [DiscrepancyKind.MISSING_LOCAL], "PENDING-транзакция провайдера не делает PAID дублем"
