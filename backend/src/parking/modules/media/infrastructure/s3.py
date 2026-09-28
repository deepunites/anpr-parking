"""MinIO/S3 через aioboto3: presigned POST с политикой (Content-Type, content-length-range), presigned GET, put."""

from __future__ import annotations

from typing import Any

import aioboto3
from botocore.config import Config

from parking.modules.media.application.ports import PresignedUpload
from parking.modules.media.domain.object_key import CONTENT_TYPE


class S3ObjectStore:
    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str, timeout: float = 5.0
    ) -> None:
        self._session = aioboto3.Session()
        self._kwargs: dict[str, Any] = {
            "endpoint_url": endpoint,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "config": Config(
                signature_version="s3v4", connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 1}
            ),
        }
        self._bucket = bucket
        self._client: Any = None
        self._cm: Any = None

    async def _s3(self) -> Any:
        if self._client is None:
            self._cm = self._session.client("s3", **self._kwargs)
            self._client = await self._cm.__aenter__()
        return self._client

    async def aclose(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = self._client = None

    async def presign_upload(self, key: str, *, max_bytes: int, ttl_seconds: int) -> PresignedUpload:
        s3 = await self._s3()
        post = await s3.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": CONTENT_TYPE},
            Conditions=[{"Content-Type": CONTENT_TYPE}, ["content-length-range", 1, max_bytes]],
            ExpiresIn=ttl_seconds,
        )
        return PresignedUpload(
            url=str(post["url"]),
            fields={str(k): str(v) for k, v in post["fields"].items()},
            object_key=key,
            expires_in=ttl_seconds,
            max_bytes=max_bytes,
        )

    async def presign_get(self, key: str, *, ttl_seconds: int) -> str:
        s3 = await self._s3()
        url: str = await s3.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl_seconds
        )
        return url

    async def put_object(self, key: str, data: bytes) -> None:
        s3 = await self._s3()
        await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=CONTENT_TYPE)

    async def delete_all_versions(self, key: str) -> int:
        """Privacy (фаза 10 §3): в версионируемом бакете обычный delete оставляет delete marker, а изображение
        остаётся восстановимым из старой версии — удаляем все версии и маркеры."""
        s3 = await self._s3()
        deleted = 0
        paginator = s3.get_paginator("list_object_versions")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=key):
            versions = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for group in ("Versions", "DeleteMarkers")
                for v in page.get(group, [])
                if v["Key"] == key
            ]
            if versions:
                await s3.delete_objects(Bucket=self._bucket, Delete={"Objects": versions, "Quiet": True})
                deleted += len(versions)
        return deleted
