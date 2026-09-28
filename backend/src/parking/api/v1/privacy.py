"""Запросы субъектов ПДн (фаза 10 §3): открыть → подтвердить личность → выполнить цикл → отчёт. Только PRIVACY_MANAGE
(TENANT_ADMIN); открытый номер принимается один раз в теле POST и не возвращается."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from parking.api.deps import get_container, permission_extra, require
from parking.api.schemas import Reason
from parking.modules.identity.application.contracts import CurrentUser, Permission
from parking.modules.privacy.application.contracts import DeletionRequestView, DeletionState
from parking.runtime.container import Container
from parking.shared.plate import PlateNumber

router = APIRouter(prefix="/api/v1/privacy", tags=["privacy"])


class OpenRequest(BaseModel):
    plate: str = Field(min_length=4, max_length=20)
    legal_basis: str = Field(pattern=r"^(SUBJECT_REQUEST|CONSENT_WITHDRAWN|RETENTION_EXPIRED|COURT_ORDER)$")
    contact: str | None = Field(default=None, max_length=200, description="Контакт субъекта для ответа (шифруется)")
    reason: Reason


class ReasonBody(BaseModel):
    reason: Reason


class RequestOut(BaseModel):
    id: str
    state: DeletionState
    legal_basis: str
    requested_at: datetime
    due_at: datetime
    verified_at: datetime | None
    retention_note: str | None
    anonymized_at: datetime | None
    media_deleted_at: datetime | None
    completed_at: datetime | None
    rejected_reason: str | None

    @classmethod
    def of(cls, v: DeletionRequestView) -> RequestOut:
        return cls(
            id=str(v.id),
            state=v.state,
            legal_basis=v.legal_basis,
            requested_at=v.requested_at,
            due_at=v.due_at,
            verified_at=v.verified_at,
            retention_note=v.retention_note,
            anonymized_at=v.anonymized_at,
            media_deleted_at=v.media_deleted_at,
            completed_at=v.completed_at,
            rejected_reason=v.rejected_reason,
        )


@router.post(
    "/requests",
    response_model=RequestOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=permission_extra(Permission.PRIVACY_MANAGE),
)
async def open_request(
    body: OpenRequest,
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> RequestOut:
    view = await container.privacy.request(
        user, PlateNumber.parse(body.plate), legal_basis=body.legal_basis, contact=body.contact, reason=body.reason
    )
    return RequestOut.of(view)


@router.get("/requests", response_model=list[RequestOut], openapi_extra=permission_extra(Permission.PRIVACY_MANAGE))
async def list_requests(
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
    open_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RequestOut]:
    return [RequestOut.of(v) for v in await container.privacy.list(user, open_only=open_only, limit=limit)]


@router.get(
    "/requests/{request_id}", response_model=RequestOut, openapi_extra=permission_extra(Permission.PRIVACY_MANAGE)
)
async def get_request(
    request_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> RequestOut:
    return RequestOut.of(await container.privacy.get(user, request_id))


@router.post(
    "/requests/{request_id}/verify",
    response_model=RequestOut,
    openapi_extra=permission_extra(Permission.PRIVACY_MANAGE),
)
async def verify_identity(
    request_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> RequestOut:
    return RequestOut.of(await container.privacy.verify_identity(user, request_id, reason=body.reason))


@router.post(
    "/requests/{request_id}/process",
    response_model=RequestOut,
    openapi_extra=permission_extra(Permission.PRIVACY_MANAGE),
)
async def process_request(
    request_id: UUID,
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> RequestOut:
    """Проходит LEGAL_RETENTION_CHECK → ANONYMIZATION → MEDIA_DELETION → AUDIT → COMPLETED."""
    return RequestOut.of(await container.privacy.process(user, request_id))


@router.post(
    "/requests/{request_id}/reject",
    response_model=RequestOut,
    openapi_extra=permission_extra(Permission.PRIVACY_MANAGE),
)
async def reject_request(
    request_id: UUID,
    body: ReasonBody,
    user: Annotated[CurrentUser, Depends(require(Permission.PRIVACY_MANAGE))],
    container: Annotated[Container, Depends(get_container)],
) -> RequestOut:
    return RequestOut.of(await container.privacy.reject(user, request_id, reason=body.reason))
