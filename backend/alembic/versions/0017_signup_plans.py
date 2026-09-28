"""v2 этап 3: тарифы платформы, подписка клиента, ключи клиентов в конверте

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0017_up.sql")


def downgrade() -> None:
    run_sql_file("0017_down.sql")
