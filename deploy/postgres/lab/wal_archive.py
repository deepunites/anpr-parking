#!/usr/bin/env python
"""archive_command / restore_command для лаборатории: WAL-сегменты и базовые бэкапы в MinIO (S3) через boto3.

    wal_archive.py push <path> <name>      # archive_command = "python wal_archive.py push %p %f"
    wal_archive.py get  <name> <path>      # restore_command = "python wal_archive.py get %f %p"
    wal_archive.py put-file <path> <key>   # загрузка tar базового бэкапа
    wal_archive.py get-file <key> <path>
    wal_archive.py ls <prefix>

В production ту же роль выполняет pgBackRest (deploy/postgres/pgbackrest.conf): архивирование, полные/дифф. бэкапы,
шифрование репозитория и retention. Здесь — минимальная замена, чтобы учения PITR/восстановления были выполнены
реально, а не «по документации» (A-61).
Код выхода ≠ 0 при любой ошибке: PostgreSQL повторит archive_command, WAL не удаляется, пока не заархивирован.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

BUCKET = os.environ.get("PG_BACKUP_BUCKET", "pg-backups")
PREFIX = os.environ.get("PG_BACKUP_PREFIX", "lab")


def client():  # type: ignore[no-untyped-def]
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("PG_BACKUP_ENDPOINT", "http://127.0.0.1:9000"),
        aws_access_key_id=os.environ["PARKING_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["PARKING_S3_SECRET_KEY"],
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        region_name="us-east-1",
    )


def main(argv: list[str]) -> int:
    cmd, *args = argv
    s3 = client()
    if cmd == "push":
        path, name = args
        key = f"{PREFIX}/wal/{name}"
        digest = hashlib.md5(Path(path).read_bytes(), usedforsecurity=False).hexdigest()  # noqa: S324 — ETag S3
        try:  # идемпотентно: сегмент уже есть с тем же содержимым — успех (PostgreSQL может повторить archive_command);
            # другое содержимое под тем же именем = архив другого кластера → ошибка, а не тихая подмена
            head = s3.head_object(Bucket=BUCKET, Key=key)
            if head["ETag"].strip('"') == digest:
                return 0
            print(f"archive conflict: {key} exists with different content", file=sys.stderr)
            return 2
        except s3.exceptions.ClientError:
            pass
        s3.upload_file(path, BUCKET, key)
        return 0
    if cmd == "get":
        name, path = args
        try:
            s3.download_file(BUCKET, f"{PREFIX}/wal/{name}", path)
        except s3.exceptions.ClientError:
            return 1  # сегмента нет — конец восстановления (штатно для истории/timeline)
        return 0
    if cmd == "put-file":
        path, key = args
        s3.upload_file(path, BUCKET, f"{PREFIX}/{key}")
        return 0
    if cmd == "get-file":
        key, path = args
        s3.download_file(BUCKET, f"{PREFIX}/{key}", path)
        return 0
    if cmd == "purge":  # новый кластер лаборатории — новый архив (у pgBackRest — stanza с проверкой system identifier)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/"):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objects, "Quiet": True})
        return 0
    if cmd == "ls":
        (prefix,) = args or ("",)
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/{prefix}"):
            for obj in page.get("Contents", []):
                print(obj["Key"], obj["Size"])
        return 0
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
