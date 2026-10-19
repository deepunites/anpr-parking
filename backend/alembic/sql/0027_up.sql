-- Эхо въезда (MediaPark, 2026-10-19): стрела одна на въезд и выезд — камера выезда видит въезжающую машину сзади через
-- секунды после камеры въезда. Такой «выезд» раньше закрывал сессию в бесплатные минуты и открывал стрелу; машина
-- оставалась на площадке без сессии, а настоящий выезд становился «выездом без въезда». Выезд раньше
-- exit_min_stay_seconds после въезда — эхо: ни выезда, ни стрелы; в журнале распознаваний результат EXIT_ENTRY_ECHO.
ALTER TYPE anpr_result ADD VALUE IF NOT EXISTS 'EXIT_ENTRY_ECHO';
-- По умолчанию выключено: нужно площадкам, где камера выезда видит и въезжающих (MediaPark — 60 с).
ALTER TABLE parking_lot ADD COLUMN exit_min_stay_seconds smallint NOT NULL DEFAULT 0
    CHECK (exit_min_stay_seconds BETWEEN 0 AND 600);
COMMENT ON COLUMN parking_lot.exit_min_stay_seconds IS 'Выезд раньше стольких секунд после въезда — эхо въезда (камера выезда видит въезжающую машину), 0 — выключено.';
