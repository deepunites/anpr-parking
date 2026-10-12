"""Видеоархив сервера площадки: круглосуточная запись камер (deploy/truenas/video.sh) лежит на диске файлами
<корень>/<папка камеры>/<ГГГГ-ММ-ДД>/<ЧЧ-ММ-СС>.mp4 — 10-минутные фрагментированные MP4, время в имени — местное
(часовой пояс записи). Какая папка чья — файл <папка>/.camera с id камеры (пишет video.sh).

Ролик к событию — кусок записи от «до» до «после», склеенный из одного-двух файлов без перекодирования (PyAV: только
перекладка пакетов, начало — с ближайшего ключевого кадра не позже начала окна). Ролик обычный MP4 с индексом в начале
(+faststart) — играет любой браузер и телефон, перемотка работает. Готовые ролики кешируются на диске; пока файл
записи растёт (машина выехала только что), ключ кеша меняется вместе с его размером.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import av

from parking.platform.logging import get_logger

log = get_logger("media.video")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE = re.compile(r"^(\d{2})-(\d{2})-(\d{2})\.mp4$")
CAMERA_MARKER = ".camera"
MAX_SEGMENT = timedelta(minutes=15)  # запись режется по 10 мин; запас на сбои часов и переподключения
CACHE_MAX_AGE_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class Segment:
    path: Path
    start: datetime  # по имени файла, UTC
    end: datetime  # время последней записи в файл, UTC
    size: int


class FileVideoArchive:
    def __init__(self, root: Path, timezone: ZoneInfo, cache_dir: Path) -> None:
        self._root = root
        self._tz = timezone
        self._cache = cache_dir

    def folder_for(self, camera_id: UUID) -> Path | None:
        try:
            markers = list(self._root.glob(f"*/{CAMERA_MARKER}"))
        except OSError:
            return None
        for marker in markers:
            try:
                if marker.read_text().strip() == str(camera_id):
                    return marker.parent
            except OSError:
                continue
        return None

    def segments(self, camera_id: UUID, start: datetime, end: datetime) -> list[Segment]:
        """Файлы записи камеры, пересекающие окно [start, end] (aware datetime), по времени."""
        folder = self.folder_for(camera_id)
        if folder is None:
            return []
        days = {(start - MAX_SEGMENT).astimezone(self._tz).date(), end.astimezone(self._tz).date()}
        found: list[Segment] = []
        for day in sorted(days):
            found.extend(self._day_segments(folder, day))
        found.sort(key=lambda s: s.start)
        return [s for s in found if s.start < end and s.end > start]

    def _day_segments(self, folder: Path, day: date) -> list[Segment]:
        directory = folder / day.isoformat()
        if not _DAY.match(directory.name):
            return []
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return []
        segments: list[Segment] = []
        for entry in entries:
            match = _FILE.match(entry.name)
            if match is None or not entry.is_file():
                continue
            hour, minute, second = (int(x) for x in match.groups())
            if hour > 23 or minute > 59 or second > 59:
                continue
            local = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=self._tz)
            stat = entry.stat()
            started = local.astimezone(ZoneInfo("UTC"))
            ended = datetime.fromtimestamp(stat.st_mtime, ZoneInfo("UTC"))
            if ended - started > MAX_SEGMENT:  # файл дописан не в своё время (часы, копирование) — по имени
                ended = started + MAX_SEGMENT
            segments.append(Segment(Path(entry.path), started, ended, stat.st_size))
        return segments

    def clip(self, camera_id: UUID, start: datetime, end: datetime) -> Path | None:
        """Путь к ролику [start, end] (из кеша или только что собранному); None — записи за это время нет."""
        segments = self.segments(camera_id, start, end)
        if not segments:
            return None
        fingerprint = "|".join(
            [str(camera_id), start.isoformat(), end.isoformat(), *(f"{s.path.name}:{s.size}" for s in segments)]
        )
        target = self._cache / f"{hashlib.sha256(fingerprint.encode()).hexdigest()[:32]}.mp4"
        if target.exists():
            return target
        self._cache.mkdir(parents=True, exist_ok=True)
        self._expire_cache()
        partial = target.with_suffix(f".{uuid4().hex}.part")  # параллельные запросы того же ролика
        try:
            written = remux(segments, start, end, partial)
            if written == 0:
                partial.unlink(missing_ok=True)
                return None
            partial.replace(target)
        except (av.FFmpegError, OSError) as exc:  # битый или недописанный файл записи — ролика нет, API не падает
            partial.unlink(missing_ok=True)
            log.warning("video_clip_failed", camera_id=str(camera_id), error=type(exc).__name__)
            return None
        return target

    def _expire_cache(self) -> None:
        cutoff = time.time() - CACHE_MAX_AGE_SECONDS
        for old in self._cache.glob("*"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                continue


def remux(segments: list[Segment], start: datetime, end: datetime, target: Path) -> int:
    """Пакеты видео с последнего ключевого кадра не позже start до end — в новый MP4 с отсчётом от нуля.
    Возвращает число записанных пакетов."""
    with av.open(str(target), "w", format="mp4", options={"movflags": "+faststart"}) as output:
        clip = _Clip(output, start)
        for segment in segments:
            if not _copy_segment(clip, segment, start, end):
                break
        return clip.written


def _copy_segment(clip: _Clip, segment: Segment, start: datetime, end: datetime) -> bool:
    """Пакеты одного файла записи; False — окно ролика кончилось."""
    with av.open(str(segment.path)) as source:
        stream = source.streams.video[0]
        rate = stream.time_base
        time_base = Fraction(rate.numerator, rate.denominator) if rate else Fraction(1, 90000)
        clip.ensure_stream(stream)
        into = (start - segment.start).total_seconds()
        if clip.base is None and into > 0:  # не читать весь 10-минутный файл с начала
            source.seek(int(into / time_base), stream=stream, backward=True, any_frame=False)
        for packet in source.demux(stream):
            if packet.pts is None or packet.dts is None:
                continue
            if segment.start + timedelta(seconds=float(packet.dts * time_base)) > end:
                return False  # по порядку декодирования: B-кадры до конца окна ссылаются на следующий P-кадр
            at = segment.start + timedelta(seconds=float(packet.pts * time_base))
            clip.feed(packet, at, segment.start, time_base)
    return True


class _Clip:
    """Выходной ролик: начало отсчёта — первый записанный ключевой кадр; кадры с последнего ключевого до начала окна
    придерживаются, чтобы ролик начался с картинки, а не с серого экрана."""

    def __init__(self, output: av.container.OutputContainer, start: datetime) -> None:
        self._output = output
        self._start = start
        self._stream: av.video.stream.VideoStream | None = None
        self._gop: list[tuple[av.Packet[av.video.stream.VideoStream], datetime, Fraction]] = []
        self._last_dts: int | None = None
        self._lead = 0.0  # B-кадры: декодирование первого кадра раньше показа — сдвиг, чтобы DTS не ушёл в минус
        self.base: datetime | None = None
        self.written = 0

    def ensure_stream(self, template: av.video.stream.VideoStream) -> None:
        if self._stream is None:
            self._stream = self._output.add_stream_from_template(template)

    def feed(
        self, packet: av.Packet[av.video.stream.VideoStream], at: datetime, segment_start: datetime, time_base: Fraction
    ) -> None:
        if self.base is None:
            if at < self._start:
                if packet.is_keyframe:
                    self._gop = []
                if self._gop or packet.is_keyframe:
                    self._gop.append((packet, segment_start, time_base))
                return
            if not self._gop and not packet.is_keyframe:
                return  # окно началось посреди группы кадров, а ключевого до него нет — ждём ключевой
            first, first_start, first_base = self._gop[0] if self._gop else (packet, segment_start, time_base)
            assert first.pts is not None and first.dts is not None
            self.base = first_start + timedelta(seconds=float(first.pts * first_base))
            self._lead = float(max(0, first.pts - first.dts) * first_base)
            for held, held_start, held_base in self._gop:
                self._write(held, held_start, held_base)
            self._gop = []
        self._write(packet, segment_start, time_base)

    def _write(
        self, packet: av.Packet[av.video.stream.VideoStream], segment_start: datetime, time_base: Fraction
    ) -> None:
        assert self.base is not None and self._stream is not None
        assert packet.pts is not None and packet.dts is not None
        shift = round(((segment_start - self.base).total_seconds() + self._lead) / time_base)
        packet.pts += shift
        packet.dts += shift
        if packet.dts < 0 or (self._last_dts is not None and packet.dts <= self._last_dts):
            return  # стык двух файлов: кадр уже был в предыдущем
        self._last_dts = packet.dts
        packet.time_base = time_base
        packet.stream = self._stream
        self._output.mux(packet)
        self.written += 1
