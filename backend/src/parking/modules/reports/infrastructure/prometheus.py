"""Здоровье платформы из Prometheus (HTTP API /api/v1/query). Не настроен или не ответил за 2 с — None,
дашборд показывает локальные признаки (outbox, DLQ, вебхуки, камеры)."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx
import structlog

from parking.modules.reports.application.ports import PromHealth
from parking.platform.http.client import make_async_client

log = structlog.get_logger(__name__)

QUERIES = {
    "api_up": 'count(up{job="parking-api"} == 1)',
    "api_p95": "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
    "api_errors": (
        'sum(rate(http_request_duration_seconds_count{status_class="5xx"}[5m]))'
        " / clamp_min(sum(rate(http_request_duration_seconds_count[5m])), 1e-9)"
    ),
    "exit_p95": "histogram_quantile(0.95, sum(rate(exit_end_to_end_seconds_bucket[15m])) by (le))",
    "outbox_lag": "max(outbox_publish_lag_seconds)",
}


class PrometheusHealth:
    def __init__(self, base_url: str | None, *, timeout: float = 2.0) -> None:
        self._client = make_async_client(base_url=base_url.rstrip("/"), timeout=timeout) if base_url else None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _query(self, promql: str) -> list[dict[str, Any]]:
        assert self._client is not None
        resp = await self._client.get("/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise ValueError(body.get("error", "prometheus error"))
        return list(body["data"]["result"])

    @staticmethod
    def _scalar(result: list[dict[str, Any]]) -> float | None:
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value  # NaN — нет данных за окно

    async def snapshot(self) -> PromHealth | None:
        if self._client is None:
            return None
        try:
            names = list(QUERIES)
            results = await asyncio.gather(*(self._query(QUERIES[n]) for n in names))
            alerts = await self._query('ALERTS{alertstate="firing"}')
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("prometheus_unavailable", error=type(exc).__name__)
            return None
        v = {n: self._scalar(r) for n, r in zip(names, results, strict=True)}
        return PromHealth(
            api_up=int(v["api_up"]) if v["api_up"] is not None else None,
            api_p95_ms=round(v["api_p95"] * 1000, 1) if v["api_p95"] is not None else None,
            api_error_rate=round(v["api_errors"], 5) if v["api_errors"] is not None else None,
            exit_p95_ms=round(v["exit_p95"] * 1000, 1) if v["exit_p95"] is not None else None,
            outbox_lag_seconds=v["outbox_lag"],
            firing_alerts=sorted({str(a["metric"].get("alertname", "?")) for a in alerts}),
        )
