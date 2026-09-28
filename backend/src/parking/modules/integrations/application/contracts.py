"""Что интеграции отдают другим модулям и API: каталоги прав ключей и событий вебхуков."""

from __future__ import annotations

from parking.modules.integrations.domain.api_key import GRANTABLE_SCOPES, looks_like_api_key
from parking.modules.integrations.domain.journal import JournalEntry, Outcome, mask_plate
from parking.modules.integrations.domain.webhook import EVENT_CATALOG

__all__ = ["EVENT_CATALOG", "GRANTABLE_SCOPES", "JournalEntry", "Outcome", "looks_like_api_key", "mask_plate"]
