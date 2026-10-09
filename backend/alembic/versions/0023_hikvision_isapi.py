"""Шлагбаум через реле камеры Hikvision (ISAPI)

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0023_up.sql")


def downgrade() -> None:
    run_sql_file("0023_down.sql")
