"""v2 этап 6: API-ключи, вебхуки, журнал событий

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0020_up.sql")


def downgrade() -> None:
    run_sql_file("0020_down.sql")
