"""Фабрика FastAPI-приложения. Роли процессов (APP_ROLE) — фаза 9; здесь монтируются все роуты."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from parking.api.errors import install_error_handlers
from parking.api.middleware.correlation import CorrelationMiddleware
from parking.api.middleware.eager_body import EagerBodyMiddleware
from parking.api.middleware.limits import BodyLimitMiddleware
from parking.api.middleware.request_log import RequestLogMiddleware
from parking.api.v1 import (
    access,
    admin,
    auth,
    camera,
    dashboard,
    devices,
    health,
    integrations,
    invites,
    layout,
    lots,
    metrics,
    payments,
    privacy,
    sessions,
    subscription,
    tariffs,
    tenants,
    users,
    vehicles,
)
from parking.api.ws import router as ws_router
from parking.platform.config import Settings
from parking.platform.db.engine import Database, build_database
from parking.platform.telemetry import bootstrap_telemetry, instrument_app, instrument_database
from parking.runtime.container import build_container
from parking.runtime.key_refresh import run_key_refresh

CAMERA_BODY_LIMIT = 8 * 1024


def create_app(settings: Settings, *, database: Database | None = None) -> FastAPI:
    bootstrap_telemetry(settings, service=settings.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database = database or build_database(settings)
        instrument_database(app.state.database)
        app.state.container = build_container(settings, app.state.database)
        stop_keys = asyncio.Event()
        keys_task = asyncio.create_task(run_key_refresh(app.state.container, stop_keys))
        try:
            yield
        finally:
            stop_keys.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(keys_task, 5)
            await app.state.database.dispose()

    app = FastAPI(
        title="ANPR Parking Platform API",
        version=settings.version,
        openapi_version="3.1.0",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestLogMiddleware)
    # внутри BodyLimit: лимит действует и на раннее чтение тела
    app.add_middleware(EagerBodyMiddleware, prefixes=("/api/v1/camera/",))
    app.add_middleware(
        BodyLimitMiddleware,
        path_limits={
            "/api/v1/camera/events": CAMERA_BODY_LIMIT,
            "/api/v1/camera/heartbeat": CAMERA_BODY_LIMIT,
            "/api/v1/camera/vendor": settings.media_max_bytes + 64 * 1024,  # кадр + XML/JSON vendor-адаптера
        },
    )
    app.add_middleware(CorrelationMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Correlation-Id"],
        )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(invites.router)
    app.include_router(lots.router)
    app.include_router(layout.router)
    app.include_router(tenants.router)
    app.include_router(tariffs.router)
    app.include_router(access.router)
    app.include_router(sessions.router)
    app.include_router(vehicles.router)
    app.include_router(subscription.router)
    app.include_router(camera.router)
    app.include_router(devices.router)
    app.include_router(integrations.router)
    app.include_router(dashboard.router)
    app.include_router(admin.router)
    app.include_router(payments.router)
    app.include_router(privacy.router)
    app.include_router(metrics.router)
    app.include_router(ws_router)
    instrument_app(app)
    return app
