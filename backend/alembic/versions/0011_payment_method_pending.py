"""Обязательство (PENDING) не знает способа оплаты: method NULL до факта оплаты

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0011_up.sql")


def downgrade() -> None:
    run_sql_file("0011_down.sql")
