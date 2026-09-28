"""v2: марка/модель/цвет машины и ручная регистрация въезда

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0016_up.sql")


def downgrade() -> None:
    run_sql_file("0016_down.sql")
