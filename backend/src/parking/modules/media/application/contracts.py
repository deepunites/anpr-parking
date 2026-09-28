"""Open Host Service модуля media для других контекстов (privacy: удаление изображений субъекта)."""

from __future__ import annotations

from typing import Protocol


class ObjectDeleter(Protocol):
    async def delete_all_versions(self, key: str) -> int:
        """Удаляет все версии объекта в версионируемом бакете (delete marker недостаточно). Возвращает число версий."""
        ...


__all__ = ["ObjectDeleter"]
