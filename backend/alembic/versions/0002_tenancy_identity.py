"""Tenancy и Identity: tenant, parking_lot, parking_spot, app_user, user_lot, refresh_token, login_attempt

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0002_up.sql")


def downgrade() -> None:
    run_sql_file("0002_down.sql")
