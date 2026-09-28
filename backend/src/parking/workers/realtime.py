"""Integration events → сообщения операторского WebSocket (потребитель `ws-fanout` из реестра событий)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from parking.platform.logging import get_logger
from parking.platform.realtime import RealtimePublisher, WsEventType, WsMessage
from parking.runtime.container import Container
from parking.workers.envelope import Envelope

log = get_logger("workers.realtime")

Handler = Callable[[Envelope], Awaitable[None]]


class RealtimeFanout:
    def __init__(self, container: Container, publisher: RealtimePublisher | None = None) -> None:
        self._container = container
        self._publisher = publisher or RealtimePublisher(container.redis)

    async def handle(self, env: Envelope) -> None:
        for message in await self.messages_for(env):
            try:
                await self._publisher.publish(env.tenant_id, message)
            except Exception as exc:  # realtime — best effort: UI перезапросит состояние; событие в БД уже есть
                log.warning("realtime_publish_failed", event_type=env.event_type, error_type=type(exc).__name__)

    async def messages_for(self, env: Envelope) -> list[WsMessage]:  # noqa: PLR0911 — таблица событий
        p = env.payload
        lot_raw = p.get("lot_id")
        if not lot_raw:
            return []
        lot_id = UUID(str(lot_raw))

        def msg(kind: WsEventType, data: dict[str, Any], *, suffix: str = "", coalesce_key: str = "") -> WsMessage:
            return WsMessage(
                type=kind,
                lot_id=lot_id,
                data=data,
                ts=env.occurred_at,
                event_id=f"{env.event_id}{suffix}",
                coalesce_key=coalesce_key,
            )

        kind = env.event_type
        if kind == "parking.session.created.v1":
            base = {
                "session_id": p.get("session_id"),
                "status": p.get("status"),
                "entry_time": p.get("entry_time"),
                "camera_id": p.get("camera_id"),
            }
            return [
                msg(WsEventType.ENTRY, base, suffix=":entry"),
                msg(WsEventType.SESSION_CREATED, base),
                *await self._occupancy(msg, lot_id),
            ]
        if kind == "parking.session.closed.v1":
            base = {
                "session_id": p.get("session_id"),
                "status": p.get("status", "CLOSED"),
                "exit_time": p.get("exit_time"),
                "exit_kind": p.get("exit_kind"),
            }
            return [
                msg(WsEventType.EXIT, base, suffix=":exit"),
                msg(WsEventType.SESSION_CLOSED, base),
                *await self._occupancy(msg, lot_id),
            ]
        if kind == "parking.session.orphaned.v1":
            base = {"session_id": p.get("session_id"), "status": "ORPHAN", "reason": p.get("reason")}
            return [
                msg(WsEventType.SESSION_CLOSED, base),
                msg(WsEventType.ALERT, {"kind": "ORPHAN", **base}, suffix=":alert"),
                *await self._occupancy(msg, lot_id),
            ]
        if kind == "parking.session.manual_review.v1":
            return [
                msg(
                    WsEventType.ALERT,
                    {"kind": "MANUAL_REVIEW", "session_id": p.get("session_id"), "reason": p.get("reason")},
                ),
                msg(
                    WsEventType.SESSION_UPDATED,
                    {"session_id": p.get("session_id"), "status": "MANUAL_REVIEW", "reason": p.get("reason")},
                    suffix=":upd",
                ),
            ]
        if kind == "parking.exit.requested.v1":
            return [
                msg(
                    WsEventType.EXIT,
                    {
                        "session_id": p.get("session_id"),
                        "saga_id": p.get("saga_id"),
                        "amount": p.get("amount_due"),
                        "state": p.get("state"),
                    },
                )
            ]
        if kind == "parking.plate.corrected.v1":
            return [msg(WsEventType.SESSION_UPDATED, {"session_id": p.get("session_id"), "corrected": True})]
        if kind.startswith("billing.payment."):
            status = {
                "requested": "PENDING",
                "confirmed": "PAID",
                "failed": "FAILED",
                "cancelled": "CANCELLED",
                "unverified": "PENDING_VERIFICATION",
            }
            return [
                msg(
                    WsEventType.PAYMENT,
                    {
                        "payment_id": p.get("payment_id"),
                        "session_id": p.get("session_id"),
                        "amount": p.get("amount"),
                        "method": p.get("method"),
                        "status": status.get(kind.split(".")[2], "UNKNOWN"),
                    },
                )
            ]
        if kind.startswith("devices.barrier.command_"):
            return [
                msg(
                    WsEventType.BARRIER,
                    {
                        "command_id": p.get("command_id"),
                        "barrier_id": p.get("barrier_id"),
                        "session_id": p.get("session_id"),
                        "result": p.get("result") or "REQUESTED",
                        "error": p.get("error"),
                    },
                )
            ]
        if kind == "devices.camera.status_changed.v1":
            out = [
                msg(
                    WsEventType.CAMERA_STATUS,
                    {"camera_id": p.get("camera_id"), "status": p.get("new_status"), "old_status": p.get("old_status")},
                    coalesce_key=str(p.get("camera_id")),
                )
            ]
            if p.get("new_status") in {"OFFLINE", "SILENT"}:
                out.append(
                    msg(
                        WsEventType.ALERT,
                        {"kind": "CAMERA", "camera_id": p.get("camera_id"), "status": p.get("new_status")},
                        suffix=":alert",
                    )
                )
            return out
        return []

    async def _occupancy(self, msg: Any, lot_id: UUID) -> list[WsMessage]:
        try:
            occupied = await self._container.occupancy.get(lot_id)
        except Exception as exc:
            log.warning("realtime_occupancy_unavailable", error_type=type(exc).__name__)
            return []
        if occupied is None:
            return []
        result: WsMessage = msg(WsEventType.OCCUPANCY, {"occupied": occupied}, suffix=":occ", coalesce_key=str(lot_id))
        return [result]


REALTIME_EVENTS = (
    "parking.session.created.v1",
    "parking.session.closed.v1",
    "parking.session.orphaned.v1",
    "parking.session.manual_review.v1",
    "parking.exit.requested.v1",
    "parking.plate.corrected.v1",
    "billing.payment.requested.v1",
    "billing.payment.confirmed.v1",
    "billing.payment.failed.v1",
    "billing.payment.cancelled.v1",
    "billing.payment.unverified.v1",
    "devices.barrier.command_requested.v1",
    "devices.barrier.command_completed.v1",
    "devices.camera.status_changed.v1",
)


def with_realtime(handlers: dict[str, Handler], fanout: RealtimeFanout) -> dict[str, Handler]:
    """Добавляет realtime-рассылку к существующим обработчикам: сначала доменный эффект, потом UI."""
    combined: dict[str, Handler] = dict(handlers)
    for event_type in REALTIME_EVENTS:
        inner = handlers.get(event_type)

        async def run(env: Envelope, _inner: Handler | None = inner) -> None:
            if _inner is not None:
                await _inner(env)
            await fanout.handle(env)

        combined[event_type] = run
    return combined
