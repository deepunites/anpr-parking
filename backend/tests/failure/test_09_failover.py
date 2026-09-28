"""§5: «Primary упал, standby промотирован → RTO измерен, потери принятых событий нет».
Синхронная реплика (synchronous_standby_names): всё закоммиченное до падения есть на standby;
promote + переключение upstream toxiproxy (в проде — Patroni + HAProxy/VIP)."""

from __future__ import annotations

import time

import httpx
from tests.conftest import _with_db
from tests.failure.lab import FailureLab
from tests.failure.support import db_event_ids, drain_offline, duplicate_sessions, send_batch, wait_until

from parking.runtime.container import Container


async def test_primary_crash_promote_standby_measure_rto(
    lab: FailureLab, client: httpx.AsyncClient, container: Container, entry_key: str, test_dsn: str
) -> None:
    await container.redis.delete("anpr:offline")
    before = await send_batch(client, entry_key, 25)
    assert set(before.statuses()) == {200}
    committed = len(await db_event_ids(_with_db(lab.direct_dsn(lab.primary_port), "parking_test")))

    t0 = time.monotonic()
    lab.crash_primary()  # pg_ctl stop -m immediate: как kill -9 postmaster
    during = await send_batch(client, entry_key, 10, prefix="03")
    lab.promote_standby()
    lab.repoint_proxy(lab.standby_port)
    promoted_at = time.monotonic() - t0

    async def api_writes_again() -> bool:
        resp = await send_batch(client, entry_key, 1, prefix="04")
        return resp.statuses() == [200]

    rto = promoted_at + await wait_until(api_writes_again, limit=60)
    assert lab.psql(lab.standby_port, "SELECT pg_is_in_recovery()").strip() == "f"
    new_primary = _with_db(lab.direct_dsn(lab.standby_port), "parking_test")
    in_db = await db_event_ids(new_primary)
    assert before.accepted <= in_db, "синхронная реплика: ничего из подтверждённого до падения не потеряно"
    assert len(in_db) >= committed
    processed, duplicates = await drain_offline(container)
    in_db = await db_event_ids(new_primary)
    assert during.accepted <= in_db and duplicates == 0
    assert await duplicate_sessions(new_primary) == 0
    assert set(during.statuses()) <= {200, 202}, during.statuses()
    print(
        f"\nGATE failover: primary crashed, standby promoted at +{promoted_at:.1f}s, API writes again at RTO={rto:.1f}s; "
        f"events before crash {len(before.accepted)} — all present on new primary; during outage "
        f"{during.statuses().count(202)}×202 replayed ({processed}), duplicates {duplicates}"
    )
    # лаборатория дальше живёт на новом primary (старый не возвращаем — как после реального failover)
    lab.primary_port, lab.standby_port = lab.standby_port, lab.primary_port
