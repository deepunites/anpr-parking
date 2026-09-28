"""Права фазы 3: DELETE user_lot (скоуп), UPDATE (validity) tariff_plan_version (закрытие окна версии)

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0009_up.sql")


def downgrade() -> None:
    run_sql_file("0009_down.sql")
