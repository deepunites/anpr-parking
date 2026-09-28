"""Реестр integration events: имя с версией, JSON Schema, продюсер, потребители, гарантии.

Схема проверяется при `UnitOfWork.publish()` (продюсер) и в publisher'е перед XADD. `docs/EVENTS.md` генерируется
из этого реестра (`scripts/export_events.py`) — документация не может разойтись с кодом.
Эволюция: изменения совместимы только добавлением необязательных полей; иначе — новая версия `.vN+1`, старая
публикуется параллельно минимум один релиз (A-22).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

UUID_SCHEMA: dict[str, Any] = {"type": "string", "format": "uuid", "pattern": "^[0-9a-f-]{36}$"}
TS_SCHEMA: dict[str, Any] = {"type": "string", "format": "date-time"}
MONEY_MINOR: dict[str, Any] = {"type": "integer", "minimum": 0}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,  # совместимая эволюция: новые необязательные поля не ломают потребителей
    }


@dataclass(frozen=True, slots=True)
class EventSpec:
    type: str
    producer: str
    consumers: tuple[str, ...]
    description: str
    schema: dict[str, Any]
    guarantees: str = "at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id"
    validator: Draft202012Validator = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "validator", Draft202012Validator(self.schema))


SESSION_PROPS = {
    "session_id": UUID_SCHEMA,
    "lot_id": UUID_SCHEMA,
    "vehicle_id": UUID_SCHEMA,
    "entry_time": TS_SCHEMA,
    "status": {"type": "string", "enum": ["ACTIVE", "CLOSED", "ORPHAN", "MANUAL_REVIEW", "CANCELLED"]},
}

REGISTRY: dict[str, EventSpec] = {
    spec.type: spec
    for spec in [
        EventSpec(
            "parking.session.created.v1",
            "parking",
            ("occupancy", "ws-fanout", "reporting", "event_log"),
            "Создана сессия (ENTRY принят). status = ACTIVE либо MANUAL_REVIEW (низкая уверенность).",
            _schema(
                {**SESSION_PROPS, "entry_access_kind": {"type": "string"}, "camera_id": UUID_SCHEMA},
                ["session_id", "lot_id", "vehicle_id", "entry_time", "status"],
            ),
        ),
        EventSpec(
            "parking.session.closed.v1",
            "parking",
            ("occupancy", "ws-fanout", "reporting", "event_log"),
            "Сессия закрыта: выезд по камере, по платежу (INFERRED_BY_PAYMENT) или вручную.",
            _schema(
                {
                    **SESSION_PROPS,
                    "exit_time": TS_SCHEMA,
                    "billing_minutes": {"type": "integer", "minimum": 0},
                    "exit_kind": {"type": "string", "enum": ["CAMERA", "INFERRED_BY_PAYMENT", "MANUAL"]},
                },
                ["session_id", "lot_id", "vehicle_id", "entry_time", "exit_time", "billing_minutes", "exit_kind"],
            ),
        ),
        EventSpec(
            "parking.session.orphaned.v1",
            "parking",
            ("occupancy", "ws-fanout", "reporting", "event_log"),
            "Сессия признана осиротевшей (A-07): повторный въезд, таймаут или решение оператора.",
            _schema(
                {**SESSION_PROPS, "reason": {"type": "string"}},
                ["session_id", "lot_id", "vehicle_id", "entry_time", "reason"],
            ),
        ),
        EventSpec(
            "parking.session.manual_review.v1",
            "parking",
            ("notification", "ws-fanout", "event_log"),
            "Сессия требует вмешательства оператора (низкая уверенность, отказ шлагбаума, неизвестный выезд).",
            _schema(
                {**SESSION_PROPS, "reason": {"type": "string"}, "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]}},
                ["session_id", "lot_id", "reason"],
            ),
        ),
        EventSpec(
            "parking.exit.requested.v1",
            "parking",
            ("saga-runner", "event_log"),
            "EXIT принят, создана сага; продвижение саги начинается с этого события.",
            _schema(
                {
                    "saga_id": UUID_SCHEMA,
                    "session_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "amount_due": MONEY_MINOR,
                    "access_kind": {"type": "string"},
                },
                ["saga_id", "session_id", "lot_id", "amount_due"],
            ),
        ),
        EventSpec(
            "parking.plate.corrected.v1",
            "parking",
            ("reporting", "event_log", "ws-fanout"),
            "Оператор скорректировал номер сессии (I-16): исходное распознавание сохранено в anpr_event/manual_correction.",
            _schema(
                {
                    "session_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "old_vehicle_id": UUID_SCHEMA,
                    "new_vehicle_id": UUID_SCHEMA,
                    "correction_id": UUID_SCHEMA,
                    "user_id": UUID_SCHEMA,
                },
                ["session_id", "lot_id", "old_vehicle_id", "new_vehicle_id", "correction_id", "user_id"],
            ),
        ),
        EventSpec(
            "devices.barrier.command_requested.v1",
            "devices",
            ("barrier-executor", "event_log"),
            "Команда шлагбауму записана (PENDING) и должна быть исполнена вне транзакции (I-9).",
            _schema(
                {
                    "command_id": UUID_SCHEMA,
                    "barrier_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "kind": {"type": "string", "enum": ["OPEN", "CLOSE", "STATUS"]},
                    "source": {"type": "string"},
                    "session_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                },
                ["command_id", "barrier_id", "lot_id", "kind", "source"],
            ),
        ),
        EventSpec(
            "devices.barrier.command_completed.v1",
            "devices",
            ("saga-runner", "ws-fanout", "reporting", "event_log"),
            "Команда исполнена: OK, FAILED или SKIPPED (нет шлагбаума).",
            _schema(
                {
                    "command_id": UUID_SCHEMA,
                    "barrier_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "lot_id": UUID_SCHEMA,
                    "result": {"type": "string", "enum": ["OK", "FAILED", "SKIPPED"]},
                    "session_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "error": {"type": ["string", "null"]},
                },
                ["command_id", "lot_id", "result"],
            ),
        ),
        EventSpec(
            "devices.camera.status_changed.v1",
            "devices",
            ("notification", "ws-fanout", "reporting"),
            "Здоровье камеры изменилось (фаза 4).",
            _schema(
                {
                    "camera_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "old_status": {"type": "string"},
                    "new_status": {"type": "string"},
                },
                ["camera_id", "lot_id", "new_status"],
            ),
        ),
        EventSpec(
            "devices.camera_key.revoked.v1",
            "devices",
            ("camera-auth-cache",),
            "Ключ камеры отозван — кеш аутентификации сбрасывается (фаза 4).",
            _schema(
                {"key_id": UUID_SCHEMA, "camera_id": UUID_SCHEMA, "key_prefix": {"type": "string"}},
                ["key_id", "camera_id", "key_prefix"],
            ),
        ),
        EventSpec(
            "access.rule.changed.v1",
            "access",
            ("access-cache", "reporting", "event_log"),
            "Правило доступа добавлено/отозвано — страховочная инвалидация кеша решений (ADR-04).",
            _schema(
                {
                    "rule_id": UUID_SCHEMA,
                    "kind": {"type": "string"},
                    "lot_id": {"type": ["string", "null"]},
                    "change": {"type": "string", "enum": ["added", "revoked"]},
                    "plate_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                ["rule_id", "kind", "change", "plate_hash"],
            ),
        ),
        EventSpec(
            "tariff.version.published.v1",
            "tariff",
            ("reporting", "event_log"),
            "Опубликована новая версия тарифа (I-12).",
            _schema(
                {
                    "plan_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "version_id": UUID_SCHEMA,
                    "version": {"type": "integer", "minimum": 1},
                    "valid_from": TS_SCHEMA,
                },
                ["plan_id", "lot_id", "version_id", "version", "valid_from"],
            ),
        ),
        EventSpec(
            "identity.user.role_changed.v1",
            "identity",
            ("event_log",),
            "Роль пользователя изменена; token_version увеличен — старые токены отвергаются немедленно.",
            _schema(
                {"user_id": UUID_SCHEMA, "old_role": {"type": "string"}, "new_role": {"type": "string"}},
                ["user_id", "old_role", "new_role"],
            ),
        ),
        EventSpec(
            "identity.refresh.reuse_detected.v1",
            "identity",
            ("notification", "event_log"),
            "Повторное использование refresh-токена: семейство отозвано, возможна компрометация.",
            _schema({"user_id": UUID_SCHEMA, "family_id": UUID_SCHEMA}, ["user_id", "family_id"]),
        ),
        EventSpec(
            "identity.login.locked.v1",
            "identity",
            ("notification", "event_log"),
            "Пользователь заблокирован после серии неудачных входов.",
            _schema({"user_id": UUID_SCHEMA, "locked_until": TS_SCHEMA}, ["user_id", "locked_until"]),
        ),
        EventSpec(
            "tenancy.lot.updated.v1",
            "tenancy",
            ("reporting", "event_log", "ws-fanout"),
            "Площадка создана или изменена (вместимость, политики).",
            _schema(
                {
                    "lot_id": UUID_SCHEMA,
                    "code": {"type": "string"},
                    "capacity": {"type": "integer", "minimum": 1},
                    "active": {"type": "boolean"},
                    "timezone": {"type": "string"},
                },
                ["lot_id", "code", "capacity", "active"],
            ),
        ),
        EventSpec(
            "billing.payment.requested.v1",
            "billing",
            ("ws-fanout", "notification", "event_log"),
            "Создано обязательство (PENDING): сумма к оплате известна кассиру/дисплею.",
            _schema(
                {
                    "payment_id": UUID_SCHEMA,
                    "session_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "amount": MONEY_MINOR,
                    "currency": {"type": "string"},
                },
                ["payment_id", "session_id", "lot_id", "amount", "currency"],
            ),
        ),
        EventSpec(
            "billing.payment.confirmed.v1",
            "billing",
            ("saga-runner", "ws-fanout", "reporting", "event_log"),
            "Платёж PAID (наличные, вебхук провайдера, льгота с суммой 0).",
            _schema(
                {
                    "payment_id": UUID_SCHEMA,
                    "session_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "amount": MONEY_MINOR,
                    "method": {"type": "string"},
                    "paid_at": TS_SCHEMA,
                },
                ["payment_id", "session_id", "lot_id", "amount", "method", "paid_at"],
            ),
        ),
        EventSpec(
            "billing.payment.failed.v1",
            "billing",
            ("saga-runner", "notification", "event_log"),
            "Провайдер отклонил платёж.",
            _schema(
                {
                    "payment_id": UUID_SCHEMA,
                    "session_id": UUID_SCHEMA,
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
                ["payment_id", "session_id", "reason"],
            ),
        ),
        EventSpec(
            "billing.payment.cancelled.v1",
            "billing",
            ("ws-fanout", "event_log"),
            "Обязательство отменено (сага истекла или оператор).",
            _schema(
                {"payment_id": UUID_SCHEMA, "session_id": UUID_SCHEMA, "reason": {"type": "string"}},
                ["payment_id", "session_id", "reason"],
            ),
        ),
        EventSpec(
            "billing.payment.unverified.v1",
            "billing",
            ("saga-runner", "notification", "event_log"),
            "create() провайдера не ответил, status() не знает: сага → PAYMENT_PENDING_VERIFICATION, оператор уведомлён (фаза 5 §6).",
            _schema(
                {
                    "payment_id": UUID_SCHEMA,
                    "session_id": UUID_SCHEMA,
                    "saga_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "provider": {"type": "string"},
                    "attempt_id": UUID_SCHEMA,
                },
                ["payment_id", "session_id", "provider", "attempt_id"],
            ),
        ),
        EventSpec(
            "billing.refund.created.v1",
            "billing",
            ("payments-worker", "reporting", "event_log"),
            "Возврат запрошен (CARD/ONLINE — исполняется воркером через провайдера).",
            _schema(
                {
                    "refund_id": UUID_SCHEMA,
                    "payment_id": UUID_SCHEMA,
                    "amount": MONEY_MINOR,
                    "method": {"type": "string"},
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["refund_id", "payment_id", "amount", "method", "status"],
            ),
        ),
        EventSpec(
            "billing.refund.completed.v1",
            "billing",
            ("reporting", "notification", "event_log"),
            "Возврат исполнен (DONE); REFUNDED у платежа — производно триггером БД (I-6).",
            _schema(
                {
                    "refund_id": UUID_SCHEMA,
                    "payment_id": UUID_SCHEMA,
                    "amount": MONEY_MINOR,
                    "method": {"type": "string"},
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["refund_id", "payment_id", "amount", "method", "status"],
            ),
        ),
        EventSpec(
            "billing.refund.failed.v1",
            "billing",
            ("notification", "event_log"),
            "Провайдер отказал в возврате — ручное решение.",
            _schema(
                {
                    "refund_id": UUID_SCHEMA,
                    "payment_id": UUID_SCHEMA,
                    "amount": MONEY_MINOR,
                    "method": {"type": "string"},
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["refund_id", "payment_id", "amount", "method", "status"],
            ),
        ),
        EventSpec(
            "billing.shift.closed.v1",
            "billing",
            ("reporting", "notification", "event_log"),
            "Смена кассира закрыта: ожидаемая/фактическая наличность и расхождение.",
            _schema(
                {
                    "shift_id": UUID_SCHEMA,
                    "lot_id": UUID_SCHEMA,
                    "cashier_id": UUID_SCHEMA,
                    "expected_cash": {"type": "integer"},
                    "counted_cash": {"type": "integer"},
                    "discrepancy": {"type": "integer"},
                },
                ["shift_id", "lot_id", "cashier_id", "expected_cash", "counted_cash", "discrepancy"],
            ),
        ),
        EventSpec(
            "billing.reconciliation.completed.v1",
            "billing",
            ("reporting", "event_log"),
            "Сверка за период завершена; отчёт целиком — в payment_reconciliation.report.",
            _schema(
                {
                    "reconciliation_id": UUID_SCHEMA,
                    "provider": {"type": "string"},
                    "period_start": {"type": "string"},
                    "period_end": {"type": "string"},
                    "discrepancies": {"type": "integer"},
                },
                ["reconciliation_id", "provider", "period_start", "period_end", "discrepancies"],
            ),
        ),
        EventSpec(
            "billing.reconciliation.discrepancy_opened.v1",
            "billing",
            ("notification", "event_log"),
            "Открыто расхождение сверки (в т. ч. двойная оплата провайдером → очередь возврата).",
            _schema(
                {
                    "discrepancy_id": UUID_SCHEMA,
                    "kind": {"type": "string"},
                    "payment_id": {**UUID_SCHEMA, "type": ["string", "null"]},
                    "amount": MONEY_MINOR,
                },
                ["discrepancy_id", "kind", "amount"],
            ),
        ),
    ]
}


class UnknownEventTypeError(ValueError):
    pass


class EventSchemaError(ValueError):
    pass


def validate_event(event_type: str, payload: dict[str, Any]) -> None:
    """Бросает EventSchemaError / UnknownEventTypeError — продюсер обязан публиковать только зарегистрированные контракты."""
    spec = REGISTRY.get(event_type)
    if spec is None:
        raise UnknownEventTypeError(f"integration event {event_type!r} is not registered in platform.events_registry")
    errors = sorted(spec.validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise EventSchemaError(f"{event_type}: {'/'.join(str(p) for p in first.path) or '$'}: {first.message}")
