"""Фаза 10 §3: запрос субъекта ПДн проходит весь цикл REQUESTED → … → COMPLETED, после чего номер не восстановим
ни из БД, ни из MinIO (все версии объекта), ни из логов (I-10), ни из бэкапа новее даты удаления (pg_dump после
удаления не содержит ни номера, ни старого хеша), а финансовая отчётность за период не меняется."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from parking.modules.devices.application.commands import BarrierCommandExecutor
from parking.modules.identity.application.contracts import CurrentUser, Role
from parking.modules.media.domain.object_key import build_key
from parking.modules.parking.application.contracts import SagaState
from parking.modules.privacy.application.contracts import SLA_DAYS, DeletionState
from parking.modules.privacy.application.service import ANONYMIZED_PLATE
from parking.runtime.container import Container
from parking.shared.errors import ConflictError
from parking.shared.plate import PlateNumber
from tests.conftest import CAM_A1_IN, LOT_A1, TENANT_A, USER_A_ADMIN
from tests.observability.logcapture import capture_logs
from tests.support.api_fixtures import Principal
from tests.support.billing_fixtures import as_user, make_obligation
from tests.support.fakes import FakeBarrierDriver

ADMIN = CurrentUser(user_id=USER_A_ADMIN, tenant_id=TENANT_A, role=Role.TENANT_ADMIN, lot_scope=frozenset())
PGBIN = Path(__import__("pgserver").__file__).parent / "pginstall" / "bin"


def _plate() -> PlateNumber:
    return PlateNumber.parse(f"01P{uuid4().int % 900 + 100}PD")


async def _revenue(owner: asyncpg.Connection) -> tuple[int, int, int]:
    return (
        int(
            await owner.fetchval(
                "SELECT coalesce(sum(amount), 0) FROM payment WHERE status = 'PAID' AND tenant_id = $1", TENANT_A
            )
        ),
        int(await owner.fetchval("SELECT count(*) FROM payment WHERE tenant_id = $1", TENANT_A)),
        int(await owner.fetchval("SELECT count(*) FROM parking_session WHERE tenant_id = $1", TENANT_A)),
    )


async def _plate_in_dump(test_dsn: str, needles: list[bytes]) -> dict[str, bool]:
    """«Бэкап новее даты удаления»: pg_dump данных не содержит номер и старый хеш ни в одной таблице."""
    dump = (
        await asyncio.to_thread(
            subprocess.run,
            [str(PGBIN / "pg_dump"), "--data-only", "--no-owner", "--dbname", test_dsn],
            capture_output=True,
            check=True,
        )
    ).stdout
    return {n.decode(errors="replace"): n in dump for n in needles}


@pytest.fixture
def cashier(users: dict[Role, Principal]) -> CurrentUser:
    return as_user(users[Role.CASHIER])


async def test_erasure_request_full_cycle_is_irreversible_and_keeps_finance(  # noqa: PLR0915 — сквозной сценарий
    container: Container,
    owner: asyncpg.Connection,
    minio: str,
    test_dsn: str,
    cashier: CurrentUser,
    executor: tuple[BarrierCommandExecutor, FakeBarrierDriver],
    clean_lot: None,
) -> None:
    plate = _plate()
    plate_hash = container.plate_crypto.hash(TENANT_A, plate)
    hash_hex = plate_hash.hex().encode()
    # --- субъект: сессия с оплаченным обязательством и изображением въезда в MinIO ------------------------------
    ob = await make_obligation(container, owner, plate)
    await container.cashier.open_shift(
        cashier, LOT_A1, opening_float=0, notes="privacy test"
    ) if not await container.cashier.current_shift(cashier) else None
    paid = await container.cashier.confirm_cash(cashier, ob.payment_id, declared_amount=ob.amount, reason=None)
    assert paid.status.value == "PAID"
    # выезд завершён: сага открывает шлагбаум и закрывает сессию (активная сессия блокирует удаление)
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.BARRIER_OPENING
    saga = await container.parking.get_saga_by_id(TENANT_A, ob.saga_id)
    assert saga.barrier_command_id is not None
    await executor[0].execute(TENANT_A, saga.barrier_command_id)
    assert await container.parking.advance_saga(TENANT_A, ob.saga_id) is SagaState.COMPLETED
    key = build_key(TENANT_A, LOT_A1, CAM_A1_IN, datetime.now(tz=UTC), uuid4())
    await container.object_store.put_object(key, b"\xff\xd8frame-1")
    await container.object_store.put_object(key, b"\xff\xd8frame-2")  # вторая версия: delete marker недостаточно
    event_id = uuid4()  # событие въезда с изображением (make_obligation идёт мимо ingest, событие вставляем сами)
    encrypted, enc_version = container.plate_crypto.encrypt(TENANT_A, plate)
    await owner.execute(
        "INSERT INTO anpr_event (id, event_time, tenant_id, lot_id, camera_id, event_type, direction, plate_hash, "
        "plate_encrypted, enc_key_version, confidence, image_object_key, result, session_id, idempotency_key, correlation_id) "
        "VALUES ($1, now(), $2, $3, $4, 'ENTRY', 'IN', $5, $6, $7, 95, $8, 'ENTRY_CONFIRMED', $9, $10, $11)",
        event_id,
        TENANT_A,
        LOT_A1,
        CAM_A1_IN,
        plate_hash,
        encrypted,
        enc_version,
        key,
        ob.session_id,
        uuid4().bytes * 2,
        uuid4(),
    )
    await owner.execute(
        "INSERT INTO media_object (object_key, tenant_id, lot_id, camera_id, event_id, session_id, status, delivered_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, 'DELIVERED', now())",
        key,
        TENANT_A,
        LOT_A1,
        CAM_A1_IN,
        event_id,
        ob.session_id,
    )
    s3 = await container.object_store._s3()
    versions = await s3.list_object_versions(Bucket=container.settings.s3_bucket, Prefix=key)
    assert len(versions.get("Versions", [])) == 2, "бакет версионируемый — две версии кадра"
    revenue_before = await _revenue(owner)
    assert (await _plate_in_dump(test_dsn, [hash_hex]))[hash_hex.decode()] is True, "до удаления хеш в дампе есть"
    # v2: марка/модель/цвет привязаны к номеру — стираются вместе с ним
    await owner.execute(
        "UPDATE vehicle SET make = 'Chevrolet', model = 'Cobalt', color = 'white' "
        "WHERE id = (SELECT vehicle_id FROM parking_session WHERE id = $1)",
        ob.session_id,
    )

    # --- цикл запроса ---------------------------------------------------------------------------------------------
    with capture_logs() as logs:
        req = await container.privacy.request(
            ADMIN, plate, legal_basis="SUBJECT_REQUEST", contact="+998 90 000-00-00", reason="заявление субъекта"
        )
        assert req.state is DeletionState.REQUESTED
        assert (req.due_at - req.requested_at).days == SLA_DAYS
        with pytest.raises(ConflictError):  # без подтверждения личности цикл не идёт
            await container.privacy.process(ADMIN, req.id)
        await container.privacy.verify_identity(ADMIN, req.id, reason="техпаспорт ТС сверен")
        done = await container.privacy.process(ADMIN, req.id)
    assert done.state is DeletionState.COMPLETED and done.anonymized_at and done.media_deleted_at and done.completed_at
    assert "ретеншен 7 лет" in (done.retention_note or "")

    # --- номер не восстановим -------------------------------------------------------------------------------------
    assert await owner.fetchval("SELECT count(*) FROM vehicle WHERE plate_hash = $1", plate_hash) == 0
    vehicle_id = await owner.fetchval("SELECT vehicle_id FROM parking_session WHERE id = $1", ob.session_id)
    row = await owner.fetchrow(
        "SELECT plate_hash, plate_encrypted, enc_key_version, anonymized_at, make, model, color FROM vehicle WHERE id = $1",
        vehicle_id,
    )
    assert row is not None and (row["make"], row["model"], row["color"]) == (None, None, None)
    assert row is not None and row["anonymized_at"] is not None
    assert (
        container.plate_crypto.decrypt(TENANT_A, bytes(row["plate_encrypted"]), row["enc_key_version"])
        == ANONYMIZED_PLATE
    )
    assert await owner.fetchval("SELECT count(*) FROM anpr_event WHERE plate_hash = $1", plate_hash) == 0
    events = await owner.fetch(
        "SELECT plate_encrypted, enc_key_version FROM anpr_event WHERE session_id = $1", ob.session_id
    )
    assert events and all(
        container.plate_crypto.decrypt(TENANT_A, bytes(e["plate_encrypted"]), e["enc_key_version"]) == ANONYMIZED_PLATE
        for e in events
    )
    assert await owner.fetchval(
        "SELECT subject_plate_hash <> $1 AND subject_contact_encrypted IS NULL FROM deletion_request WHERE id = $2",
        plate_hash,
        req.id,
    )
    assert await container.parking.find_by_plate(ADMIN, plate) == [], "поиск по номеру больше ничего не находит"
    versions = await s3.list_object_versions(Bucket=container.settings.s3_bucket, Prefix=key)
    assert not versions.get("Versions") and not versions.get("DeleteMarkers"), (
        "в MinIO не осталось ни версии, ни маркера"
    )
    assert await owner.fetchval("SELECT deleted_at IS NOT NULL FROM media_object WHERE object_key = $1", key)
    # логи всего цикла: ни номера, ни хеша (I-10)
    text = "\n".join(str(entry) for entry in logs)
    assert plate.value not in text and plate_hash.hex() not in text
    # бэкап новее даты удаления: дамп без номера и без старого хеша
    found = await _plate_in_dump(test_dsn, [plate.value.encode(), hash_hex])
    assert found == {plate.value: False, hash_hex.decode(): False}, found
    # финансовая отчётность за период не изменилась
    assert await _revenue(owner) == revenue_before
    audit = await owner.fetch(
        "SELECT action, payload::text AS payload FROM audit_log WHERE entity_id = $1 ORDER BY created_at", req.id
    )
    assert [a["action"] for a in audit] == ["PRIVACY_REQUEST_OPENED", "PRIVACY_IDENTITY_VERIFIED", "PRIVACY_ERASURE"]
    assert all(plate.value not in a["payload"] for a in audit)
    print(
        f"\nGATE privacy: REQUESTED→IDENTITY_VERIFIED→LEGAL_RETENTION_CHECK→ANONYMIZATION→MEDIA_DELETION→AUDIT→COMPLETED; "
        f"vehicle/events анонимизированы, MinIO {key.split('/')[-1]} — 0 версий, дамп БД без номера/хеша, выручка {revenue_before[0]} неизменна"
    )


async def test_active_session_blocks_erasure_until_closed(
    container: Container, owner: asyncpg.Connection, clean_lot: None
) -> None:
    from tests.support.parking_fixtures import entry_cmd

    plate = _plate()
    entered = await container.parking.process_entry(entry_cmd(plate, at=datetime.now(tz=UTC)))
    assert entered.session_id is not None
    req = await container.privacy.request(ADMIN, plate, legal_basis="SUBJECT_REQUEST", contact=None, reason="заявление")
    await container.privacy.verify_identity(ADMIN, req.id, reason="документ сверен")
    with pytest.raises(ConflictError, match="active parking session"):
        await container.privacy.process(ADMIN, req.id)
    assert (await container.privacy.get(ADMIN, req.id)).state is DeletionState.IDENTITY_VERIFIED
    rejected = await container.privacy.reject(ADMIN, req.id, reason="субъект отозвал заявление")
    assert rejected.state is DeletionState.REJECTED and rejected.rejected_reason
