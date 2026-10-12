"""Полные номера в панели — политика площадки

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0025_up.sql")


def downgrade() -> None:
    run_sql_file("0025_down.sql")
