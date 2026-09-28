# Реестр integration events

Сгенерировано из `backend/src/parking/platform/events_registry.py` (`uv run python scripts/export_events.py`). Не редактировать вручную.

## Контракт транспорта (ADR-07, ADR-11)

- Транспорт: `outbox` → Redis Streams `events:{hash(aggregate_id) mod 8}`; истина о публикации — таблица `outbox` (7 дней).
- Гарантии: **at-least-once**; порядок сохраняется в пределах `aggregate_id`; между агрегатами порядка нет.
- Потребители **обязаны быть идемпотентны** по `event_id` (таблица `consumer_inbox` в транзакции эффекта).
- Конверт (поля Redis Stream entry): `event_id`, `event_type`, `tenant_id`, `aggregate_type`, `aggregate_id`, `occurred_at`, `correlation_id`, `trace_parent`, `payload` (JSON).
- Эволюция: совместимые изменения — только добавление необязательных полей (`additionalProperties: true`); несовместимые — новая версия `vN+1`, старая публикуется параллельно ≥ 1 релиз.
- Схема проверяется у продюсера в `UnitOfWork.publish()` и в publisher перед `XADD`.

## События

| Тип | Продюсер | Потребители | Описание |
|---|---|---|---|
| `parking.session.created.v1` | parking | occupancy, ws-fanout, reporting, event_log | Создана сессия (ENTRY принят). status = ACTIVE либо MANUAL_REVIEW (низкая уверенность). |
| `parking.session.closed.v1` | parking | occupancy, ws-fanout, reporting, event_log | Сессия закрыта: выезд по камере, по платежу (INFERRED_BY_PAYMENT) или вручную. |
| `parking.session.orphaned.v1` | parking | occupancy, ws-fanout, reporting, event_log | Сессия признана осиротевшей (A-07): повторный въезд, таймаут или решение оператора. |
| `parking.session.manual_review.v1` | parking | notification, ws-fanout, event_log | Сессия требует вмешательства оператора (низкая уверенность, отказ шлагбаума, неизвестный выезд). |
| `parking.exit.requested.v1` | parking | saga-runner, event_log | EXIT принят, создана сага; продвижение саги начинается с этого события. |
| `parking.plate.corrected.v1` | parking | reporting, event_log, ws-fanout | Оператор скорректировал номер сессии (I-16): исходное распознавание сохранено в anpr_event/manual_correction. |
| `devices.barrier.command_requested.v1` | devices | barrier-executor, event_log | Команда шлагбауму записана (PENDING) и должна быть исполнена вне транзакции (I-9). |
| `devices.barrier.command_completed.v1` | devices | saga-runner, ws-fanout, reporting, event_log | Команда исполнена: OK, FAILED или SKIPPED (нет шлагбаума). |
| `devices.camera.status_changed.v1` | devices | notification, ws-fanout, reporting | Здоровье камеры изменилось (фаза 4). |
| `devices.camera_key.revoked.v1` | devices | camera-auth-cache | Ключ камеры отозван — кеш аутентификации сбрасывается (фаза 4). |
| `access.rule.changed.v1` | access | access-cache, reporting, event_log | Правило доступа добавлено/отозвано — страховочная инвалидация кеша решений (ADR-04). |
| `tariff.version.published.v1` | tariff | reporting, event_log | Опубликована новая версия тарифа (I-12). |
| `identity.user.role_changed.v1` | identity | event_log | Роль пользователя изменена; token_version увеличен — старые токены отвергаются немедленно. |
| `identity.refresh.reuse_detected.v1` | identity | notification, event_log | Повторное использование refresh-токена: семейство отозвано, возможна компрометация. |
| `identity.login.locked.v1` | identity | notification, event_log | Пользователь заблокирован после серии неудачных входов. |
| `tenancy.lot.updated.v1` | tenancy | reporting, event_log, ws-fanout | Площадка создана или изменена (вместимость, политики). |
| `billing.payment.requested.v1` | billing | ws-fanout, notification, event_log | Создано обязательство (PENDING): сумма к оплате известна кассиру/дисплею. |
| `billing.payment.confirmed.v1` | billing | saga-runner, ws-fanout, reporting, event_log | Платёж PAID (наличные, вебхук провайдера, льгота с суммой 0). |
| `billing.payment.failed.v1` | billing | saga-runner, notification, event_log | Провайдер отклонил платёж. |
| `billing.payment.cancelled.v1` | billing | ws-fanout, event_log | Обязательство отменено (сага истекла или оператор). |
| `billing.payment.unverified.v1` | billing | saga-runner, notification, event_log | create() провайдера не ответил, status() не знает: сага → PAYMENT_PENDING_VERIFICATION, оператор уведомлён (фаза 5 §6). |
| `billing.refund.created.v1` | billing | payments-worker, reporting, event_log | Возврат запрошен (CARD/ONLINE — исполняется воркером через провайдера). |
| `billing.refund.completed.v1` | billing | reporting, notification, event_log | Возврат исполнен (DONE); REFUNDED у платежа — производно триггером БД (I-6). |
| `billing.refund.failed.v1` | billing | notification, event_log | Провайдер отказал в возврате — ручное решение. |
| `billing.shift.closed.v1` | billing | reporting, notification, event_log | Смена кассира закрыта: ожидаемая/фактическая наличность и расхождение. |
| `billing.reconciliation.completed.v1` | billing | reporting, event_log | Сверка за период завершена; отчёт целиком — в payment_reconciliation.report. |
| `billing.reconciliation.discrepancy_opened.v1` | billing | notification, event_log | Открыто расхождение сверки (в т. ч. двойная оплата провайдером → очередь возврата). |

### `parking.session.created.v1`

Создана сессия (ENTRY принят). status = ACTIVE либо MANUAL_REVIEW (низкая уверенность).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "entry_time": {
      "type": "string",
      "format": "date-time"
    },
    "status": {
      "type": "string",
      "enum": [
        "ACTIVE",
        "CLOSED",
        "ORPHAN",
        "MANUAL_REVIEW",
        "CANCELLED"
      ]
    },
    "entry_access_kind": {
      "type": "string"
    },
    "camera_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "session_id",
    "lot_id",
    "vehicle_id",
    "entry_time",
    "status"
  ],
  "additionalProperties": true
}
```

### `parking.session.closed.v1`

Сессия закрыта: выезд по камере, по платежу (INFERRED_BY_PAYMENT) или вручную.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "entry_time": {
      "type": "string",
      "format": "date-time"
    },
    "status": {
      "type": "string",
      "enum": [
        "ACTIVE",
        "CLOSED",
        "ORPHAN",
        "MANUAL_REVIEW",
        "CANCELLED"
      ]
    },
    "exit_time": {
      "type": "string",
      "format": "date-time"
    },
    "billing_minutes": {
      "type": "integer",
      "minimum": 0
    },
    "exit_kind": {
      "type": "string",
      "enum": [
        "CAMERA",
        "INFERRED_BY_PAYMENT",
        "MANUAL"
      ]
    }
  },
  "required": [
    "session_id",
    "lot_id",
    "vehicle_id",
    "entry_time",
    "exit_time",
    "billing_minutes",
    "exit_kind"
  ],
  "additionalProperties": true
}
```

### `parking.session.orphaned.v1`

Сессия признана осиротевшей (A-07): повторный въезд, таймаут или решение оператора.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "entry_time": {
      "type": "string",
      "format": "date-time"
    },
    "status": {
      "type": "string",
      "enum": [
        "ACTIVE",
        "CLOSED",
        "ORPHAN",
        "MANUAL_REVIEW",
        "CANCELLED"
      ]
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "lot_id",
    "vehicle_id",
    "entry_time",
    "reason"
  ],
  "additionalProperties": true
}
```

### `parking.session.manual_review.v1`

Сессия требует вмешательства оператора (низкая уверенность, отказ шлагбаума, неизвестный выезд).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "entry_time": {
      "type": "string",
      "format": "date-time"
    },
    "status": {
      "type": "string",
      "enum": [
        "ACTIVE",
        "CLOSED",
        "ORPHAN",
        "MANUAL_REVIEW",
        "CANCELLED"
      ]
    },
    "reason": {
      "type": "string"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "session_id",
    "lot_id",
    "reason"
  ],
  "additionalProperties": true
}
```

### `parking.exit.requested.v1`

EXIT принят, создана сага; продвижение саги начинается с этого события.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "saga_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount_due": {
      "type": "integer",
      "minimum": 0
    },
    "access_kind": {
      "type": "string"
    }
  },
  "required": [
    "saga_id",
    "session_id",
    "lot_id",
    "amount_due"
  ],
  "additionalProperties": true
}
```

### `parking.plate.corrected.v1`

Оператор скорректировал номер сессии (I-16): исходное распознавание сохранено в anpr_event/manual_correction.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "old_vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "new_vehicle_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "correction_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "user_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "session_id",
    "lot_id",
    "old_vehicle_id",
    "new_vehicle_id",
    "correction_id",
    "user_id"
  ],
  "additionalProperties": true
}
```

### `devices.barrier.command_requested.v1`

Команда шлагбауму записана (PENDING) и должна быть исполнена вне транзакции (I-9).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "command_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "barrier_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "kind": {
      "type": "string",
      "enum": [
        "OPEN",
        "CLOSE",
        "STATUS"
      ]
    },
    "source": {
      "type": "string"
    },
    "session_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "command_id",
    "barrier_id",
    "lot_id",
    "kind",
    "source"
  ],
  "additionalProperties": true
}
```

### `devices.barrier.command_completed.v1`

Команда исполнена: OK, FAILED или SKIPPED (нет шлагбаума).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "command_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "barrier_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "result": {
      "type": "string",
      "enum": [
        "OK",
        "FAILED",
        "SKIPPED"
      ]
    },
    "session_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "error": {
      "type": [
        "string",
        "null"
      ]
    }
  },
  "required": [
    "command_id",
    "lot_id",
    "result"
  ],
  "additionalProperties": true
}
```

### `devices.camera.status_changed.v1`

Здоровье камеры изменилось (фаза 4).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "camera_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "old_status": {
      "type": "string"
    },
    "new_status": {
      "type": "string"
    }
  },
  "required": [
    "camera_id",
    "lot_id",
    "new_status"
  ],
  "additionalProperties": true
}
```

### `devices.camera_key.revoked.v1`

Ключ камеры отозван — кеш аутентификации сбрасывается (фаза 4).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "key_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "camera_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "key_prefix": {
      "type": "string"
    }
  },
  "required": [
    "key_id",
    "camera_id",
    "key_prefix"
  ],
  "additionalProperties": true
}
```

### `access.rule.changed.v1`

Правило доступа добавлено/отозвано — страховочная инвалидация кеша решений (ADR-04).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "rule_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "kind": {
      "type": "string"
    },
    "lot_id": {
      "type": [
        "string",
        "null"
      ]
    },
    "change": {
      "type": "string",
      "enum": [
        "added",
        "revoked"
      ]
    },
    "plate_hash": {
      "type": "string",
      "pattern": "^[0-9a-f]{64}$"
    }
  },
  "required": [
    "rule_id",
    "kind",
    "change",
    "plate_hash"
  ],
  "additionalProperties": true
}
```

### `tariff.version.published.v1`

Опубликована новая версия тарифа (I-12).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "plan_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "version_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "version": {
      "type": "integer",
      "minimum": 1
    },
    "valid_from": {
      "type": "string",
      "format": "date-time"
    }
  },
  "required": [
    "plan_id",
    "lot_id",
    "version_id",
    "version",
    "valid_from"
  ],
  "additionalProperties": true
}
```

### `identity.user.role_changed.v1`

Роль пользователя изменена; token_version увеличен — старые токены отвергаются немедленно.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "user_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "old_role": {
      "type": "string"
    },
    "new_role": {
      "type": "string"
    }
  },
  "required": [
    "user_id",
    "old_role",
    "new_role"
  ],
  "additionalProperties": true
}
```

### `identity.refresh.reuse_detected.v1`

Повторное использование refresh-токена: семейство отозвано, возможна компрометация.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "user_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "family_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "user_id",
    "family_id"
  ],
  "additionalProperties": true
}
```

### `identity.login.locked.v1`

Пользователь заблокирован после серии неудачных входов.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "user_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "locked_until": {
      "type": "string",
      "format": "date-time"
    }
  },
  "required": [
    "user_id",
    "locked_until"
  ],
  "additionalProperties": true
}
```

### `tenancy.lot.updated.v1`

Площадка создана или изменена (вместимость, политики).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "code": {
      "type": "string"
    },
    "capacity": {
      "type": "integer",
      "minimum": 1
    },
    "active": {
      "type": "boolean"
    },
    "timezone": {
      "type": "string"
    }
  },
  "required": [
    "lot_id",
    "code",
    "capacity",
    "active"
  ],
  "additionalProperties": true
}
```

### `billing.payment.requested.v1`

Создано обязательство (PENDING): сумма к оплате известна кассиру/дисплею.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    },
    "currency": {
      "type": "string"
    }
  },
  "required": [
    "payment_id",
    "session_id",
    "lot_id",
    "amount",
    "currency"
  ],
  "additionalProperties": true
}
```

### `billing.payment.confirmed.v1`

Платёж PAID (наличные, вебхук провайдера, льгота с суммой 0).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    },
    "method": {
      "type": "string"
    },
    "paid_at": {
      "type": "string",
      "format": "date-time"
    }
  },
  "required": [
    "payment_id",
    "session_id",
    "lot_id",
    "amount",
    "method",
    "paid_at"
  ],
  "additionalProperties": true
}
```

### `billing.payment.failed.v1`

Провайдер отклонил платёж.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "payment_id",
    "session_id",
    "reason"
  ],
  "additionalProperties": true
}
```

### `billing.payment.cancelled.v1`

Обязательство отменено (сага истекла или оператор).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "payment_id",
    "session_id",
    "reason"
  ],
  "additionalProperties": true
}
```

### `billing.payment.unverified.v1`

create() провайдера не ответил, status() не знает: сага → PAYMENT_PENDING_VERIFICATION, оператор уведомлён (фаза 5 §6).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "session_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "saga_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "provider": {
      "type": "string"
    },
    "attempt_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    }
  },
  "required": [
    "payment_id",
    "session_id",
    "provider",
    "attempt_id"
  ],
  "additionalProperties": true
}
```

### `billing.refund.created.v1`

Возврат запрошен (CARD/ONLINE — исполняется воркером через провайдера).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "refund_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    },
    "method": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "refund_id",
    "payment_id",
    "amount",
    "method",
    "status"
  ],
  "additionalProperties": true
}
```

### `billing.refund.completed.v1`

Возврат исполнен (DONE); REFUNDED у платежа — производно триггером БД (I-6).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "refund_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    },
    "method": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "refund_id",
    "payment_id",
    "amount",
    "method",
    "status"
  ],
  "additionalProperties": true
}
```

### `billing.refund.failed.v1`

Провайдер отказал в возврате — ручное решение.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "refund_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "payment_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    },
    "method": {
      "type": "string"
    },
    "status": {
      "type": "string"
    },
    "reason": {
      "type": "string"
    }
  },
  "required": [
    "refund_id",
    "payment_id",
    "amount",
    "method",
    "status"
  ],
  "additionalProperties": true
}
```

### `billing.shift.closed.v1`

Смена кассира закрыта: ожидаемая/фактическая наличность и расхождение.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "shift_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "lot_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "cashier_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "expected_cash": {
      "type": "integer"
    },
    "counted_cash": {
      "type": "integer"
    },
    "discrepancy": {
      "type": "integer"
    }
  },
  "required": [
    "shift_id",
    "lot_id",
    "cashier_id",
    "expected_cash",
    "counted_cash",
    "discrepancy"
  ],
  "additionalProperties": true
}
```

### `billing.reconciliation.completed.v1`

Сверка за период завершена; отчёт целиком — в payment_reconciliation.report.

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "reconciliation_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "provider": {
      "type": "string"
    },
    "period_start": {
      "type": "string"
    },
    "period_end": {
      "type": "string"
    },
    "discrepancies": {
      "type": "integer"
    }
  },
  "required": [
    "reconciliation_id",
    "provider",
    "period_start",
    "period_end",
    "discrepancies"
  ],
  "additionalProperties": true
}
```

### `billing.reconciliation.discrepancy_opened.v1`

Открыто расхождение сверки (в т. ч. двойная оплата провайдером → очередь возврата).

Гарантии: at-least-once, порядок в пределах aggregate_id, идемпотентность потребителя по event_id

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "discrepancy_id": {
      "type": "string",
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "kind": {
      "type": "string"
    },
    "payment_id": {
      "type": [
        "string",
        "null"
      ],
      "format": "uuid",
      "pattern": "^[0-9a-f-]{36}$"
    },
    "amount": {
      "type": "integer",
      "minimum": 0
    }
  },
  "required": [
    "discrepancy_id",
    "kind",
    "amount"
  ],
  "additionalProperties": true
}
```
