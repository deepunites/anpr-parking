"""Видео въезда и выезда сессии из записи камер на сервере площадки (deploy/truenas/video.sh): кассир выбирает
сессию по номеру — видит, как машина заезжала и выезжала. Окно — от «до» до «после» момента события камеры.
Права — как у фото сессии (sessions:read, сессия своя и в скоупе площадок); ссылка на ролик подписана и живёт минуты.
Записи нет (архив не подключён, камера не пишется, файлы уже удалены) — ролик помечен недоступным, а не ошибкой."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

import anyio.to_thread

from parking.modules.identity.application.contracts import CurrentUser
from parking.modules.parking.application.contracts import SessionView
from parking.shared.clock import Clock
from parking.shared.errors import NotFoundError

CLIP_PATH = "/api/v1/video/clips/"


@dataclass(frozen=True, slots=True)
class ClipRef:
    """Что показать по ссылке: камера и окно времени (проверены при выдаче ссылки)."""

    session_id: UUID
    camera_id: UUID
    start: datetime
    end: datetime


class VideoArchive(Protocol):
    def segments(self, camera_id: UUID, start: datetime, end: datetime) -> Sequence[object]: ...

    def clip(self, camera_id: UUID, start: datetime, end: datetime) -> Path | None: ...


class ClipTokens(Protocol):
    @property
    def ttl_seconds(self) -> int: ...

    def issue(self, ref: ClipRef, now: datetime) -> str: ...

    def verify(self, token: str, now: datetime) -> ClipRef: ...


@dataclass(frozen=True, slots=True)
class SessionVideo:
    kind: Literal["ENTRY", "EXIT"]
    camera_id: UUID
    event_time: datetime
    start: datetime
    end: datetime
    available: bool
    url: str | None
    expires_in: int


class VideoService:
    def __init__(
        self,
        *,
        archive: VideoArchive | None,
        tokens: ClipTokens,
        session_lookup: Callable[[CurrentUser, UUID], Awaitable[SessionView]],
        clock: Clock,
        before_seconds: int,
        after_seconds: int,
    ) -> None:
        self._archive = archive
        self._tokens = tokens
        self._session_lookup = session_lookup
        self._clock = clock
        self._before = timedelta(seconds=before_seconds)
        self._after = timedelta(seconds=after_seconds)

    @property
    def enabled(self) -> bool:
        return self._archive is not None

    async def session_videos(self, actor: CurrentUser, session_id: UUID) -> list[SessionVideo]:
        session = await self._session_lookup(actor, session_id)  # 404, если сессия чужая или вне скоупа площадок
        moments: list[tuple[Literal["ENTRY", "EXIT"], UUID | None, datetime | None]] = [
            ("ENTRY", session.entry_camera_id, session.entry_time),
            ("EXIT", session.exit_camera_id, session.exit_time),
        ]
        videos: list[SessionVideo] = []
        now = self._clock.now()
        for kind, camera_id, at in moments:
            if camera_id is None or at is None:  # ручной въезд, выезд кассиром или машина ещё на парковке
                continue
            start, end = at - self._before, at + self._after
            available = self._archive is not None and bool(
                await anyio.to_thread.run_sync(self._archive.segments, camera_id, start, end)
            )
            url = None
            if available:
                token = self._tokens.issue(ClipRef(session.id, camera_id, start, end), now)
                url = CLIP_PATH + token
            videos.append(SessionVideo(kind, camera_id, at, start, end, available, url, self._tokens.ttl_seconds))
        return videos

    async def clip(self, token: str) -> Path:
        """Файл ролика по подписанной ссылке (401 — ссылка чужая или устарела, 404 — записи за это время нет)."""
        ref = self._tokens.verify(token, self._clock.now())
        if self._archive is None:
            raise NotFoundError("video archive is not configured")
        path = await anyio.to_thread.run_sync(self._archive.clip, ref.camera_id, ref.start, ref.end)
        if path is None:
            raise NotFoundError("no recording for this moment")
        return path
