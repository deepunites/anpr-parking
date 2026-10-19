-- PostgreSQL не удаляет значения enum: EXIT_ENTRY_ECHO остаётся, старый код его просто не пишет.
ALTER TABLE parking_lot DROP COLUMN IF EXISTS exit_min_stay_seconds;
