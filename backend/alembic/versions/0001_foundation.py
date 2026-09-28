"""Фундамент: btree_gist, схема app, роли, enum-типы, служебные функции

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0001_up.sql")


def downgrade() -> None:
    run_sql_file("0001_down.sql")
