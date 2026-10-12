"""Видео сессии: кассир выбирает сессию — видит ролик въезда (и выезда) из записи камер на сервере площадки.
Ролик по подписанной ссылке без заголовка Authorization (тег <video>), перемотка — Range; ссылка не годится как
токен входа и наоборот, подделка и чужая сессия не проходят. Архив не подключён — раздел выключен, а не ошибка."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import av
import httpx
import pytest

from parking.modules.identity.domain.permissions import Role
from parking.modules.media.application.video import ClipRef, VideoService
from parking.modules.media.infrastructure.clip_tokens import JwtClipTokens
from parking.modules.media.infrastructure.video_archive import FileVideoArchive
from parking.platform.config import DevSettings
from parking.runtime.container import Container
from parking.shared.errors import AuthenticationError
from tests.conftest import CAM_A1_IN
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate
from tests.support.video import mark_camera, write_segment

TASHKENT = ZoneInfo("Asia/Tashkent")


def _enable_video(container: Container, settings: DevSettings, root: Path) -> None:
    container.video = VideoService(
        archive=FileVideoArchive(root, TASHKENT, root.parent / "clips"),
        tokens=JwtClipTokens(
            private_key_pem=settings.jwt_private_key.get_secret_value(),
            public_key_pem=settings.jwt_public_key,
            issuer=settings.jwt_issuer,
            ttl=timedelta(minutes=5),
        ),
        session_lookup=container.parking.get_session,
        clock=container.clock,
        before_seconds=10,
        after_seconds=20,
    )


async def _enter(client: httpx.AsyncClient, entry_key: str, at: datetime) -> str:
    entered = await send_event(client, entry_key, event_body(unique_plate(), event_time=at))
    assert entered.status_code == 200 and entered.json()["session_id"], entered.text
    return str(entered.json()["session_id"])


async def test_session_entry_video_plays_by_signed_link(
    client: httpx.AsyncClient,
    container: Container,
    api_settings: DevSettings,
    users: dict[Role, Principal],
    tenant_b_admin: Principal,
    entry_key: str,
    tmp_path: Path,
) -> None:
    root = tmp_path / "video"
    mark_camera(root / "cam-entry", CAM_A1_IN)
    now = datetime.now(UTC).replace(microsecond=0)
    write_segment(root / "cam-entry", now - timedelta(seconds=40), 80, TASHKENT, fps=5, gop=5)
    _enable_video(container, api_settings, root)
    session_id = await _enter(client, entry_key, now)
    operator = await bearer(client, users[Role.OPERATOR])

    listed = await client.get(f"/api/v1/sessions/{session_id}/videos", headers=operator)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["enabled"] is True and [v["kind"] for v in body["videos"]] == ["ENTRY"]  # машина ещё на парковке
    entry = body["videos"][0]
    assert entry["available"] is True and entry["camera_id"] == str(CAM_A1_IN) and entry["expires_in"] == 300
    assert datetime.fromisoformat(entry["end"]) - datetime.fromisoformat(entry["start"]) == timedelta(seconds=30)

    clip = await client.get(entry["url"])  # без Authorization — как тег <video>
    assert clip.status_code == 200, clip.text
    assert clip.headers["content-type"] == "video/mp4" and clip.headers["accept-ranges"] == "bytes"
    with av.open(io.BytesIO(clip.content)) as video:
        frames = sum(1 for _ in video.decode(video.streams.video[0]))
    assert 150 <= frames <= 156  # 30 с по 5 кадров, начало — с ключевого кадра не позже «за 10 с до»
    part = await client.get(entry["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100  # перемотка в плеере

    token = entry["url"].rsplit("/", 1)[1]
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert (await client.get(f"/api/v1/video/clips/{forged}")).status_code == 401
    access = operator["Authorization"].removeprefix("Bearer ")
    assert (await client.get(f"/api/v1/video/clips/{access}")).status_code == 401  # токен входа — не ссылка
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401  # и ссылка — не токен входа

    other = await bearer(client, tenant_b_admin)
    assert (await client.get(f"/api/v1/sessions/{session_id}/videos", headers=other)).status_code == 404


async def test_without_recording_video_is_unavailable_not_an_error(
    client: httpx.AsyncClient,
    container: Container,
    api_settings: DevSettings,
    users: dict[Role, Principal],
    entry_key: str,
    tmp_path: Path,
) -> None:
    admin = await bearer(client, users[Role.TENANT_ADMIN])
    session_id = await _enter(client, entry_key, datetime.now(UTC))

    off = await client.get(f"/api/v1/sessions/{session_id}/videos", headers=admin)  # архив не подключён
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert off.json()["videos"][0]["available"] is False and off.json()["videos"][0]["url"] is None

    mark_camera(tmp_path / "video" / "cam-entry", CAM_A1_IN)  # камера пишется, но не в это время
    _enable_video(container, api_settings, tmp_path / "video")
    gap = await client.get(f"/api/v1/sessions/{session_id}/videos", headers=admin)
    assert gap.json()["enabled"] is True and gap.json()["videos"][0]["available"] is False


def test_expired_clip_link_is_rejected(api_settings: DevSettings) -> None:
    tokens = JwtClipTokens(
        private_key_pem=api_settings.jwt_private_key.get_secret_value(),
        public_key_pem=api_settings.jwt_public_key,
        issuer=api_settings.jwt_issuer,
        ttl=timedelta(minutes=5),
    )
    now = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    ref = ClipRef(UUID(int=1), CAM_A1_IN, now - timedelta(seconds=10), now + timedelta(seconds=20))
    token = tokens.issue(ref, now)
    assert tokens.verify(token, now + timedelta(minutes=4)) == ref
    with pytest.raises(AuthenticationError):
        tokens.verify(token, now + timedelta(minutes=5, seconds=1))
