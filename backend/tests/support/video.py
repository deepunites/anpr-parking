"""Файлы записи как у сервиса video (deploy/truenas/video): фрагментированный MP4 с H.264, ключевой кадр раз в
gop кадров, имя — местное время начала. Картинка крошечная — тесту важны время и ключевые кадры, а не изображение."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import av

FRAGMENTED = {"movflags": "+frag_keyframe+empty_moov+default_base_moof"}


def mark_camera(folder: Path, camera_id: UUID) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".camera").write_text(f"{camera_id}\n")


def write_segment(
    folder: Path, start: datetime, seconds: float, tz: ZoneInfo, *, fps: int = 10, gop: int = 10, bframes: int = 0
) -> Path:
    """<folder>/<ГГГГ-ММ-ДД>/<ЧЧ-ММ-СС>.mp4 с началом start; время изменения файла — конец записи."""
    local = start.astimezone(tz)
    path = folder / local.strftime("%Y-%m-%d") / local.strftime("%H-%M-%S.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="mp4", options=FRAGMENTED) as output:
        stream = output.add_stream(
            "libx264",
            rate=fps,
            options={
                "preset": "ultrafast",
                "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes={bframes}",
            },
        )
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(round(seconds * fps)):
            frame = av.VideoFrame(64, 48, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([(i * 7) % 256]) * plane.buffer_size)
            frame.pts, frame.time_base = i, Fraction(1, fps)
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    end = (start + timedelta(seconds=seconds)).timestamp()
    os.utime(path, (end, end))
    return path
