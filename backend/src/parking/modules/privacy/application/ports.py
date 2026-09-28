from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from parking.modules.media.application.contracts import ObjectDeleter
from parking.modules.privacy.domain.request import DeletionRequest, DeletionState

__all__ = [
    "AnonymizationResult",
    "DeletionRequestRepository",
    "ObjectDeleter",
    "SubjectDataRepository",
    "SubjectFootprint",
]


class DeletionRequestRepository(Protocol):
    async def add(self, request: DeletionRequest) -> None: ...
    async def get(self, request_id: UUID, *, for_update: bool = False) -> DeletionRequest | None: ...
    async def save(self, request: DeletionRequest) -> None: ...
    async def list(self, *, states: tuple[DeletionState, ...] | None, limit: int) -> list[DeletionRequest]: ...


@dataclass(frozen=True, slots=True)
class SubjectFootprint:
    vehicle_ids: list[UUID]
    active_sessions: int
    sessions_total: int
    payments_total: int
    media_keys: list[str]


@dataclass(frozen=True, slots=True)
class AnonymizationResult:
    vehicles: int
    events: int
    corrections: int


class SubjectDataRepository(Protocol):
    """Данные субъекта в пределах tenant: поиск по хешу номера, анонимизация, ключи изображений."""

    async def footprint(self, plate_hashes: list[bytes]) -> SubjectFootprint: ...
    async def media_keys(self, vehicle_ids: list[UUID]) -> list[str]: ...
    async def anonymize(
        self, vehicle_ids: list[UUID], *, token: bytes, sentinel_blob: bytes, key_version: int, now: datetime
    ) -> AnonymizationResult:
        """vehicle.plate_hash → token (необратимый), plate_encrypted → шифртекст-заглушка; то же для anpr_event и
        manual_correction по этим машинам. Возвращает число затронутых строк."""
        ...

    async def mark_media_deleted(self, keys: list[str], now: datetime) -> int: ...
