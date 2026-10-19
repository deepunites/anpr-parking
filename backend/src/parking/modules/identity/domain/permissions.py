"""RBAC: разрешения и матрица роль → разрешения. В коде, не в БД: меняется вместе с кодом и ревьюится в PR."""

from __future__ import annotations

import enum


class Permission(enum.StrEnum):
    SESSIONS_READ = "sessions:read"
    SESSIONS_WRITE = "sessions:write"  # коррекция номера, закрытие вручную, orphan
    BARRIER_OPEN = "barrier:open"
    BARRIER_CLOSE = "barrier:close"
    PAYMENT_READ = "payment:read"
    PAYMENT_CREATE_CASH = "payment:create_cash"
    PAYMENT_CREATE_CASH_CUSTOM = "payment:create_cash_custom"  # нестандартная сумма с reason
    PAYMENT_REFUND = "payment:refund"
    SHIFT_MANAGE = "shift:manage"
    TARIFF_READ = "tariff:read"
    TARIFF_WRITE = "tariff:write"
    ACCESS_READ = "access:read"
    ACCESS_WRITE = "access:write"
    CAMERAS_READ = "cameras:read"
    CAMERAS_WRITE = "cameras:write"
    CAMERAS_ROTATE_KEY = "cameras:rotate_key"
    BARRIERS_READ = "barriers:read"
    BARRIERS_WRITE = "barriers:write"
    LOTS_READ = "lots:read"
    LOTS_WRITE = "lots:write"
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    REPORTS_READ = "reports:read"
    RECONCILIATION_MANAGE = "reconciliation:manage"  # сверка: запуск, расхождения, решения
    AUDIT_READ = "audit:read"
    TENANTS_MANAGE = "tenants:manage"  # только оператор платформы
    DLQ_REPLAY = "dlq:replay"  # только оператор платформы
    PRIVACY_MANAGE = "privacy:manage"  # запросы субъектов ПДн (фаза 10 §3)
    INTEGRATIONS_MANAGE = "integrations:manage"  # v2: API-ключи, вебхуки, адаптеры, журнал событий, песочница
    DASHBOARD_VIEW = "dashboard:view"  # v2: сводка бизнеса владельца парковки
    SUBSCRIPTION_MANAGE = "subscription:manage"  # v2: тариф платформы и оплата подписки
    PASSES_SELL = "passes:sell"  # месячный абонемент на стоянку: продажа и чек — только администратор (MediaPark)
    PLATFORM_VIEW = "platform:view"  # v2: сводка владельца платформы — только агрегаты, без данных водителей


class Role(enum.StrEnum):
    PLATFORM_OWNER = "PLATFORM_OWNER"  # v2: владелец платформы — клиенты, подписки, здоровье; данных tenant не видит
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    OWNER = "OWNER"  # v2: владелец парковки — дашборд, сотрудники, подписка; операционных действий нет
    TENANT_ADMIN = "TENANT_ADMIN"
    DEVELOPER = "DEVELOPER"  # v2: интеграции и устройства; ни денег, ни персональных данных водителей
    MANAGER = "MANAGER"
    OPERATOR = "OPERATOR"
    CASHIER = "CASHIER"
    VIEWER = "VIEWER"


_VIEWER = frozenset({Permission.SESSIONS_READ, Permission.LOTS_READ, Permission.REPORTS_READ})
_OPERATOR = _VIEWER | frozenset(
    {
        Permission.SESSIONS_WRITE,
        Permission.BARRIER_OPEN,
        Permission.BARRIER_CLOSE,
        Permission.PAYMENT_READ,
        Permission.CAMERAS_READ,
        Permission.BARRIERS_READ,
        Permission.ACCESS_READ,
    }
)
# Кассир на выезде — единственный человек у стрелы (MediaPark, 2026-10-15): кроме денег и смены он выпускает машину
# «выезд без въезда», открывает стрелу и исправляет номер — всё с причиной и в аудите (I-15)
_CASHIER = _VIEWER | frozenset(
    {
        Permission.PAYMENT_READ,
        Permission.PAYMENT_CREATE_CASH,
        Permission.SHIFT_MANAGE,
        Permission.SESSIONS_WRITE,
        Permission.BARRIER_OPEN,
        Permission.BARRIER_CLOSE,
    }
)
_MANAGER = (
    _OPERATOR
    | _CASHIER
    | frozenset(
        {
            Permission.PAYMENT_REFUND,
            Permission.PAYMENT_CREATE_CASH_CUSTOM,
            Permission.TARIFF_READ,
            Permission.TARIFF_WRITE,
            Permission.ACCESS_WRITE,
            Permission.USERS_READ,
            Permission.AUDIT_READ,
            Permission.RECONCILIATION_MANAGE,
        }
    )
)
_TENANT_ADMIN = _MANAGER | frozenset(
    {
        Permission.USERS_WRITE,
        Permission.LOTS_WRITE,
        Permission.CAMERAS_WRITE,
        Permission.CAMERAS_ROTATE_KEY,
        Permission.BARRIERS_WRITE,
        Permission.PRIVACY_MANAGE,
    }
)
_TENANT_ADMIN = _TENANT_ADMIN | frozenset(
    {Permission.DASHBOARD_VIEW, Permission.SUBSCRIPTION_MANAGE, Permission.INTEGRATIONS_MANAGE, Permission.PASSES_SELL}
)
# Владелец: всё видит, сотрудниками и подпиской управляет, но стрелу не открывает и наличные не принимает —
# у роли ровно те кнопки, за которые она отвечает (v2, матрица ролей).
_OWNER = frozenset(
    {
        Permission.SESSIONS_READ,
        Permission.LOTS_READ,
        Permission.REPORTS_READ,
        Permission.PAYMENT_READ,
        Permission.TARIFF_READ,
        Permission.ACCESS_READ,
        Permission.CAMERAS_READ,
        Permission.BARRIERS_READ,
        Permission.USERS_READ,
        Permission.USERS_WRITE,
        Permission.AUDIT_READ,
        Permission.DASHBOARD_VIEW,
        Permission.SUBSCRIPTION_MANAGE,
    }
)
# Разработчик: устройства, политики площадок, интеграции; сессии читает для диагностики (маскирование номеров —
# этап 6 v2), денег и сотрудников не видит.
_DEVELOPER = frozenset(
    {
        Permission.SESSIONS_READ,
        Permission.LOTS_READ,
        Permission.LOTS_WRITE,
        Permission.TARIFF_READ,
        Permission.ACCESS_READ,
        Permission.CAMERAS_READ,
        Permission.CAMERAS_WRITE,
        Permission.CAMERAS_ROTATE_KEY,
        Permission.BARRIERS_READ,
        Permission.BARRIERS_WRITE,
        Permission.INTEGRATIONS_MANAGE,
    }
)
_PLATFORM_ADMIN = _TENANT_ADMIN | frozenset({Permission.TENANTS_MANAGE, Permission.DLQ_REPLAY})
_PLATFORM_OWNER = frozenset({Permission.PLATFORM_VIEW, Permission.TENANTS_MANAGE})

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.OPERATOR: _OPERATOR,
    Role.CASHIER: _CASHIER,
    Role.MANAGER: _MANAGER,
    Role.TENANT_ADMIN: _TENANT_ADMIN,
    Role.OWNER: _OWNER,
    Role.DEVELOPER: _DEVELOPER,
    Role.PLATFORM_ADMIN: _PLATFORM_ADMIN,
    Role.PLATFORM_OWNER: _PLATFORM_OWNER,
}


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]


PLATFORM_ROLES = frozenset({Role.PLATFORM_ADMIN, Role.PLATFORM_OWNER})


def can_grant(actor: Role, role: Role) -> bool:
    """Роли платформы (доступ ко всем клиентам) выдаёт только сотрудник платформы: администратор клиента не может
    завести себе «владельца платформы» и через него увидеть чужих клиентов."""
    return role not in PLATFORM_ROLES or actor in PLATFORM_ROLES
