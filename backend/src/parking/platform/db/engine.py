"""Движки и фабрики сессий для трёх пулов (ADR-02)."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from parking.platform.config import Settings


class Pool(enum.StrEnum):
    CRITICAL = "critical"  # remote_apply: события, сессии, платежи, outbox
    BULK = "bulk"  # off: журналы, проекции, статусы устройств
    MAINTENANCE = "maintenance"  # кросс-tenant джобы


@dataclass(slots=True)
class Database:
    engines: dict[Pool, AsyncEngine]
    sessions: dict[Pool, async_sessionmaker[AsyncSession]]

    def session_factory(self, pool: Pool) -> async_sessionmaker[AsyncSession]:
        return self.sessions[pool]

    async def dispose(self) -> None:
        for engine in self.engines.values():
            await engine.dispose()


# серверные statement_timeout ролей (0001): critical 5s, bulk 60s, maintenance 30min; клиентский — с запасом
COMMAND_TIMEOUT_SECONDS: dict[Pool, float] = {Pool.CRITICAL: 6.0, Pool.BULK: 90.0, Pool.MAINTENANCE: 1900.0}


def build_database(settings: Settings) -> Database:
    urls = {
        Pool.CRITICAL: settings.database_url_critical.get_secret_value(),
        Pool.BULK: settings.database_url_bulk.get_secret_value(),
        Pool.MAINTENANCE: settings.database_url_maintenance.get_secret_value(),
    }
    engines = {
        pool: create_async_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_size,
            pool_pre_ping=True,
            # клиентские таймауты: при сетевом разрыве (пакеты теряются, RST нет) серверные statement_timeout
            # до клиента не доходят — без них транзакция «висит» до TCP keepalive (фаза 8 §5)
            connect_args={
                "timeout": settings.db_connect_timeout_seconds,
                "command_timeout": COMMAND_TIMEOUT_SECONDS[pool],
            },
        )
        for pool, url in urls.items()
    }
    for engine in engines.values():
        event.listen(engine.sync_engine, "invalidate", _abort_invalidated_connection)
    sessions = {
        pool: async_sessionmaker(engine, expire_on_commit=False, autoflush=False) for pool, engine in engines.items()
    }
    return Database(engines=engines, sessions=sessions)


def _abort_invalidated_connection(dbapi_connection: Any, _record: Any, _exception: BaseException | None) -> None:
    """Инвалидированное соединение (таймаут, обрыв) закрывается жёстко: graceful close asyncpg ждёт ответа сервера
    и при «чёрной дыре» в сети (пакеты теряются, RST нет) висит 60 с на каждое соединение пула — проверено
    tests/failure/test_network_partition.py. terminate() освобождает пул за миллисекунды."""
    raw = getattr(dbapi_connection, "_connection", None)
    if raw is not None and not raw.is_closed():
        raw.terminate()
