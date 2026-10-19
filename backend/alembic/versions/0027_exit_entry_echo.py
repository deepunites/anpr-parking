"""Эхо въезда: выезд сразу после въезда (EXIT_ENTRY_ECHO) и parking_lot.exit_min_stay_seconds

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0027_up.sql")


def downgrade() -> None:
    run_sql_file("0027_down.sql")
