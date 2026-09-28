"""Devices: camera, camera_api_key, barrier, barrier_command (партиции по месяцам)

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0003_up.sql")


def downgrade() -> None:
    run_sql_file("0003_down.sql")
