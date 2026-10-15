from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from parking.api.deps import PUBLIC, get_container, permission_extra, require
from parking.api.pagination import MAX_LIMIT, Page, decode_cursor, encode_cursor
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.parking.application.contracts import SagaState, SagaView, SessionStatus, SessionView
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber

router = APIRouter(prefix="/api/v1", tags=["sessions"])


class SessionOut(BaseModel):
    id: str
    lot_id: str
    vehicle_id: str
    plate: str | None
    status: SessionStatus
    entry_time: datetime
    exit_time: datetime | None
    billing_minutes: int | None
    exit_kind: str | None
    entry_access_kind: str
    entry_unknown: bool
    entry_inferred: bool
    review_reason: str | None
    closed_reason: str | None
    entry_camera_id: str | None
    exit_camera_id: str | None
    version: int
    entry_manual: bool = False
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_color: str | None = None

    @classmethod
    def of(cls, s: SessionView) -> SessionOut:
        return cls(
            id=str(s.id),
            lot_id=str(s.lot_id),
            vehicle_id=str(s.vehicle_id),
            plate=s.plate,
            status=s.status,
            entry_time=s.entry_time,
            exit_time=s.exit_time,
            billing_minutes=s.billing_minutes,
            exit_kind=s.exit_kind.value if s.exit_kind else None,
            entry_access_kind=s.entry_access_kind,
            entry_unknown=s.entry_unknown,
            entry_inferred=s.entry_inferred,
            review_reason=s.review_reason.value if s.review_reason else None,
            closed_reason=s.closed_reason,
            entry_camera_id=str(s.entry_camera_id) if s.entry_camera_id else None,
            exit_camera_id=str(s.exit_camera_id) if s.exit_camera_id else None,
            version=s.version,
            entry_manual=s.entry_manual,
            vehicle_make=s.vehicle_make,
            vehicle_model=s.vehicle_model,
            vehicle_color=s.vehicle_color,
        )


class SagaOut(BaseModel):
    id: str
    session_id: str
    state: SagaState
    amount_due: int
    payment_id: str | None
    barrier_command_id: str | None
    barrier_result: str | None
    review_reason: str | None
    last_error: str | None
    step_deadline: datetime | None
    calculation: dict[str, Any]

    @classmethod
    def of(cls, g: SagaView) -> SagaOut:
        return cls(
            id=str(g.id),
            session_id=str(g.session_id),
            state=g.state,
            amount_due=g.amount_due,
            payment_id=str(g.payment_id) if g.payment_id else None,
            barrier_command_id=str(g.barrier_command_id) if g.barrier_command_id else None,
            barrier_result=g.barrier_result,
            review_reason=g.review_reason,
            last_error=g.last_error,
            step_deadline=g.step_deadline,
            calculation=g.calculation,
        )


class OccupancyOut(BaseModel):
    lot_id: str
    occupied: int
    capacity: int
    free: int
    ratio: float


class ReasonRequest(BaseModel):
    reason: Reason


class CloseStaleRequest(BaseModel):
    older_than_hours: int = Field(ge=1, le=24 * 365)
    dry_run: bool | None = None  # true — только посчитать, ничего не закрывать
    reason: str | None = Field(default=None, max_length=500)


class CloseStaleOut(BaseModel):
    older_than: datetime
    found: int
    closed: int
    skipped_in_exit: int


class CorrectPlateRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    reason: Reason


@router.get(
    "/lots/{lot_id}/sessions", response_model=Page[SessionOut], openapi_extra=permission_extra(Permission.SESSIONS_READ)
)
async def list_sessions(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
    status: Annotated[list[SessionStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> Page[SessionOut]:
    rows = await container.parking.list_sessions(
        user, lot_id, statuses=tuple(status) if status else None, limit=limit + 1, after=decode_cursor(cursor)
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].entry_time, page[-1].id) if len(rows) > limit and page else None
    return Page(items=[SessionOut.of(s) for s in page], next_cursor=next_cursor)


@router.get(
    "/lots/{lot_id}/occupancy", response_model=OccupancyOut, openapi_extra=permission_extra(Permission.SESSIONS_READ)
)
async def occupancy(
    lot_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> OccupancyOut:
    occupied, capacity = await container.parking.occupancy(user, lot_id)
    return OccupancyOut(
        lot_id=str(lot_id),
        occupied=occupied,
        capacity=capacity,
        free=max(0, capacity - occupied),
        ratio=round(occupied / capacity, 4) if capacity else 0.0,
    )


@router.get(
    "/sessions/search", response_model=list[SessionOut], openapi_extra=permission_extra(Permission.SESSIONS_READ)
)
async def search_sessions(
    plate: Annotated[str, Query(min_length=4, max_length=20)],
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[SessionOut]:
    return [SessionOut.of(s) for s in await container.parking.find_by_plate(user, PlateNumber.parse(plate))]


@router.get(
    "/sessions/review-queue", response_model=list[SessionOut], openapi_extra=permission_extra(Permission.SESSIONS_READ)
)
async def review_queue(
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[SessionOut]:
    return [SessionOut.of(s) for s in await container.parking.review_queue(user)]


@router.get(
    "/sessions/{session_id}", response_model=SessionOut, openapi_extra=permission_extra(Permission.SESSIONS_READ)
)
async def get_session(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    return SessionOut.of(await container.parking.get_session(user, session_id))


class SessionImageOut(BaseModel):
    event_id: str
    object_key: str
    event_time: datetime
    url: str
    expires_in: int


@router.get(
    "/sessions/{session_id}/images",
    response_model=list[SessionImageOut],
    openapi_extra=permission_extra(Permission.SESSIONS_READ),
)
async def session_images(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[SessionImageOut]:
    """Presigned GET на изображения событий сессии (TTL минуты); tenant и скоуп площадок проверяются по сессии."""
    return [
        SessionImageOut(
            event_id=str(i.event_id),
            object_key=i.object_key,
            event_time=i.event_time,
            url=i.url,
            expires_in=i.expires_in,
        )
        for i in await container.media.session_images(user, session_id)
    ]


class SessionVideoOut(BaseModel):
    kind: Literal["ENTRY", "EXIT"]
    camera_id: str
    event_time: datetime
    start: datetime
    end: datetime
    available: bool
    url: str | None
    expires_in: int


class SessionVideosOut(BaseModel):
    enabled: bool  # на сервере есть запись камер; нет — раздел «Видео» не показывается
    videos: list[SessionVideoOut]


@router.get(
    "/sessions/{session_id}/videos",
    response_model=SessionVideosOut,
    openapi_extra=permission_extra(Permission.SESSIONS_READ),
)
async def session_videos(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionVideosOut:
    """Ролики въезда и выезда из записи камер сервера площадки; ссылки подписаны и живут минуты."""
    videos = await container.video.session_videos(user, session_id)
    return SessionVideosOut(
        enabled=container.video.enabled,
        videos=[
            SessionVideoOut(
                kind=v.kind,
                camera_id=str(v.camera_id),
                event_time=v.event_time,
                start=v.start,
                end=v.end,
                available=v.available,
                url=v.url,
                expires_in=v.expires_in,
            )
            for v in videos
        ],
    )


@router.get(
    "/media/images/{token}",
    response_class=Response,
    openapi_extra=PUBLIC,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def media_image(token: str, container: Annotated[Container, Depends(get_container)]) -> Response:
    """Фото номера по ссылке из /sessions/{id}/images, когда хранилище браузеру не видно (сервер площадки)."""
    data = await container.media.image(token)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get(
    "/video/clips/{token}",
    response_class=FileResponse,
    openapi_extra=PUBLIC,
    responses={200: {"content": {"video/mp4": {}}}},
)
async def video_clip(token: str, container: Annotated[Container, Depends(get_container)]) -> FileResponse:
    """Ролик по ссылке из /sessions/{id}/videos: право — подпись в ссылке (тег <video> не шлёт Authorization).
    Перемотка — запросами Range."""
    path = await container.video.clip(token)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=300"})


@router.get(
    "/sessions/{session_id}/saga",
    response_model=SagaOut | None,
    openapi_extra=permission_extra(Permission.SESSIONS_READ),
)
async def get_saga(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> SagaOut | None:
    saga = await container.parking.get_saga(user, session_id)
    return None if saga is None else SagaOut.of(saga)


@router.post(
    "/sessions/{session_id}/correct-plate",
    response_model=SessionOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE),
)
async def correct_plate(
    session_id: UUID,
    body: CorrectPlateRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    return SessionOut.of(
        await container.parking.correct_plate(user, session_id, PlateNumber.parse(body.plate), reason=body.reason)
    )


@router.post(
    "/sessions/{session_id}/confirm-entry",
    response_model=SessionOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE, Permission.BARRIER_OPEN),
)
async def confirm_entry(
    session_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE, Permission.BARRIER_OPEN))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    return SessionOut.of(await container.parking.confirm_entry(user, session_id, reason=body.reason))


@router.post(
    "/sessions/{session_id}/open-exit", response_model=SagaOut, openapi_extra=permission_extra(Permission.BARRIER_OPEN)
)
async def open_exit(
    session_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.BARRIER_OPEN))],
    container: Annotated[Container, Depends(get_container)],
) -> SagaOut:
    return SagaOut.of(await container.parking.manual_open_exit(user, session_id, reason=body.reason))


class SimilarOut(BaseModel):
    session: SessionOut
    distance: float = Field(description="взвешенная правка номера: 0 — совпадает; путаница OCR (O/0, B/8) — 0.5")


class MatchRequest(BaseModel):
    target_session_id: UUID = Field(description="машина на площадке, которая на самом деле выезжает")
    reason: Reason


@router.get(
    "/sessions/{session_id}/similar",
    response_model=list[SimilarOut],
    openapi_extra=permission_extra(Permission.SESSIONS_READ),
)
async def similar_sessions(
    session_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_READ))],
    container: Annotated[Container, Depends(get_container)],
) -> list[SimilarOut]:
    """«Выезд без въезда» (камера выезда прочитала номер с ошибкой): машины на площадке с похожим номером."""
    found = await container.parking.similar_sessions(user, session_id)
    return [SimilarOut(session=SessionOut.of(f.session), distance=f.distance) for f in found]


@router.post(
    "/sessions/{session_id}/match",
    response_model=SagaOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE, Permission.BARRIER_OPEN),
)
async def match_exit(
    session_id: UUID,
    body: MatchRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE, Permission.BARRIER_OPEN))],
    container: Annotated[Container, Depends(get_container)],
) -> SagaOut:
    """«Это она»: выезд без въезда — машина target; у неё начинается обычный выезд (тариф, оплата, стрела)."""
    return SagaOut.of(await container.parking.match_exit(user, session_id, body.target_session_id, reason=body.reason))


@router.post(
    "/sessions/{session_id}/close", response_model=SessionOut, openapi_extra=permission_extra(Permission.SESSIONS_WRITE)
)
async def close_session(
    session_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    return SessionOut.of(await container.parking.close_manual(user, session_id, reason=body.reason))


@router.post(
    "/lots/{lot_id}/sessions/close-stale",
    response_model=CloseStaleOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE),
)
async def close_stale_sessions(
    lot_id: UUID,
    body: CloseStaleRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> CloseStaleOut:
    """Машины уехали незамеченными: незакрытые сессии площадки старше N часов → «Осиротевшая» (без начисления).
    dry_run — сколько закроет; иначе обязательна причина (аудит по каждой сессии)."""
    result = await container.parking.close_stale(
        user, lot_id, older_than_hours=body.older_than_hours, reason=body.reason, dry_run=bool(body.dry_run)
    )
    return CloseStaleOut(
        older_than=result.older_than,
        found=result.found,
        closed=result.closed,
        skipped_in_exit=result.skipped_in_exit,
    )


@router.post(
    "/sessions/{session_id}/orphan",
    response_model=SessionOut,
    openapi_extra=permission_extra(Permission.SESSIONS_WRITE),
)
async def orphan_session(
    session_id: UUID,
    body: ReasonRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.SESSIONS_WRITE))],
    container: Annotated[Container, Depends(get_container)],
) -> SessionOut:
    return SessionOut.of(await container.parking.mark_orphan(user, session_id, reason=body.reason))
