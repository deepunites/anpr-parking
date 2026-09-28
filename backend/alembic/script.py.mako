"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from sqlfiles import run_sql_file

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    run_sql_file("${up_revision}_up.sql")


def downgrade() -> None:
    run_sql_file("${up_revision}_down.sql")
