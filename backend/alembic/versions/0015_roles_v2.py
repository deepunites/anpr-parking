"""v2 этап 1: роли OWNER, DEVELOPER, PLATFORM_OWNER

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0015_up.sql")


def downgrade() -> None:
    run_sql_file("0015_down.sql")
