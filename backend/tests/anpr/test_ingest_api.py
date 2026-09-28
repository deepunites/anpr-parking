"""Camera API ingest (фаза 4 §1): идемпотентность (100 параллельных), время, номер, image key, уверенность, blacklist."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import httpx

from parking.modules.access.application.contracts import AccessKind
from parking.modules.anpr.infrastructure.redis_stores import RedisRateLimiter
from parking.modules.identity.application.contracts import Role
from parking.runtime.container import Container
from tests.conftest import CAM_A1_IN, LOT_A1, TENANT_A, TENANT_B
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate


async def _events_for(owner: asyncpg.Connection, plate_hash: bytes) -> list[asyncpg.Record]:
    return await owner.fetch(
        "SELECT result::text AS result, reject_reason, clock_skew_seconds, late, session_id, queued_offline FROM anpr_event WHERE plate_hash = $1 ORDER BY received_at",
        plate_hash,
    )


async def test_entry_then_exit_happy_path(
    client: httpx.AsyncClient, container: Container, entry_key: str, exit_key: str, owner: asyncpg.Connection
) -> None:
    plate = unique_plate()
    entry = await send_event(client, entry_key, event_body(plate))
    assert entry.status_code == 200, entry.text
    body = entry.json()
    assert body["status"] == "ENTRY_CONFIRMED" and body["duplicate"] is False and body["queued"] is False
    session_id = UUID(body["session_id"])
    assert body["barrier_command_id"] is not None and body["correlation_id"]
    exit_ = await send_event(
        client, exit_key, event_body(plate, event_type="EXIT", camera_code="EXIT_01", event_time=datetime.now(tz=UTC))
    )
    assert exit_.status_code == 200, exit_.text
    assert exit_.json()["status"] in {"PAYMENT_REQUIRED", "EXIT_CONFIRMED"} and exit_.json()["session_id"] == str(
        session_id
    )
    rows = await owner.fetch(
        "SELECT event_type::text AS t, session_id FROM anpr_event WHERE session_id = $1 ORDER BY event_time", session_id
    )
    assert [r["t"] for r in rows] == ["ENTRY", "EXIT"]


async def test_duplicate_returns_same_response_200(client: httpx.AsyncClient, entry_key: str) -> None:
    body = event_body(unique_plate())
    first = await send_event(client, entry_key, body)
    second = await send_event(client, entry_key, body)
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["session_id"] == first.json()["session_id"]
    assert second.json()["status"] == first.json()["status"]


async def test_100_parallel_identical_events_one_session(
    client: httpx.AsyncClient, entry_key: str, owner: asyncpg.Connection, container: Container
) -> None:
    """Gate: 100 параллельных одинаковых запросов → одна сессия, 100 идентичных ответов (session_id, status)."""
    plate = unique_plate()
    body = event_body(plate)
    responses = await asyncio.gather(*(send_event(client, entry_key, body) for _ in range(100)))
    assert {r.status_code for r in responses} == {200}
    payloads = [r.json() for r in responses]
    assert len({p["session_id"] for p in payloads}) == 1 and len({p["status"] for p in payloads}) == 1
    assert sum(1 for p in payloads if not p["duplicate"]) == 1  # ровно один «оригинал», остальные duplicate=true
    session_id = UUID(payloads[0]["session_id"])
    from parking.shared.plate import PlateNumber

    plate_hash = container.plate_crypto.hash(TENANT_A, PlateNumber.parse(plate))
    vehicle = await owner.fetchrow("SELECT id FROM vehicle WHERE plate_hash = $1", plate_hash)
    sessions = await owner.fetch("SELECT id FROM parking_session WHERE vehicle_id = $1", vehicle["id"])
    assert [s["id"] for s in sessions] == [session_id]
    assert await owner.fetchval("SELECT count(*) FROM anpr_event WHERE session_id = $1", session_id) == 1
    assert (
        await owner.fetchval(
            "SELECT count(*) FROM anpr_idempotency WHERE event_id IS NOT NULL AND camera_id = $1 AND created_at > now() - interval '1 minute' AND response->>'session_id' = $2",
            CAM_A1_IN,
            str(session_id),
        )
        == 1
    )
    print(
        f"\nGATE 100 parallel identical events: 1 session {session_id}, responses identical, duplicates={sum(p['duplicate'] for p in payloads)}"
    )


async def test_clock_skew_10_minutes_accepted_and_marked(
    client: httpx.AsyncClient, entry_key: str, owner: asyncpg.Connection, container: Container
) -> None:
    plate = unique_plate()
    ahead = datetime.now(tz=UTC) + timedelta(minutes=10)  # часы камеры спешат на 10 минут
    response = await send_event(client, entry_key, event_body(plate, event_time=ahead))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ENTRY_CONFIRMED"
    from parking.shared.plate import PlateNumber

    rows = await _events_for(owner, container.plate_crypto.hash(TENANT_A, PlateNumber.parse(plate)))
    assert len(rows) == 1 and 590 <= rows[0]["clock_skew_seconds"] <= 610 and rows[0]["late"] is False
    print(f"\nGATE clock skew: accepted, clock_skew_seconds={rows[0]['clock_skew_seconds']} (max 120 → marked)")


async def test_late_event_creates_session_without_barrier(client: httpx.AsyncClient, entry_key: str) -> None:
    behind = datetime.now(tz=UTC) - timedelta(hours=2)  # буфер камеры после обрыва: в окне 24 ч, но старше 10 мин
    response = await send_event(client, entry_key, event_body(unique_plate(), event_time=behind))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LATE_ACCEPTED" and response.json()["barrier_command_id"] is None


async def test_month_old_event_rejected_as_replay(client: httpx.AsyncClient, entry_key: str) -> None:
    old = datetime.now(tz=UTC) - timedelta(days=30)
    response = await send_event(client, entry_key, event_body(unique_plate(), event_time=old))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "REPLAY"
    print("\nGATE replay: month-old event → 422 REPLAY")


async def test_foreign_tenant_image_key_rejected(client: httpx.AsyncClient, entry_key: str) -> None:
    foreign = f"tenant/{TENANT_B}/lot/{LOT_A1}/camera/{CAM_A1_IN}/2026/09/19/{uuid4()}.jpg"
    response = await send_event(client, entry_key, event_body(unique_plate(), image_object_key=foreign))
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_IMAGE_KEY"
    other_camera = f"tenant/{TENANT_A}/lot/{LOT_A1}/camera/{uuid4()}/2026/09/19/{uuid4()}.jpg"
    response = await send_event(client, entry_key, event_body(unique_plate(), image_object_key=other_camera))
    assert response.status_code == 422
    traversal = f"tenant/{TENANT_A}/lot/{LOT_A1}/camera/{CAM_A1_IN}/../../../etc/passwd"
    response = await send_event(client, entry_key, event_body(unique_plate(), image_object_key=traversal))
    assert response.status_code == 422
    own = f"tenant/{TENANT_A}/lot/{LOT_A1}/camera/{CAM_A1_IN}/2026/09/19/{uuid4()}.jpg"
    response = await send_event(client, entry_key, event_body(unique_plate(), image_object_key=own))
    assert response.status_code == 200, response.text
    print("\nGATE image key: foreign tenant/camera/traversal → 422 INVALID_IMAGE_KEY; own key → 200")


async def test_confidence_policy(
    client: httpx.AsyncClient, entry_key: str, owner: asyncpg.Connection, container: Container
) -> None:
    from parking.shared.plate import PlateNumber

    low = unique_plate()
    rejected = await send_event(client, entry_key, event_body(low, confidence=40))
    assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "LOW_CONFIDENCE"
    rows = await _events_for(owner, container.plate_crypto.hash(TENANT_A, PlateNumber.parse(low)))
    assert rows[0]["result"] == "REJECTED" and rows[0]["reject_reason"] == "LOW_CONFIDENCE"  # изображение сохранено
    review = await send_event(client, entry_key, event_body(unique_plate(), confidence=70))
    assert review.status_code == 200 and review.json()["status"] == "MANUAL_REVIEW"
    assert review.json()["barrier_command_id"] is None


async def test_blacklist_hit_never_opens_barrier(
    client: httpx.AsyncClient,
    entry_key: str,
    owner: asyncpg.Connection,
    container: Container,
    users: dict[Role, Principal],
) -> None:
    """Gate + I-8: blacklist → сессия MANUAL_REVIEW, команды шлагбауму нет (проверка по barrier_command)."""
    plate = unique_plate("01B")
    headers = await bearer(client, users[Role.TENANT_ADMIN])
    rule = await client.post(
        "/api/v1/access-rules", json={"plate": plate, "kind": "BLACKLIST", "reason": "stolen car"}, headers=headers
    )
    assert rule.status_code == 201, rule.text
    response = await send_event(client, entry_key, event_body(plate))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ENTRY_DENIED" and response.json()["barrier_command_id"] is None
    session_id = UUID(response.json()["session_id"])
    assert await owner.fetchval("SELECT status::text FROM parking_session WHERE id = $1", session_id) == "MANUAL_REVIEW"
    assert await owner.fetchval("SELECT count(*) FROM barrier_command WHERE session_id = $1", session_id) == 0
    decision = await container.access_resolver.cached(
        TENANT_A,
        LOT_A1,
        container.plate_crypto.hash(
            TENANT_A, __import__("parking.shared.plate", fromlist=["PlateNumber"]).PlateNumber.parse(plate)
        ),
    )
    assert decision is not None and decision.kind is AccessKind.BLACKLIST
    print(f"\nGATE blacklist: ENTRY_DENIED, session {session_id} MANUAL_REVIEW, barrier commands = 0")


async def test_direction_and_camera_code_mismatch(client: httpx.AsyncClient, entry_key: str) -> None:
    wrong_type = await send_event(
        client, entry_key, event_body(unique_plate(), event_type="EXIT", camera_code="ENTRY_01")
    )
    assert wrong_type.status_code == 422 and wrong_type.json()["error"]["code"] == "DIRECTION_MISMATCH"
    wrong_code = await send_event(client, entry_key, event_body(unique_plate(), camera_code="EXIT_01"))
    assert wrong_code.status_code == 422


async def test_body_size_limit_and_invalid_plate(client: httpx.AsyncClient, entry_key: str) -> None:
    big = event_body(unique_plate(), recognition={"provider": "x" * 9000})
    response = await send_event(client, entry_key, big)
    assert response.status_code == 413  # BodyLimitMiddleware: 8 КиБ на /api/v1/camera/events
    bad_plate = await send_event(client, entry_key, event_body("!!"))
    assert bad_plate.status_code == 422


async def test_rate_limiter_sliding_window(container: Container) -> None:
    limiter = RedisRateLimiter(container.redis)
    subject = f"test:{uuid4()}"
    waits = [await limiter.hit(subject, limit=3, window_seconds=60) for _ in range(5)]
    assert waits[:3] == [None, None, None] and all(w is not None and w >= 1 for w in waits[3:])
