"""Redis 7.4 без Docker и root: пакет conda-forge распаковывается в backend/.redis (нужен Python ≥ 3.14 ради zstd в stdlib
либо `uv pip install zstandard`). Зависимости: системные libssl.so.3 / libcrypto.so.3.

    python3 scripts/fetch_redis.py            # скачать и распаковать
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://api.anaconda.org/download/conda-forge/redis-server/7.4.7/linux-64/redis-server-7.4.7-h35e630c_0.conda"
TARGET = Path(__file__).resolve().parent.parent / ".redis"


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(data)
    except ImportError:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def main() -> int:
    if (TARGET / "bin" / "redis-server").exists():
        print(f"redis already at {TARGET}")
        return 0
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:
        archive = resp.read()
    z = zipfile.ZipFile(io.BytesIO(archive))
    TARGET.mkdir(parents=True, exist_ok=True)
    for name in z.namelist():
        if name.startswith("pkg-") and name.endswith(".tar.zst"):
            tarfile.open(fileobj=io.BytesIO(_zstd_decompress(z.read(name)))).extractall(TARGET, filter="data")
    os.chmod(TARGET / "bin" / "redis-server", 0o755)
    print(f"installed: {os.listdir(TARGET / 'bin')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
