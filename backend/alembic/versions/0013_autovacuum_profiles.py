"""Фаза 9 §3: autovacuum-профили горячих таблиц (anpr_idempotency, outbox, consumer_inbox, anpr_event, webhook_inbox)

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from sqlfiles import run_sql_file

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0013_up.sql")


def downgrade() -> None:
    run_sql_file("0013_down.sql")
