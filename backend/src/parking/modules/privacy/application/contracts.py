"""Контракт модуля privacy для API и других модулей."""

from __future__ import annotations

from parking.modules.privacy.application.service import DeletionRequestView
from parking.modules.privacy.domain.request import SLA_DAYS, DeletionState

__all__ = ["SLA_DAYS", "DeletionRequestView", "DeletionState"]
