"""v2 этап 5: схема площадки (lot_layout), координаты мест

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0019_up.sql")


def downgrade() -> None:
    run_sql_file("0019_down.sql")
