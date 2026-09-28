"""I-10 на уровне схемы: ни одной колонки с открытым номером (grep по information_schema)."""

from __future__ import annotations

import asyncpg

ALLOWED = {
    ("vehicle", "plate_hash"),
    ("vehicle", "plate_encrypted"),
    ("vehicle", "plate_format"),
    ("anpr_event", "plate_hash"),
    ("anpr_event", "plate_encrypted"),
    ("access_rule", "plate_hash"),
    ("access_rule", "plate_encrypted"),
    ("subscription", "plate_hash"),
    ("manual_correction", "corrected_plate_encrypted"),
    ("deletion_request", "subject_plate_hash"),
    ("event_journal", "plate_masked"),  # v2 этап 6: только маска «01A***AA» (CHECK на «*» в БД), восстановить нельзя
}
MASKED = {("event_journal", "plate_masked")}


async def test_only_hashed_or_encrypted_plate_columns_exist(owner: asyncpg.Connection) -> None:
    rows = await owner.fetch(
        """
        SELECT c.relname AS table_name, a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND a.attnum > 0 AND NOT a.attisdropped
          AND (a.attname ILIKE '%plate%' OR a.attname ILIKE '%number%' OR a.attname ILIKE '%nomer%')
        """
    )
    found = {(r["table_name"], r["column_name"]) for r in rows}
    unexpected = found - ALLOWED
    assert unexpected == set(), f"неожиданные колонки с номером: {unexpected}"
    for r in rows:
        if r["column_name"].endswith(("_hash", "_encrypted")):
            assert r["data_type"] == "bytea", (r["table_name"], r["column_name"], r["data_type"])
    assert ("vehicle", "plate_format") in found  # формат — не ПДн
    masked_checked = await owner.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'event_journal'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%plate_masked%'"
    )
    assert masked_checked == 1, "маскированная колонка без CHECK на маску"
