"""Партнёры площадки: отметка «выезд за счёт партнёра» и расчёт с партнёром раз в месяц

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0030_up.sql")


def downgrade() -> None:
    run_sql_file("0030_down.sql")
