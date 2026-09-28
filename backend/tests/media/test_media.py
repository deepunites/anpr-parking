"""Media (фаза 4 §5) против реального MinIO: presigned POST с политикой (размер, Content-Type), presigned GET по tenant,
vendor-адаптеры кладут кадр сами, lifecycle policy бакета применена (mc ilm rule ls)."""

from __future__ import annotations

import base64
import subprocess
import sys
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from parking.modules.identity.application.contracts import Role
from parking.modules.media.domain.object_key import key_belongs_to, parse_key
from parking.runtime.container import Container
from tests.conftest import BACKEND, CAM_A1_IN, LOT_A1, TENANT_A
from tests.support.api_fixtures import Principal, bearer
from tests.support.camera_fixtures import event_body, send_event, unique_plate

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"\xff\xd9"


def test_object_key_policy() -> None:
    key = f"tenant/{TENANT_A}/lot/{LOT_A1}/camera/{CAM_A1_IN}/2026/09/19/{UUID(int=7)}.jpg"
    assert key_belongs_to(key, TENANT_A, LOT_A1, CAM_A1_IN)
    assert not key_belongs_to(key, TENANT_A, LOT_A1, UUID(int=1))
    assert parse_key("tenant/x/lot/y/camera/z/2026/09/19/a.jpg") is None
    assert parse_key(f"tenant/{TENANT_A}/lot/{LOT_A1}/camera/{CAM_A1_IN}/2026/09/19/../x.jpg") is None
    assert parse_key(key.replace(".jpg", ".sh")) is None


async def _presign(client: httpx.AsyncClient, key: str) -> dict[str, object]:
    response = await client.post("/api/v1/camera/media/presign", headers={"X-Camera-Key": key})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def test_presigned_upload_policy_and_view(
    minio: str,
    client: httpx.AsyncClient,
    container: Container,
    entry_key: str,
    users: dict[Role, Principal],
) -> None:
    presigned = await _presign(client, entry_key)
    fields = dict(presigned["fields"])  # type: ignore[call-overload]
    assert presigned["content_type"] == "image/jpeg" and presigned["max_bytes"] == container.settings.media_max_bytes
    assert key_belongs_to(str(presigned["object_key"]), TENANT_A, LOT_A1, CAM_A1_IN)
    async with httpx.AsyncClient(timeout=10) as s3:
        ok = await s3.post(str(presigned["url"]), data=fields, files={"file": ("frame.jpg", JPEG, "image/jpeg")})
        assert ok.status_code == 204, ok.text
        too_big = await s3.post(
            str(presigned["url"]),
            data=fields,
            files={"file": ("big.jpg", b"\xff\xd8" + b"\0" * (container.settings.media_max_bytes + 1), "image/jpeg")},
        )
        assert too_big.status_code == 400 and "EntityTooLarge" in too_big.text
        executable = await s3.post(
            str(presigned["url"]),
            data={**fields, "Content-Type": "application/x-sh"},
            files={"file": ("evil.sh", b"#!/bin/sh", "application/x-sh")},
        )
        assert executable.status_code == 403, executable.text  # политика: Content-Type зафиксирован image/jpeg
        other_key = str(presigned["object_key"]).replace(str(CAM_A1_IN), str(UUID(int=9)))
        hijack = await s3.post(
            str(presigned["url"]), data={**fields, "key": other_key}, files={"file": ("x.jpg", JPEG, "image/jpeg")}
        )
        assert hijack.status_code == 403  # ключ подписан: подменить объект нельзя

    event = await send_event(
        client, entry_key, event_body(unique_plate(), image_object_key=str(presigned["object_key"]))
    )
    assert event.status_code == 200, event.text
    session_id = event.json()["session_id"]
    headers = await bearer(client, users[Role.OPERATOR])
    images = await client.get(f"/api/v1/sessions/{session_id}/images", headers=headers)
    assert images.status_code == 200, images.text
    assert len(images.json()) == 1 and images.json()[0]["object_key"] == presigned["object_key"]
    assert images.json()[0]["expires_in"] == container.settings.s3_presign_get_ttl_seconds
    async with httpx.AsyncClient(timeout=10) as s3:
        view = await s3.get(images.json()[0]["url"])
        assert view.status_code == 200 and view.content == JPEG
    foreign = await client.get(
        f"/api/v1/sessions/{session_id}/images", headers=await bearer(client, users[Role.VIEWER])
    )
    assert foreign.status_code == 200  # тот же tenant, sessions:read
    print(
        "\nGATE media: presigned POST ok=204, oversize=400 EntityTooLarge, executable type=403, key hijack=403; presigned GET 200"
    )


async def test_vendor_adapters_store_frame(minio: str, client: httpx.AsyncClient, entry_key: str) -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <ipAddress>10.10.1.5</ipAddress><dateTime>{datetime.now(tz=UTC).isoformat()}</dateTime>
      <eventType>ANPR</eventType>
      <ANPR><licensePlate>{unique_plate()}</licensePlate><confidenceLevel>97</confidenceLevel><direction>forward</direction>
      <country>UZ</country></ANPR>
    </EventNotificationAlert>"""
    files = {
        "anpr.xml": ("anpr.xml", xml.encode(), "application/xml"),
        "licensePlatePicture.jpg": ("p.jpg", JPEG, "image/jpeg"),
    }
    hik = await client.post("/api/v1/camera/vendor/hikvision", files=files, headers={"X-Camera-Key": entry_key})
    assert hik.status_code == 200, hik.text
    assert hik.json()["status"] == "ENTRY_CONFIRMED"
    xxe = xml.replace("<?xml", '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><?xml', 1)
    bad = await client.post(
        "/api/v1/camera/vendor/hikvision",
        files={"anpr.xml": ("anpr.xml", xxe.encode(), "application/xml")},
        headers={"X-Camera-Key": entry_key},
    )
    assert bad.status_code == 422
    dahua = {
        "Picture": {
            "Plate": {"PlateNumber": unique_plate(), "Confidence": 93, "PlateType": "Normal"},
            "SnapInfo": {"Direction": "Enter", "SnapTime": datetime.now(tz=UTC).isoformat()},
            "NormalPic": {"Content": base64.b64encode(JPEG).decode()},
        }
    }
    dh = await client.post("/api/v1/camera/vendor/dahua", json=dahua, headers={"X-Camera-Key": entry_key})
    assert dh.status_code == 200, dh.text
    assert dh.json()["status"] == "ENTRY_CONFIRMED"


@pytest.mark.skipif(sys.platform != "linux", reason="mc binary")
def test_bucket_lifecycle_policy_applied(minio: str) -> None:
    """Gate: policy реально применена — вывод mc ilm rule ls."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); import minio_dev; print(minio_dev.mc(9000, 'ilm', 'rule', 'ls', 'local/anpr-media').stdout)",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND,
        check=True,
    )
    print("\nGATE mc ilm rule ls local/anpr-media:\n" + result.stdout)
    assert "90" in result.stdout and "Enabled" in result.stdout
