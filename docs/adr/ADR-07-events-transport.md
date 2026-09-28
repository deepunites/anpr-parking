### ADR-07: Транспорт integration events — Outbox → Redis Streams, шардирование по `aggregate_id`

**Контекст:** 30–60 событий/с в пике, ≤ 8 внутренних потребителей, Redis уже обязателен для кеша и оффлайн-буфера. Требования: не терять (I-4, I-5), сохранять порядок событий одного агрегата, доставлять at-least-once.

**Решение:** Единственный активный publisher (advisory lock в PostgreSQL) читает `outbox` через `FOR UPDATE SKIP LOCKED` в порядке `id`, просыпаясь по `NOTIFY` с fallback-опросом 250 мс, и делает `XADD` в `events:{hash(aggregate_id) mod 8}` с `MAXLEN ~`. На каждый шард — consumer group; один активный потребитель на шард (аренда в Redis), поэтому порядок на агрегат сохраняется. Обработчики идемпотентны по `event_id` (таблица inbox в той же транзакции, что и эффект). Истина — `outbox` (хранится 7 дней), что позволяет переопубликовать окно при потере данных Redis.

**Отвергнуто:**
- `LISTEN/NOTIFY` — нет consumer group, pending и повторной доставки.
- Kafka/RabbitMQ — третья stateful-система ради 60 msg/s для одного разработчика.

**Последствия:** проще — один Redis, `XAUTOCLAIM` и DLQ из коробки, `traceparent` в конверте; дороже — аренда шардов, `MAXLEN` как предел буфера (алерт при 80 %), inbox-таблица у потребителей.

**Риск:** Redis без persistence теряет непотреблённые события при failover. Обнаруживается разрывом `event_id` в inbox и метрикой `outbox_published_not_consumed`; закрывается командой переопубликации из outbox.
