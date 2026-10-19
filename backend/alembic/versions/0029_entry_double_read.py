"""Двойное чтение на въезде (ENTRY_DOUBLE_READ)

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0029_up.sql")


def downgrade() -> None:
    run_sql_file("0029_down.sql")
