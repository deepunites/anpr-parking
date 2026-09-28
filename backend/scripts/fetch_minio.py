"""MinIO server + mc без Docker и root: пакеты conda-forge (статические Go-бинарники) → backend/.minio/bin.

python3 scripts/fetch_minio.py
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PACKAGES = {
    "minio": "https://api.anaconda.org/download/conda-forge/minio-server/2025.10.15.17.29.55/linux-64/minio-server-2025.10.15.17.29.55-hbcca054_0.conda",
    "mc": "https://api.anaconda.org/download/conda-forge/minio-client/{version}/linux-64/{basename}",
}
TARGET = Path(__file__).resolve().parent.parent / ".minio"


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(data)
    except ImportError:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def _latest_client() -> str:
    import json

    with urllib.request.urlopen("https://api.anaconda.org/package/conda-forge/minio-client", timeout=60) as resp:
        d = json.load(resp)
    files = sorted(
        [f for f in d["files"] if f["attrs"].get("subdir") == "linux-64"],
        key=lambda f: (f["version"], f["attrs"].get("build_number", 0)),
    )
    url = str(files[-1]["download_url"])
    return "https:" + url if url.startswith("//") else url


def _install(url: str) -> None:
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310 — фиксированные https URL conda-forge
        archive = resp.read()
    z = zipfile.ZipFile(io.BytesIO(archive))
    for name in z.namelist():
        if name.startswith("pkg-") and name.endswith(".tar.zst"):
            tarfile.open(fileobj=io.BytesIO(_zstd_decompress(z.read(name)))).extractall(TARGET, filter="data")


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    if not (TARGET / "bin" / "minio").exists():
        _install(PACKAGES["minio"])
    if not (TARGET / "bin" / "mc").exists():
        _install(_latest_client())
    for binary in ("minio", "mc"):
        os.chmod(TARGET / "bin" / binary, 0o755)
    print(f"installed: {sorted(os.listdir(TARGET / 'bin'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
