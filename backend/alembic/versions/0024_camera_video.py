"""Адрес и логин видеопотока камеры (запись на сервере площадки)

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0024_up.sql")


def downgrade() -> None:
    run_sql_file("0024_down.sql")
