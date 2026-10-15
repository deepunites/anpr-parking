"""Эхо выезда: въезд-событие выезжающей машины (ENTRY_EXIT_ECHO)

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0026_up.sql")


def downgrade() -> None:
    run_sql_file("0026_down.sql")
