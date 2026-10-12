# Матрица прав (RBAC)

Сгенерировано из `backend/src/parking/modules/identity/domain/permissions.py` и `docs/openapi.json`
(`uv run python scripts/export_rbac.py`). Не редактировать вручную. Контрактный тест
`tests/api/test_rbac_contract.py` вызывает каждый эндпоинт каждой ролью и проверяет 403/не-403 по этой матрице;
API-ключ внешней системы получает только права из своих scopes (не роль создателя, A-89).

## Роли × права

| Право | VIEWER | OPERATOR | CASHIER | MANAGER | TENANT_ADMIN | OWNER | DEVELOPER | PLATFORM_ADMIN | PLATFORM_OWNER |
|---|---|---|---|---|---|---|---|---|---|
| `sessions:read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `sessions:write` |  | ✓ |  | ✓ | ✓ |  |  | ✓ |  |
| `barrier:open` |  | ✓ |  | ✓ | ✓ |  |  | ✓ |  |
| `barrier:close` |  | ✓ |  | ✓ | ✓ |  |  | ✓ |  |
| `payment:read` |  | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `payment:create_cash` |  |  | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `payment:create_cash_custom` |  |  |  | ✓ | ✓ |  |  | ✓ |  |
| `payment:refund` |  |  |  | ✓ | ✓ |  |  | ✓ |  |
| `shift:manage` |  |  | ✓ | ✓ | ✓ |  |  | ✓ |  |
| `tariff:read` |  |  |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `tariff:write` |  |  |  | ✓ | ✓ |  |  | ✓ |  |
| `access:read` |  | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `access:write` |  |  |  | ✓ | ✓ |  |  | ✓ |  |
| `cameras:read` |  | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `cameras:write` |  |  |  |  | ✓ |  | ✓ | ✓ |  |
| `cameras:rotate_key` |  |  |  |  | ✓ |  | ✓ | ✓ |  |
| `barriers:read` |  | ✓ |  | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `barriers:write` |  |  |  |  | ✓ |  | ✓ | ✓ |  |
| `lots:read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| `lots:write` |  |  |  |  | ✓ |  | ✓ | ✓ |  |
| `users:read` |  |  |  | ✓ | ✓ | ✓ |  | ✓ |  |
| `users:write` |  |  |  |  | ✓ | ✓ |  | ✓ |  |
| `reports:read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |
| `reconciliation:manage` |  |  |  | ✓ | ✓ |  |  | ✓ |  |
| `audit:read` |  |  |  | ✓ | ✓ | ✓ |  | ✓ |  |
| `tenants:manage` |  |  |  |  |  |  |  | ✓ | ✓ |
| `dlq:replay` |  |  |  |  |  |  |  | ✓ |  |
| `privacy:manage` |  |  |  |  | ✓ |  |  | ✓ |  |
| `integrations:manage` |  |  |  |  | ✓ |  | ✓ | ✓ |  |
| `dashboard:view` |  |  |  |  | ✓ | ✓ |  | ✓ |  |
| `subscription:manage` |  |  |  |  | ✓ | ✓ |  | ✓ |  |
| `platform:view` |  |  |  |  |  |  |  |  | ✓ |

## Эндпоинты

| Метод | Путь | Доступ |
|---|---|---|
| GET | `/api/v1/access-rules` | `access:read` |
| POST | `/api/v1/access-rules` | `access:write` |
| POST | `/api/v1/access-rules/{rule_id}/revoke` | `access:write` |
| GET | `/api/v1/access/resolve` | `access:read` |
| GET | `/api/v1/admin/dlq` | `dlq:replay` |
| POST | `/api/v1/admin/dlq/replay` | `dlq:replay` |
| GET | `/api/v1/api-keys` | `integrations:manage` |
| POST | `/api/v1/api-keys` | `integrations:manage` |
| POST | `/api/v1/api-keys/{api_key_id}/revoke` | `integrations:manage` |
| POST | `/api/v1/auth/login` | публичный |
| POST | `/api/v1/auth/logout` | публичный |
| GET | `/api/v1/auth/me` | — |
| POST | `/api/v1/auth/password` | — |
| POST | `/api/v1/auth/refresh` | публичный |
| GET | `/api/v1/barrier-commands/{command_id}` | `barrier:open` |
| GET | `/api/v1/barriers` | `barriers:read` |
| POST | `/api/v1/barriers` | `barriers:write` |
| GET | `/api/v1/barriers/{barrier_id}` | `barriers:read` |
| PATCH | `/api/v1/barriers/{barrier_id}` | `barriers:write` |
| POST | `/api/v1/barriers/{barrier_id}/open` | `barrier:open` |
| POST | `/api/v1/barriers/{barrier_id}/probe` | `barriers:write` |
| POST | `/api/v1/camera-keys/{key_id}/revoke` | `cameras:rotate_key` |
| POST | `/api/v1/camera-keys/{key_id}/rotate` | `cameras:rotate_key` |
| POST | `/api/v1/camera/events` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/heartbeat` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/media/presign` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/vendor/dahua` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/vendor/dahua/{camera_key}` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/vendor/hikvision` | ключ камеры (`X-Camera-Key`) |
| POST | `/api/v1/camera/vendor/hikvision/{camera_key}` | ключ камеры (`X-Camera-Key`) |
| GET | `/api/v1/cameras` | `cameras:read` |
| POST | `/api/v1/cameras` | `cameras:write` |
| GET | `/api/v1/cameras/{camera_id}` | `cameras:read` |
| PATCH | `/api/v1/cameras/{camera_id}` | `cameras:write` |
| GET | `/api/v1/cameras/{camera_id}/keys` | `cameras:rotate_key` |
| POST | `/api/v1/cameras/{camera_id}/keys` | `cameras:rotate_key` |
| GET | `/api/v1/dashboard/owner` | `dashboard:view` |
| GET | `/api/v1/dashboard/platform` | `platform:view` |
| POST | `/api/v1/hooks/inbox/{token}` | публичный |
| GET | `/api/v1/integrations/catalog` | `integrations:manage` |
| GET | `/api/v1/integrations/journal` | `integrations:manage` |
| GET | `/api/v1/integrations/openapi` | `integrations:manage` |
| POST | `/api/v1/integrations/sandbox` | `integrations:manage` |
| POST | `/api/v1/integrations/test-inbox` | `integrations:manage` |
| GET | `/api/v1/integrations/test-inbox/{token}` | `integrations:manage` |
| GET | `/api/v1/invites` | `users:read` |
| POST | `/api/v1/invites` | `users:write` |
| POST | `/api/v1/invites/{invite_id}/revoke` | `users:write` |
| GET | `/api/v1/join/{token}` | публичный |
| POST | `/api/v1/join/{token}` | публичный |
| GET | `/api/v1/lots` | `lots:read` |
| POST | `/api/v1/lots` | `lots:write` |
| GET | `/api/v1/lots/{lot_id}` | `lots:read` |
| PATCH | `/api/v1/lots/{lot_id}` | `lots:write` |
| GET | `/api/v1/lots/{lot_id}/layout` | `lots:read` |
| PUT | `/api/v1/lots/{lot_id}/layout` | `lots:write` |
| GET | `/api/v1/lots/{lot_id}/layout/versions` | `lots:read` |
| GET | `/api/v1/lots/{lot_id}/layout/versions/{version}` | `lots:read` |
| POST | `/api/v1/lots/{lot_id}/manual-entry` | `sessions:write` |
| GET | `/api/v1/lots/{lot_id}/map` | `sessions:read` |
| GET | `/api/v1/lots/{lot_id}/occupancy` | `sessions:read` |
| GET | `/api/v1/lots/{lot_id}/sessions` | `sessions:read` |
| GET | `/api/v1/lots/{lot_id}/tariff-plans` | `tariff:read` |
| POST | `/api/v1/lots/{lot_id}/tariff-plans` | `tariff:write` |
| POST | `/api/v1/payments/webhook/{provider}` | публичный |
| GET | `/api/v1/payments/{payment_id}` | `payment:read` |
| POST | `/api/v1/payments/{payment_id}/checkout` | `payment:read` |
| POST | `/api/v1/payments/{payment_id}/confirm-cash` | `payment:create_cash` |
| GET | `/api/v1/payments/{payment_id}/refunds` | `payment:read` |
| POST | `/api/v1/payments/{payment_id}/refunds` | `payment:refund` |
| GET | `/api/v1/plans` | публичный |
| GET | `/api/v1/platform/tenants` | `platform:view` |
| PATCH | `/api/v1/platform/tenants/{tenant_id}` | `tenants:manage` |
| GET | `/api/v1/privacy/requests` | `privacy:manage` |
| POST | `/api/v1/privacy/requests` | `privacy:manage` |
| GET | `/api/v1/privacy/requests/{request_id}` | `privacy:manage` |
| POST | `/api/v1/privacy/requests/{request_id}/process` | `privacy:manage` |
| POST | `/api/v1/privacy/requests/{request_id}/reject` | `privacy:manage` |
| POST | `/api/v1/privacy/requests/{request_id}/verify` | `privacy:manage` |
| GET | `/api/v1/reconciliation-discrepancies` | `reconciliation:manage` |
| POST | `/api/v1/reconciliation-discrepancies/{discrepancy_id}/resolve` | `reconciliation:manage` |
| POST | `/api/v1/reconciliations` | `reconciliation:manage` |
| GET | `/api/v1/reconciliations/{run_id}` | `reconciliation:manage` |
| GET | `/api/v1/sessions/review-queue` | `sessions:read` |
| GET | `/api/v1/sessions/search` | `sessions:read` |
| GET | `/api/v1/sessions/{session_id}` | `sessions:read` |
| POST | `/api/v1/sessions/{session_id}/close` | `sessions:write` |
| POST | `/api/v1/sessions/{session_id}/confirm-entry` | `sessions:write`, `barrier:open` |
| POST | `/api/v1/sessions/{session_id}/correct-plate` | `sessions:write` |
| GET | `/api/v1/sessions/{session_id}/images` | `sessions:read` |
| POST | `/api/v1/sessions/{session_id}/open-exit` | `barrier:open` |
| POST | `/api/v1/sessions/{session_id}/orphan` | `sessions:write` |
| GET | `/api/v1/sessions/{session_id}/payments` | `payment:read` |
| POST | `/api/v1/sessions/{session_id}/payments` | `payment:create_cash` |
| GET | `/api/v1/sessions/{session_id}/quote` | `payment:read` |
| GET | `/api/v1/sessions/{session_id}/saga` | `sessions:read` |
| POST | `/api/v1/sessions/{session_id}/vehicle` | `sessions:write` |
| GET | `/api/v1/sessions/{session_id}/videos` | `sessions:read` |
| GET | `/api/v1/shifts` | `shift:manage` |
| POST | `/api/v1/shifts` | `shift:manage` |
| GET | `/api/v1/shifts/current` | `shift:manage` |
| POST | `/api/v1/shifts/{shift_id}/close` | `shift:manage` |
| POST | `/api/v1/signup` | публичный |
| POST | `/api/v1/signup/{signup_id}/verify` | публичный |
| GET | `/api/v1/subscription` | `subscription:manage` |
| POST | `/api/v1/subscription/plan` | `subscription:manage` |
| POST | `/api/v1/subscriptions` | `access:write` |
| GET | `/api/v1/tariff-plans/{plan_id}/versions` | `tariff:read` |
| POST | `/api/v1/tariff-plans/{plan_id}/versions` | `tariff:write` |
| POST | `/api/v1/tariff/preview` | `tariff:write` |
| POST | `/api/v1/tariff/quote` | `tariff:read` |
| GET | `/api/v1/tenants/current` | `lots:read` |
| GET | `/api/v1/users` | `users:read` |
| POST | `/api/v1/users` | `users:write` |
| GET | `/api/v1/users/{user_id}` | `users:read` |
| PATCH | `/api/v1/users/{user_id}` | `users:write` |
| POST | `/api/v1/users/{user_id}/deactivate` | `users:write` |
| POST | `/api/v1/users/{user_id}/password` | `users:write` |
| PATCH | `/api/v1/users/{user_id}/role` | `users:write` |
| POST | `/api/v1/vehicles` | `sessions:write` |
| POST | `/api/v1/vehicles/lookup` | `sessions:read` |
| GET | `/api/v1/video/clips/{token}` | публичный |
| GET | `/api/v1/webhook-deliveries` | `integrations:manage` |
| POST | `/api/v1/webhook-deliveries/{delivery_id}/redeliver` | `integrations:manage` |
| GET | `/api/v1/webhooks` | `integrations:manage` |
| POST | `/api/v1/webhooks` | `integrations:manage` |
| PATCH | `/api/v1/webhooks/{endpoint_id}` | `integrations:manage` |
| POST | `/api/v1/webhooks/{endpoint_id}/delete` | `integrations:manage` |
| POST | `/api/v1/webhooks/{endpoint_id}/rotate-secret` | `integrations:manage` |
| POST | `/api/v1/webhooks/{endpoint_id}/test` | `integrations:manage` |
