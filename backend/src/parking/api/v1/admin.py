"""Админ-API платформы: DLQ (просмотр, replay с dry-run) — только dlq:replay (оператор платформы)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.anpr.application.ports import DlqEntry
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.runtime.container import Container

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class DlqEntryOut(BaseModel):
    id: str
    stream: str
    fields: dict[str, str]

    @classmethod
    def of(cls, e: DlqEntry) -> DlqEntryOut:
        return cls(id=e.entry_id, stream=e.stream, fields=e.fields)


class DlqReplayRequest(BaseModel):
    stream: str = Field(default="anpr", pattern=r"^(anpr|events)$")
    ids: list[str] | None = Field(default=None, max_length=1000)
    dry_run: bool = False
    reason: Reason


class DlqReplayResponse(BaseModel):
    dry_run: bool
    count: int
    entries: list[DlqEntryOut]


@router.get("/dlq", response_model=list[DlqEntryOut], openapi_extra=permission_extra(Permission.DLQ_REPLAY))
async def list_dlq(
    user: Annotated[CurrentUser, Depends(require(Permission.DLQ_REPLAY))],
    container: Annotated[Container, Depends(get_container)],
    stream: Annotated[str, Query(pattern=r"^(anpr|events)$")] = "anpr",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[DlqEntryOut]:
    return [DlqEntryOut.of(e) for e in await container.dlq.entries(stream, limit=limit)]


@router.post("/dlq/replay", response_model=DlqReplayResponse, openapi_extra=permission_extra(Permission.DLQ_REPLAY))
async def replay_dlq(
    body: DlqReplayRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.DLQ_REPLAY))],
    container: Annotated[Container, Depends(get_container)],
) -> DlqReplayResponse:
    entries = await container.dlq.replay(user, body.stream, body.ids, dry_run=body.dry_run, reason=body.reason)
    return DlqReplayResponse(dry_run=body.dry_run, count=len(entries), entries=[DlqEntryOut.of(e) for e in entries])
