"""Реестр чеков: сквозной номер чека по площадке (receipt, parking_lot.receipt_seq)

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0028_up.sql")


def downgrade() -> None:
    run_sql_file("0028_down.sql")
