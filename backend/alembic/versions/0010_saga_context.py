"""EXIT-сага: камера выезда и флаги контекста (late, prepaid) — нужны шагу закрытия сессии

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0010_up.sql")


def downgrade() -> None:
    run_sql_file("0010_down.sql")
