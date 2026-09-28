"""Админ-операции DLQ: просмотр и replay (ручной по id или пакетный) с ролью, аудитом и dry-run (фаза 4 §4)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

from parking.modules.anpr.application.ports import DlqEntry, DlqStore
from parking.modules.audit.application.contracts import ActorType, AuditEntry, AuditWriter
from parking.modules.identity.application.contracts import CurrentUser
from parking.platform.db.uow import UnitOfWork
from parking.platform.logging import get_logger
from parking.shared.errors import ValidationError
from parking.shared.plate import mask

log = get_logger("anpr.dlq")

_SENSITIVE = {"plate_number"}


class DlqAdminService:
    def __init__(
        self,
        *,
        store: DlqStore,
        uow_factory: Callable[[UUID | None], UnitOfWork],
        audit: Callable[[UnitOfWork], AuditWriter],
    ) -> None:
        self._store = store
        self._uow_factory = uow_factory
        self._audit = audit

    async def entries(self, stream: str, *, limit: int) -> list[DlqEntry]:
        self._check_stream(stream)
        return [self._masked(e) for e in await self._store.entries(stream, limit=limit)]

    async def replay(
        self, actor: CurrentUser, stream: str, entry_ids: Sequence[str] | None, *, dry_run: bool, reason: str
    ) -> list[DlqEntry]:
        self._check_stream(stream)
        entries = await self._store.replay(stream, entry_ids, dry_run=dry_run)
        async with self._uow_factory(actor.tenant_id) as uow:
            await self._audit(uow).record(
                AuditEntry(
                    tenant_id=actor.tenant_id,
                    action="DLQ_REPLAY_DRY_RUN" if dry_run else "DLQ_REPLAY",
                    entity_type="dlq",
                    actor_type=ActorType.USER,
                    actor_id=actor.user_id,
                    user_id=actor.user_id,
                    reason=reason,
                    payload={"stream": stream, "count": len(entries), "entry_ids": [e.entry_id for e in entries][:100]},
                )
            )
        log.warning("dlq_replay", stream=stream, count=len(entries), dry_run=dry_run, user_id=str(actor.user_id))
        return [self._masked(e) for e in entries]

    @staticmethod
    def _check_stream(stream: str) -> None:
        if stream not in {"anpr", "events"}:
            raise ValidationError("stream must be 'anpr' or 'events'")

    @staticmethod
    def _masked(entry: DlqEntry) -> DlqEntry:
        fields = {k: (mask(v) if k in _SENSITIVE else v) for k, v in entry.fields.items()}
        return DlqEntry(entry_id=entry.entry_id, stream=entry.stream, fields=fields)
