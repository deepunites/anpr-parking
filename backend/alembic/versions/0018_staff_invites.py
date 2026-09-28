"""v2 этап 4: сотрудники — телефон, временный пароль, приглашения

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0018_up.sql")


def downgrade() -> None:
    run_sql_file("0018_down.sql")
