"""v2 этап 7: история статусов камер для дашбордов

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0021_up.sql")


def downgrade() -> None:
    run_sql_file("0021_down.sql")
