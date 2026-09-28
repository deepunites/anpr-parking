"""Prometheus `/metrics`: реестр процесса + gauge-метрики, обновляемые на scrape (MetricsRefresher)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from parking.api.deps import PUBLIC, get_container
from parking.platform.metrics import registry
from parking.runtime.container import Container

router = APIRouter(tags=["ops"])


@router.get("/metrics", openapi_extra=PUBLIC, include_in_schema=False)
async def prometheus_metrics(container: Annotated[Container, Depends(get_container)]) -> Response:
    await container.metrics_refresher.refresh()
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
