"""Parking и ANPR: vehicle, parking_session (HASH 32), anpr_event, anpr_idempotency, exit_saga, manual_correction

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0005_up.sql")


def downgrade() -> None:
    run_sql_file("0005_down.sql")
