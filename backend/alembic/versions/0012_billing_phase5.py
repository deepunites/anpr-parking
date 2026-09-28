"""Billing фазы 5: ответ вебхука в inbox, индексы джобов верификации/возвратов/предоплат

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0012_up.sql")


def downgrade() -> None:
    run_sql_file("0012_down.sql")
