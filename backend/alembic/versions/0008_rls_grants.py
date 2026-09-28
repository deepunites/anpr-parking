"""RLS-политики на все tenant-owned таблицы и права ролей app_critical / app_bulk / app_maintenance

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0008_up.sql")


def downgrade() -> None:
    run_sql_file("0008_down.sql")
