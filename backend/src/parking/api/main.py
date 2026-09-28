"""Точка входа uvicorn: `uvicorn parking.api.main:app`.

Конфиг валидируется при импорте — процесс не стартует без него (I-14).
"""

from __future__ import annotations

from parking.api.app import create_app
from parking.platform.config import load_settings

app = create_app(load_settings())
