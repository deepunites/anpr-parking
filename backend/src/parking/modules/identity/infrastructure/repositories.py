from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from parking.modules.identity.application.ports import UserLookup
from parking.modules.identity.domain.invite import Invite
from parking.modules.identity.domain.permissions import Role
from parking.modules.identity.domain.refresh import RefreshTokenRecord
from parking.modules.identity.domain.user import User
from parking.shared.errors import NotFoundError

_USER_COLUMNS = """
    u.id, u.tenant_id, u.email, u.password_hash, u.role::text AS role, u.display_name, u.locale, u.token_version,
    u.active, u.failed_logins, u.locked_until, u.last_login_at, u.created_at, u.phone, u.must_change_password,
    coalesce((SELECT array_agg(ul.lot_id) FROM user_lot ul WHERE ul.user_id = u.id), '{}') AS lot_scope
"""


def _to_user(row: Any) -> User:
    return User(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        password_hash=row.password_hash,
        role=Role(row.role),
        display_name=row.display_name,
        locale=row.locale,
        token_version=row.token_version,
        active=row.active,
        failed_logins=row.failed_logins,
        locked_until=row.locked_until,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
        lot_scope=frozenset(row.lot_scope or ()),
        phone=row.phone,
        must_change_password=row.must_change_password,
    )


def _lookup(row: Any) -> UserLookup | None:
    if row is None:
        return None
    return UserLookup(
        user_id=row.id,
        tenant_id=row.tenant_id,
        password_hash=row.password_hash,
        active=row.active,
        tenant_active=row.tenant_active,
    )


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def lookup_by_email(self, email: str) -> UserLookup | None:
        row = (await self._s.execute(text("SELECT * FROM app.lookup_user_by_email(:email)"), {"email": email})).first()
        return _lookup(row)

    async def lookup_by_phone(self, phone: str) -> UserLookup | None:
        row = (await self._s.execute(text("SELECT * FROM app.lookup_user_by_phone(:phone)"), {"phone": phone})).first()
        return _lookup(row)

    async def get(self, user_id: UUID) -> User | None:
        query = text(f"SELECT {_USER_COLUMNS} FROM app_user u WHERE u.id = :id")
        row = (await self._s.execute(query, {"id": user_id})).first()
        return None if row is None else _to_user(row)

    async def get_for_update(self, user_id: UUID) -> User | None:
        query = text(f"SELECT {_USER_COLUMNS} FROM app_user u WHERE u.id = :id FOR UPDATE OF u")
        row = (await self._s.execute(query, {"id": user_id})).first()
        return None if row is None else _to_user(row)

    async def add(self, user: User) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO app_user (id, tenant_id, email, password_hash, role, display_name, locale, token_version, active,
                                      phone, must_change_password)
                VALUES (:id, :tenant_id, :email, :password_hash, CAST(:role AS user_role), :display_name, :locale,
                        :token_version, :active, :phone, :must_change_password)
                """
            ),
            {
                "id": user.id,
                "tenant_id": user.tenant_id,
                "email": user.email,
                "password_hash": user.password_hash,
                "role": user.role.value,
                "display_name": user.display_name,
                "locale": user.locale,
                "token_version": user.token_version,
                "active": user.active,
                "phone": user.phone,
                "must_change_password": user.must_change_password,
            },
        )

    async def save(self, user: User) -> None:
        await self._s.execute(
            text(
                """
                UPDATE app_user SET password_hash = :password_hash, role = CAST(:role AS user_role), display_name = :display_name,
                    locale = :locale, token_version = :token_version, active = :active, failed_logins = :failed_logins,
                    locked_until = :locked_until, last_login_at = :last_login_at, email = :email, phone = :phone,
                    must_change_password = :must_change_password
                WHERE id = :id
                """
            ),
            {
                "id": user.id,
                "password_hash": user.password_hash,
                "role": user.role.value,
                "display_name": user.display_name,
                "locale": user.locale,
                "token_version": user.token_version,
                "active": user.active,
                "failed_logins": user.failed_logins,
                "locked_until": user.locked_until,
                "last_login_at": user.last_login_at,
                "email": user.email,
                "phone": user.phone,
                "must_change_password": user.must_change_password,
            },
        )

    async def list(self, *, limit: int, after: tuple[datetime, UUID] | None) -> list[User]:
        params: dict[str, Any] = {"limit": limit}
        cursor = ""
        if after is not None:
            cursor = "WHERE (u.created_at, u.id) < (:after_at, :after_id)"
            params.update({"after_at": after[0], "after_id": after[1]})
        query = f"SELECT {_USER_COLUMNS} FROM app_user u {cursor} ORDER BY u.created_at DESC, u.id DESC LIMIT :limit"
        rows = await self._s.execute(text(query), params)
        return [_to_user(r) for r in rows]

    async def check_lots(self, lots: frozenset[UUID]) -> None:
        """Площадки только своего tenant (RLS на parking_lot): чужой/несуществующий id — 404, а не 500 по FK."""
        if not lots:
            return
        rows = await self._s.execute(text("SELECT id FROM parking_lot WHERE id = ANY(:ids)"), {"ids": list(lots)})
        missing = set(lots) - {row.id for row in rows}
        if missing:
            raise NotFoundError(f"lot not found: {sorted(str(m) for m in missing)[0]}")

    async def replace_lot_scope(self, user_id: UUID, lots: frozenset[UUID]) -> None:
        await self.check_lots(lots)
        await self._s.execute(text("DELETE FROM user_lot WHERE user_id = :id"), {"id": user_id})
        for lot_id in lots:
            await self._s.execute(
                text(
                    "INSERT INTO user_lot (tenant_id, user_id, lot_id) SELECT tenant_id, id, :lot FROM app_user WHERE id = :id"
                ),
                {"id": user_id, "lot": lot_id},
            )


class SqlRefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        token_id: UUID,
        tenant_id: UUID,
        user_id: UUID,
        family_id: UUID,
        token_hash: bytes,
        expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO refresh_token (id, tenant_id, user_id, family_id, token_hash, expires_at, ip_address, user_agent)
                VALUES (:id, :tenant_id, :user_id, :family_id, :token_hash, :expires_at, CAST(:ip AS inet), :user_agent)
                """
            ),
            {
                "id": token_id,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "family_id": family_id,
                "token_hash": token_hash,
                "expires_at": expires_at,
                "ip": ip,
                "user_agent": user_agent,
            },
        )

    async def get_for_update(self, token_hash: bytes) -> RefreshTokenRecord | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT id, tenant_id, user_id, family_id, issued_at, expires_at, used_at, revoked_at FROM refresh_token "
                    "WHERE token_hash = :h FOR UPDATE"
                ),
                {"h": token_hash},
            )
        ).first()
        if row is None:
            return None
        return RefreshTokenRecord(
            id=row.id,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            family_id=row.family_id,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            used_at=row.used_at,
            revoked_at=row.revoked_at,
        )

    async def mark_used(self, token_id: UUID, replaced_by: UUID, now: datetime) -> None:
        await self._s.execute(
            text("UPDATE refresh_token SET used_at = :now, replaced_by = :replaced WHERE id = :id AND used_at IS NULL"),
            {"id": token_id, "replaced": replaced_by, "now": now},
        )

    async def revoke_family(self, family_id: UUID, reason: str, now: datetime) -> int:
        result = await self._s.execute(
            text(
                "UPDATE refresh_token SET revoked_at = :now, revoke_reason = :reason WHERE family_id = :family AND revoked_at IS NULL"
            ),
            {"family": family_id, "reason": reason, "now": now},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def revoke_user(self, user_id: UUID, reason: str, now: datetime) -> int:
        result = await self._s.execute(
            text(
                "UPDATE refresh_token SET revoked_at = :now, revoke_reason = :reason "
                "WHERE user_id = :user AND revoked_at IS NULL AND used_at IS NULL"
            ),
            {"user": user_id, "reason": reason, "now": now},
        )
        return int(getattr(result, "rowcount", 0) or 0)


_INVITE_COLUMNS = (
    "id, tenant_id, email, phone, role::text AS role, display_name, lot_scope, created_by, created_at, expires_at, "
    "accepted_at, accepted_user_id, revoked_at"
)


def _to_invite(row: Any) -> Invite:
    return Invite(
        id=row.id,
        tenant_id=row.tenant_id,
        email=row.email,
        phone=row.phone,
        role=Role(row.role),
        display_name=row.display_name,
        lot_scope=frozenset(row.lot_scope or ()),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        accepted_at=row.accepted_at,
        accepted_user_id=row.accepted_user_id,
        revoked_at=row.revoked_at,
    )


class SqlInviteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, invite: Invite, token_hash: bytes) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO user_invite (id, tenant_id, token_hash, email, phone, role, display_name, lot_scope,
                                         created_by, created_at, expires_at)
                VALUES (:id, :tenant_id, :token_hash, :email, :phone, CAST(:role AS user_role), :display_name,
                        CAST(:lot_scope AS uuid[]), :created_by, :created_at, :expires_at)
                """
            ),
            {
                "id": invite.id,
                "tenant_id": invite.tenant_id,
                "token_hash": token_hash,
                "email": invite.email,
                "phone": invite.phone,
                "role": invite.role.value,
                "display_name": invite.display_name,
                "lot_scope": [str(lot) for lot in invite.lot_scope],
                "created_by": invite.created_by,
                "created_at": invite.created_at,
                "expires_at": invite.expires_at,
            },
        )

    async def get_for_update(self, invite_id: UUID) -> Invite | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_INVITE_COLUMNS} FROM user_invite WHERE id = :id FOR UPDATE"), {"id": invite_id}
            )
        ).first()
        return None if row is None else _to_invite(row)

    async def save(self, invite: Invite) -> None:
        await self._s.execute(
            text(
                "UPDATE user_invite SET accepted_at = :accepted_at, accepted_user_id = :accepted_user_id, "
                "revoked_at = :revoked_at WHERE id = :id"
            ),
            {
                "id": invite.id,
                "accepted_at": invite.accepted_at,
                "accepted_user_id": invite.accepted_user_id,
                "revoked_at": invite.revoked_at,
            },
        )

    async def list(self, *, limit: int) -> list[Invite]:
        rows = await self._s.execute(
            text(f"SELECT {_INVITE_COLUMNS} FROM user_invite ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}
        )
        return [_to_invite(r) for r in rows]

    async def lookup_by_token(self, token_hash: bytes) -> tuple[UUID, UUID] | None:
        row = (await self._s.execute(text("SELECT * FROM app.lookup_invite(:h)"), {"h": token_hash})).first()
        return None if row is None else (row.id, row.tenant_id)


class SqlLoginAttemptRepository:
    """Таблица без RLS; собственные короткие транзакции — вызывается до и после UoW логина."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def record(self, email_hash: bytes, ip: str | None, success: bool) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                text("INSERT INTO login_attempt (email_hash, ip_address, success) VALUES (:h, CAST(:ip AS inet), :ok)"),
                {"h": email_hash, "ip": ip, "ok": success},
            )

    async def failures(self, *, email_hash: bytes, ip: str | None, window: timedelta) -> tuple[int, int]:
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) FILTER (WHERE email_hash = :h AND ip_address IS NOT DISTINCT FROM CAST(:ip AS inet)) AS by_pair,
                               count(*) FILTER (WHERE :ip IS NOT NULL AND ip_address = CAST(:ip AS inet)) AS by_ip
                        FROM login_attempt
                        WHERE NOT success AND attempted_at > now() - CAST(:window AS interval)
                        """
                    ),
                    {"h": email_hash, "ip": ip, "window": window},
                )
            ).one()
            return int(row.by_pair), int(row.by_ip)
