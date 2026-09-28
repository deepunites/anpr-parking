"""v2 этап 8: автоотключение вебхуков после 3 суток отказов

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0022_up.sql")


def downgrade() -> None:
    run_sql_file("0022_down.sql")
