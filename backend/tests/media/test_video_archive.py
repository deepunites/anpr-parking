"""Видеоархив сервера площадки: ролик к событию склеивается из 10-минутных файлов записи без перекодирования —
с ключевого кадра не позже начала окна, на стыке двух файлов и двух суток, по местному времени в именах файлов.
Записи нет, файл битый — None, а не исключение; готовый ролик берётся из кеша, пока файл записи не вырос."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import av

from parking.modules.media.infrastructure.video_archive import FileVideoArchive
from tests.support.video import mark_camera, write_segment

TASHKENT = ZoneInfo("Asia/Tashkent")
FPS = 10


def _frames(path: Path) -> list[float]:
    with av.open(str(path)) as clip:
        stream = clip.streams.video[0]
        assert stream.time_base is not None
        return [float(frame.pts * stream.time_base) for frame in clip.decode(stream) if frame.pts is not None]


def _archive(tmp_path: Path) -> FileVideoArchive:
    return FileVideoArchive(tmp_path / "video", TASHKENT, tmp_path / "cache")


def test_clip_spans_two_files_from_keyframe_before_window(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-entry"
    mark_camera(folder, camera)
    first = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)  # 15:00 по Ташкенту — так и в имени файла
    write_segment(folder, first, 10, TASHKENT, fps=FPS, gop=FPS)
    write_segment(folder, first + timedelta(seconds=10), 10, TASHKENT, fps=FPS, gop=FPS)
    assert (folder / "2026-10-12" / "15-00-10.mp4").exists()

    clip = _archive(tmp_path).clip(camera, first + timedelta(seconds=5.5), first + timedelta(seconds=15))

    assert clip is not None and clip.read_bytes()[4:8] == b"ftyp"
    times = _frames(clip)
    assert times[0] == 0.0  # отсчёт ролика — от нуля
    # ключевой кадр в 5.0 с — не позже начала окна (5.5 с); конец — 15.0 с: 10 с видео без дыр на стыке
    assert 99 <= len(times) <= 101 and max(b - a for a, b in pairwise(times)) < 0.15


def test_clip_keeps_first_frame_with_b_frames(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-entry"
    mark_camera(folder, camera)
    start = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    write_segment(folder, start, 10, TASHKENT, fps=FPS, gop=FPS, bframes=2)

    clip = _archive(tmp_path).clip(camera, start + timedelta(seconds=3), start + timedelta(seconds=6))

    assert clip is not None
    times = _frames(clip)
    assert times[0] < 0.25  # ключевой кадр в 3.0 с не потерялся из-за DTS < 0
    assert 31 <= len(times) <= 34 and max(b - a for a, b in pairwise(times)) < 0.15  # и хвост окна на месте


def test_clip_across_midnight_uses_previous_day_folder(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-exit"
    mark_camera(folder, camera)
    evening = datetime(2026, 10, 12, 23, 59, 50, tzinfo=TASHKENT)
    write_segment(folder, evening, 10, TASHKENT, fps=FPS, gop=FPS)
    write_segment(folder, evening + timedelta(seconds=10), 10, TASHKENT, fps=FPS, gop=FPS)
    assert (folder / "2026-10-13" / "00-00-00.mp4").exists()

    clip = _archive(tmp_path).clip(camera, evening + timedelta(seconds=8), evening + timedelta(seconds=14))

    assert clip is not None and 59 <= len(_frames(clip)) <= 61


def test_no_recording_gives_none(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-entry"
    mark_camera(folder, camera)
    start = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    write_segment(folder, start, 10, TASHKENT, fps=FPS, gop=FPS)
    archive = _archive(tmp_path)

    assert archive.clip(camera, start + timedelta(hours=1), start + timedelta(hours=1, seconds=20)) is None
    assert archive.clip(uuid4(), start, start + timedelta(seconds=5)) is None  # камера не пишется
    assert FileVideoArchive(tmp_path / "missing", TASHKENT, tmp_path / "cache").clip(camera, start, start) is None


def test_broken_file_gives_none(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-entry"
    mark_camera(folder, camera)
    start = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    broken = folder / "2026-10-12" / "15-00-00.mp4"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)

    assert _archive(tmp_path).clip(camera, start, start + timedelta(seconds=5)) is None
    assert not list((tmp_path / "cache").glob("*.part"))  # недоделанный ролик не остаётся


def test_cached_clip_is_rebuilt_when_recording_grows(tmp_path: Path) -> None:
    camera, folder = uuid4(), tmp_path / "video" / "cam-exit"
    mark_camera(folder, camera)
    start = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
    write_segment(folder, start, 6, TASHKENT, fps=FPS, gop=FPS)
    archive = _archive(tmp_path)
    window = (start + timedelta(seconds=2), start + timedelta(seconds=9))

    early = archive.clip(camera, *window)  # машина только что выехала — запись ещё дописывается
    assert early is not None and archive.clip(camera, *window) == early
    assert len(_frames(early)) <= 41

    write_segment(folder, start, 10, TASHKENT, fps=FPS, gop=FPS)  # файл дописан
    later = archive.clip(camera, *window)
    assert later is not None and later != early and 69 <= len(_frames(later)) <= 71
