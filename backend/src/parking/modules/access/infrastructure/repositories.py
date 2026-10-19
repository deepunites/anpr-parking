from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from parking.modules.access.application.ports import SubscriptionRecord
from parking.modules.access.domain.rules import AccessKind, AccessRule

_COLUMNS = """
    id, tenant_id, lot_id, kind::text AS kind, plate_hash, lower(validity) AS valid_from, upper(validity) AS valid_to,
    discount_percent, source_id, reason, revoked_at
"""


def _rule(r: Any) -> AccessRule:
    return AccessRule(
        id=r.id,
        tenant_id=r.tenant_id,
        lot_id=r.lot_id,
        kind=AccessKind(r.kind),
        plate_hash=bytes(r.plate_hash),
        valid_from=r.valid_from,
        valid_to=r.valid_to,
        discount_percent=r.discount_percent,
        source_id=r.source_id,
        reason=r.reason,
        revoked_at=r.revoked_at,
    )


class SqlAccessRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def rules_for(self, plate_hash: bytes, lot_id: UUID, at: datetime) -> list[AccessRule]:
        rows = await self._s.execute(
            text(
                f"""
                SELECT {_COLUMNS} FROM access_rule
                WHERE tenant_id = app.current_tenant() AND plate_hash = :hash AND (lot_id IS NULL OR lot_id = :lot)
                  AND validity @> CAST(:at AS timestamptz) AND revoked_at IS NULL
                """
            ),
            {"hash": plate_hash, "lot": lot_id, "at": at},
        )
        return [_rule(r) for r in rows]

    async def add(
        self, rule: AccessRule, plate_encrypted: bytes, enc_key_version: int, created_by: UUID | None
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO access_rule (id, tenant_id, lot_id, kind, plate_hash, plate_encrypted, enc_key_version, validity,
                                         discount_percent, source_id, reason, created_by)
                VALUES (:id, :tenant, :lot, CAST(:kind AS access_kind), :hash, :enc, :version,
                        tstzrange(CAST(:valid_from AS timestamptz), CAST(:valid_to AS timestamptz), '[)'), :discount, :source, :reason, :created_by)
                """
            ),
            {
                "id": rule.id,
                "tenant": rule.tenant_id,
                "lot": rule.lot_id,
                "kind": rule.kind.value,
                "hash": rule.plate_hash,
                "enc": plate_encrypted,
                "version": enc_key_version,
                "valid_from": rule.valid_from,
                "valid_to": rule.valid_to,
                "discount": rule.discount_percent,
                "source": rule.source_id,
                "reason": rule.reason,
                "created_by": created_by,
            },
        )

    async def get(self, rule_id: UUID) -> AccessRule | None:
        row = (
            await self._s.execute(text(f"SELECT {_COLUMNS} FROM access_rule WHERE id = :id"), {"id": rule_id})
        ).first()
        return None if row is None else _rule(row)

    async def revoke(self, rule_id: UUID, *, revoked_by: UUID, reason: str, at: datetime) -> bool:
        result = await self._s.execute(
            text(
                "UPDATE access_rule SET revoked_at = :at, revoked_by = :by, revoke_reason = :reason WHERE id = :id AND revoked_at IS NULL"
            ),
            {"id": rule_id, "by": revoked_by, "reason": reason, "at": at},
        )
        return bool(getattr(result, "rowcount", 0))

    async def list(
        self, *, kind: AccessKind | None, lot_id: UUID | None, limit: int, after: tuple[datetime, UUID] | None
    ) -> list[tuple[AccessRule, bytes, int, datetime]]:
        conditions = ["revoked_at IS NULL"]
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            conditions.append("kind = CAST(:kind AS access_kind)")
            params["kind"] = kind.value
        if lot_id is not None:
            conditions.append("(lot_id = :lot OR lot_id IS NULL)")
            params["lot"] = lot_id
        if after is not None:
            conditions.append("(created_at, id) < (:after_at, :after_id)")
            params.update({"after_at": after[0], "after_id": after[1]})
        query = f"SELECT {_COLUMNS}, plate_encrypted, enc_key_version, created_at FROM access_rule WHERE {' AND '.join(conditions)} ORDER BY created_at DESC, id DESC LIMIT :limit"
        rows = await self._s.execute(text(query), params)
        return [(_rule(r), bytes(r.plate_encrypted), r.enc_key_version, r.created_at) for r in rows]


class SqlSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, subscription_id: UUID) -> SubscriptionRecord | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT s.id, s.lot_id, s.plan_code, s.starts_at, s.ends_at, s.price, r.plate_encrypted, r.enc_key_version
                    FROM subscription s JOIN access_rule r ON r.source_id = s.id AND r.tenant_id = s.tenant_id
                    WHERE s.id = :id LIMIT 1
                    """
                ),
                {"id": subscription_id},
            )
        ).first()
        if row is None:
            return None
        return SubscriptionRecord(
            id=row.id,
            lot_id=row.lot_id,
            plan_code=row.plan_code,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            price_minor=int(row.price),
            plate_encrypted=bytes(row.plate_encrypted),
            enc_key_version=int(row.enc_key_version),
        )

    async def add(
        self,
        *,
        subscription_id: UUID,
        tenant_id: UUID,
        lot_id: UUID | None,
        plate_hash: bytes,
        plan_code: str,
        starts_at: datetime,
        ends_at: datetime,
        price_minor: int,
        created_by: UUID,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO subscription (id, tenant_id, lot_id, plate_hash, plan_code, starts_at, ends_at, price, created_by)
                VALUES (:id, :tenant, :lot, :hash, :plan, :starts, :ends, :price, :created_by)
                """
            ),
            {
                "id": subscription_id,
                "tenant": tenant_id,
                "lot": lot_id,
                "hash": plate_hash,
                "plan": plan_code,
                "starts": starts_at,
                "ends": ends_at,
                "price": price_minor,
                "created_by": created_by,
            },
        )
