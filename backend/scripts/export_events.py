"""Генерирует docs/EVENTS.md из platform.events_registry — реестр в коде первичен."""

from __future__ import annotations

import json
from pathlib import Path

from parking.platform.events_registry import REGISTRY

OUT = Path(__file__).resolve().parents[2] / "docs" / "EVENTS.md"


def render() -> str:
    lines = [
        "# Реестр integration events",
        "",
        "Сгенерировано из `backend/src/parking/platform/events_registry.py` (`uv run python scripts/export_events.py`). Не редактировать вручную.",
        "",
        "## Контракт транспорта (ADR-07, ADR-11)",
        "",
        "- Транспорт: `outbox` → Redis Streams `events:{hash(aggregate_id) mod 8}`; истина о публикации — таблица `outbox` (7 дней).",
        "- Гарантии: **at-least-once**; порядок сохраняется в пределах `aggregate_id`; между агрегатами порядка нет.",
        "- Потребители **обязаны быть идемпотентны** по `event_id` (таблица `consumer_inbox` в транзакции эффекта).",
        "- Конверт (поля Redis Stream entry): `event_id`, `event_type`, `tenant_id`, `aggregate_type`, `aggregate_id`, `occurred_at`, `correlation_id`, `trace_parent`, `payload` (JSON).",
        "- Эволюция: совместимые изменения — только добавление необязательных полей (`additionalProperties: true`); несовместимые — новая версия `vN+1`, старая публикуется параллельно ≥ 1 релиз.",
        "- Схема проверяется у продюсера в `UnitOfWork.publish()` и в publisher перед `XADD`.",
        "",
        "## События",
        "",
        "| Тип | Продюсер | Потребители | Описание |",
        "|---|---|---|---|",
    ]
    for spec in REGISTRY.values():
        lines.append(f"| `{spec.type}` | {spec.producer} | {', '.join(spec.consumers)} | {spec.description} |")
    lines.append("")
    for spec in REGISTRY.values():
        lines.extend(
            [
                f"### `{spec.type}`",
                "",
                spec.description,
                "",
                f"Гарантии: {spec.guarantees}",
                "",
                "```json",
                json.dumps(spec.schema, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    return "\n".join(lines)


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"written {OUT} ({len(REGISTRY)} events)")
