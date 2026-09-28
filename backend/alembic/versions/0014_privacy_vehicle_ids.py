"""Фаза 10 §3: deletion_request.vehicle_ids — машины субъекта для шагов анонимизации и удаления медиа

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0014_up.sql")


def downgrade() -> None:
    run_sql_file("0014_down.sql")
