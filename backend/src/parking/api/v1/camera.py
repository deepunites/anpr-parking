"""Camera API (фаза 4 §1–2, §5): события, heartbeat, presigned-загрузка, vendor-адаптеры Hikvision/Dahua.
Аутентификация — X-Camera-Key; tenant/площадка/камера берутся из ключа, значения тела прав не расширяют."""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from defusedxml import ElementTree as ET  # noqa: N817 — запрет DOCTYPE/ENTITY/внешних сущностей на уровне парсера
from fastapi import APIRouter, Depends, Header, Path, Request, Response
from pydantic import BaseModel, Field

from parking.api.deps import client_info, get_container
from parking.api.errors import status_for
from parking.modules.anpr.application.ingest import CameraEventInput
from parking.modules.devices.application.contracts import CameraPrincipal, Heartbeat
from parking.modules.integrations.application.contracts import JournalEntry, Outcome, mask_plate
from parking.runtime.container import Container
from parking.shared.correlation import get_correlation_id, tenant_id_var
from parking.shared.errors import DomainError, ValidationError

router = APIRouter(prefix="/api/v1/camera", tags=["camera"])

CAMERA_AUTH = {"x-public": True, "x-camera-auth": True}
MAX_EVENT_BODY = 8 * 1024


async def camera_principal(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    x_camera_key: Annotated[str | None, Header()] = None,
) -> CameraPrincipal:
    principal = await container.camera_auth.authenticate(x_camera_key, client_ip=client_info(request).ip)
    tenant_id_var.set(principal.tenant_id)
    return principal


async def camera_principal_from_path(
    request: Request,
    container: Annotated[Container, Depends(get_container)],
    camera_key: Annotated[
        str, Path(min_length=16, max_length=200, description="ключ камеры (камеры без своего заголовка)")
    ],
) -> CameraPrincipal:
    """Hikvision/Dahua в режиме «HTTP-хост» не умеют добавлять заголовок — ключ приходит последним сегментом пути.
    В журнал запросов пишется шаблон роута, в трассы и Sentry — маска (platform/telemetry.py, platform/sentry.py)."""
    principal = await container.camera_auth.authenticate(camera_key, client_ip=client_info(request).ip)
    tenant_id_var.set(principal.tenant_id)
    return principal


class BoundingBox(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=0)
    h: int = Field(ge=0)


class Recognition(BaseModel):
    provider: str | None = Field(default=None, max_length=50)
    version: str | None = Field(default=None, max_length=50)
    plate_format: str | None = Field(default=None, max_length=30)
    country_code: str | None = Field(default=None, max_length=3)
    image_quality: int | None = Field(default=None, ge=0, le=100)
    bounding_box: BoundingBox | None = None


class CameraEventRequest(BaseModel):
    camera_code: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]{1,30}$")
    event_type: str = Field(pattern=r"^(ENTRY|EXIT)$")
    plate_number: str = Field(min_length=1, max_length=20)
    confidence: int = Field(ge=0, le=100)
    event_time: datetime
    image_object_key: str | None = Field(default=None, max_length=255)
    direction: str | None = Field(default=None, pattern=r"^(IN|OUT)$")
    recognition: Recognition | None = None


class CameraEventResponse(BaseModel):
    status: str
    session_id: str | None
    barrier_command_id: str | None
    correlation_id: str
    duplicate: bool
    queued: bool = False
    event_id: str | None = None
    saga_id: str | None = None


class HeartbeatRequest(BaseModel):
    camera_code: str | None = Field(default=None, max_length=30)
    firmware_version: str | None = Field(default=None, max_length=100)
    uptime_seconds: int | None = Field(default=None, ge=0)


class PresignResponse(BaseModel):
    url: str
    fields: dict[str, str]
    object_key: str
    expires_in: int
    max_bytes: int
    content_type: str = "image/jpeg"


def _to_input(body: CameraEventRequest) -> CameraEventInput:
    return CameraEventInput(
        camera_code=body.camera_code,
        event_type=body.event_type,
        plate_number=body.plate_number,
        confidence=body.confidence,
        event_time=body.event_time,
        image_object_key=body.image_object_key,
        direction=body.direction,
        recognition=body.recognition.model_dump(exclude_none=True) if body.recognition else None,
    )


def _journal(
    container: Container,
    principal: CameraPrincipal,
    *,
    source: str,
    started: float,
    outcome: str,
    data: CameraEventInput | None = None,
    status: str | None = None,
    http_status: int | None = None,
    message: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """v2 этап 6: журнал событий — в буфер процесса, в БД пачкой раз в секунду (горячий путь без лишних запросов)."""
    try:
        corr: UUID | None = UUID(correlation_id or get_correlation_id())
    except ValueError:
        corr = None
    entry = JournalEntry(
        tenant_id=principal.tenant_id,
        lot_id=principal.lot_id,
        camera_id=principal.camera_id,
        source=source,
        outcome=outcome,
        received_at=container.clock.now(),
        status=status,
        http_status=http_status,
        event_type=data.event_type if data is not None else None,
        plate_masked=mask_plate(data.plate_number) if data is not None else None,
        message=(message or "")[:300] or None,
        correlation_id=corr,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    container.journal.enqueue(entry)


def _journal_error(
    container: Container,
    principal: CameraPrincipal,
    source: str,
    started: float,
    exc: BaseException,
    data: CameraEventInput | None = None,
) -> None:
    if isinstance(exc, DomainError):
        _journal(
            container,
            principal,
            source=source,
            started=started,
            outcome=Outcome.REJECTED,
            data=data,
            status=exc.code,
            http_status=status_for(exc),
            message=exc.message,
        )
    else:
        _journal(
            container,
            principal,
            source=source,
            started=started,
            outcome=Outcome.ERROR,
            data=data,
            status=type(exc).__name__,
            http_status=500,
        )


async def _ingest(
    container: Container,
    principal: CameraPrincipal,
    data: CameraEventInput,
    response: Response,
    *,
    source: str = "camera",
    started: float | None = None,
) -> CameraEventResponse:
    started = started if started is not None else time.perf_counter()
    try:
        result = await container.ingest.process(principal, data)
    except Exception as exc:
        _journal_error(container, principal, source, started, exc, data)
        raise
    outcome = Outcome.DUPLICATE if result.duplicate else Outcome.QUEUED if result.queued else Outcome.ACCEPTED
    _journal(
        container,
        principal,
        source=source,
        started=started,
        outcome=outcome,
        data=data,
        status=result.status,
        http_status=result.http_status,
        correlation_id=result.correlation_id,
    )
    response.status_code = result.http_status
    return CameraEventResponse(**result.to_dict())


def hikvision_input(
    values: dict[str, str],
    *,
    code: str,
    event_type: str | None,
    now: datetime,
    image_key: str | None,
    camera_direction: str | None = None,
) -> CameraEventInput:
    """EventNotificationAlert → вход приёма (общий для камеры и песочницы).

    Тип события — по назначению камеры (IN → въезд, OUT → выезд): камера каждой полосы смотрит на подъезжающие машины
    и для неё любое движение — «forward». Направление из XML используется, только если назначение не известно."""
    direction = values.get("direction", "").lower()
    if event_type:
        kind = event_type
    elif camera_direction in {"IN", "OUT"}:
        kind = "ENTRY" if camera_direction == "IN" else "EXIT"
    else:
        kind = "EXIT" if direction in {"reverse", "out"} else "ENTRY"
    try:
        confidence = int(values.get("confidenceLevel") or values.get("confidence") or 0)
        event_time = datetime.fromisoformat(values.get("dateTime") or now.isoformat())
    except ValueError as exc:
        raise ValidationError("invalid confidence or dateTime in Hikvision XML") from exc
    return CameraEventInput(
        camera_code=code,
        event_type=kind,
        plate_number=values.get("licensePlate") or values.get("plateNumber") or "",
        confidence=confidence,
        event_time=event_time,
        image_object_key=image_key,
        recognition={
            "provider": "hikvision",
            "version": values.get("ipAddress"),
            "country_code": values.get("country"),
        },
    )


def dahua_input(
    payload: dict[str, Any], *, code: str, now: datetime, image_key: str | None, camera_direction: str | None = None
) -> CameraEventInput:
    """Тип события — по назначению камеры (как у Hikvision): камера выезда видит подъезжающую машину как «Approach»,
    и по Direction из JSON это был бы въезд → DIRECTION_MISMATCH на каждом выезде."""
    picture = payload.get("Picture") or {}
    plate = (picture.get("Plate") or {}).get("PlateNumber") or payload.get("PlateNumber") or ""
    snap = picture.get("SnapInfo") or {}
    direction = str(snap.get("Direction") or payload.get("Direction") or "").lower()
    try:
        confidence = int((picture.get("Plate") or {}).get("Confidence") or snap.get("Confidence") or 0)
        event_time = datetime.fromisoformat(str(snap.get("SnapTime") or now.isoformat()))
    except ValueError as exc:
        raise ValidationError("invalid Confidence or SnapTime in Dahua JSON") from exc
    return CameraEventInput(
        camera_code=code,
        event_type=(
            ("ENTRY" if camera_direction == "IN" else "EXIT")
            if camera_direction in {"IN", "OUT"}
            else ("EXIT" if direction in {"leave", "out", "reverse"} else "ENTRY")
        ),
        plate_number=str(plate),
        confidence=confidence,
        event_time=event_time,
        image_object_key=image_key,
        recognition={"provider": "dahua", "plate_format": (picture.get("Plate") or {}).get("PlateType")},
    )


@router.post(
    "/events",
    response_model=CameraEventResponse,
    openapi_extra=CAMERA_AUTH,
    responses={
        202: {"description": "PostgreSQL недоступен — событие принято в оффлайн-буфер (queued=true)"},
        413: {"description": "тело больше 8 КиБ (BodyLimitMiddleware): изображение не передаётся, только ключ"},
    },
)
async def camera_event(
    body: CameraEventRequest,
    response: Response,
    principal: Annotated[CameraPrincipal, Depends(camera_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> CameraEventResponse:
    return await _ingest(container, principal, _to_input(body), response)


@router.post("/heartbeat", status_code=204, openapi_extra=CAMERA_AUTH)
async def camera_heartbeat(
    body: HeartbeatRequest,
    principal: Annotated[CameraPrincipal, Depends(camera_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> Response:
    """Только Redis (HSET + TTL); в PostgreSQL сбрасывает джоб здоровья раз в 60 с."""
    hb = Heartbeat(camera_id=principal.camera_id, at=container.clock.now(), firmware_version=body.firmware_version)
    await container.heartbeats.record(hb, ttl_seconds=3600)
    return Response(status_code=204)


@router.post("/media/presign", response_model=PresignResponse, openapi_extra=CAMERA_AUTH)
async def presign_upload(
    principal: Annotated[CameraPrincipal, Depends(camera_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> PresignResponse:
    """Presigned POST в MinIO: ключ навязан сервером, Content-Type image/jpeg, размер ≤ media_max_bytes, TTL минуты."""
    p = await container.media.presign_upload(principal)
    return PresignResponse(
        url=p.url, fields=p.fields, object_key=p.object_key, expires_in=p.expires_in, max_bytes=p.max_bytes
    )


# ---------- vendor-адаптеры ----------

_XML_FORBIDDEN = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)


def _parse_xml(raw: bytes) -> dict[str, str]:
    """Hikvision ANPR (EventNotificationAlert): плоские теги; DOCTYPE/ENTITY запрещены (XXE)."""
    if len(raw) > MAX_EVENT_BODY or _XML_FORBIDDEN.search(raw):
        raise ValidationError("unsupported XML payload", code="BODY_TOO_LARGE")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:  # defusedxml: DefusedXmlException — подкласс ValueError
        raise ValidationError("malformed XML") from exc
    values: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if element.text and element.text.strip() and tag not in values:
            values[tag] = element.text.strip()
    return values


_NO_PLATE = {"", "unknown", "nocarplate", "无车牌"}


async def _hikvision_parts(request: Request, max_picture: int) -> tuple[bytes, bytes | None, str | None]:
    """XML события и кадр из любого варианта Hikvision: multipart с частью anpr.xml (или любой .xml/text/xml); без
    multipart — тело целиком XML (служебные события). Кадр — самый крупный jpg: общий кадр машины (detectionPicture,
    vehiclePicture), а вырез номера licensePlatePicture (1–5 КБ) — только если общего нет. На MediaPark (2026-10-15)
    в карточке стоял один вырез номера: по нему не видно, какая это машина."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return await request.body(), None, None
    form = await request.form(max_files=8, max_fields=16, max_part_size=max_picture)
    xml: bytes | None = None
    picture: bytes | None = None
    event_type: str | None = None
    for name, value in form.multi_items():
        if isinstance(value, str):
            if name == "event_type":
                event_type = value
            elif xml is None and value.lstrip().startswith("<"):
                xml = value.encode()
            continue
        filename = (value.filename or name).lower()
        ctype = (value.content_type or "").lower()
        if xml is None and (filename.endswith(".xml") or "xml" in ctype):
            xml = await value.read()
        elif filename.endswith((".jpg", ".jpeg")) or "jpeg" in ctype:
            data = await value.read()
            if picture is None or len(data) > len(picture):
                picture = data
    if xml is None:
        raise ValidationError("Hikvision event without XML part")
    return xml, picture, event_type


async def _hikvision(
    request: Request, response: Response, principal: CameraPrincipal, container: Container
) -> CameraEventResponse:
    started = time.perf_counter()
    try:
        raw_xml, picture, event_type = await _hikvision_parts(request, container.settings.media_max_bytes)
        values = _parse_xml(raw_xml)
        kind = values.get("eventType", "ANPR").upper()
        plate = (values.get("licensePlate") or values.get("plateNumber") or "").strip()
        if kind == "HEARTBEAT":
            # «Интервал контрольных сигналов» камеры: камера на связи — как POST /camera/heartbeat (Redis, без БД)
            await container.heartbeats.record(
                Heartbeat(camera_id=principal.camera_id, at=container.clock.now(), firmware_version=None),
                ttl_seconds=3600,
            )
            return CameraEventResponse(
                status="HEARTBEAT",
                session_id=None,
                barrier_command_id=None,
                correlation_id=get_correlation_id(),
                duplicate=False,
            )
        if kind != "ANPR" or plate.lower() in _NO_PLATE:
            # videoloss и прочие служебные события, проезд без номера: камера не должна повторять отправку — 200
            _journal(
                container,
                principal,
                source="hikvision",
                started=started,
                outcome=Outcome.REJECTED,
                status="IGNORED",
                http_status=200,
                message=f"eventType={kind}" + ("" if plate else ", no plate"),
            )
            return CameraEventResponse(
                status="IGNORED",
                session_id=None,
                barrier_command_id=None,
                correlation_id=get_correlation_id(),
                duplicate=False,
            )
        image_key = await container.media.store_frame(principal, picture) if picture else None
        data = hikvision_input(
            values,
            code=principal.code,
            event_type=event_type,
            now=container.clock.now(),
            image_key=image_key,
            camera_direction=principal.direction,
        )
    except Exception as exc:
        _journal_error(container, principal, "hikvision", started, exc)
        raise
    return await _ingest(container, principal, data, response, source="hikvision", started=started)


@router.post(
    "/vendor/hikvision",
    response_model=CameraEventResponse,
    openapi_extra=CAMERA_AUTH,
    responses={202: {"description": "принято в оффлайн-буфер"}},
)
async def hikvision_event(
    request: Request,
    response: Response,
    principal: Annotated[CameraPrincipal, Depends(camera_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> CameraEventResponse:
    """Ключ в заголовке X-Camera-Key. multipart/form-data: XML EventNotificationAlert (часть anpr.xml) + кадр jpg."""
    return await _hikvision(request, response, principal, container)


@router.post(
    "/vendor/hikvision/{camera_key}",
    response_model=CameraEventResponse,
    openapi_extra=CAMERA_AUTH,
    responses={202: {"description": "принято в оффлайн-буфер"}},
)
async def hikvision_event_by_path(
    request: Request,
    response: Response,
    principal: Annotated[CameraPrincipal, Depends(camera_principal_from_path)],
    container: Annotated[Container, Depends(get_container)],
) -> CameraEventResponse:
    """Для настоящей камеры: в веб-интерфейсе Hikvision (Сеть → Расширенные → HTTP-прослушивание / «Хост
    уведомлений») задаётся адрес сервера и путь `/api/v1/camera/vendor/hikvision/<ключ камеры>` — свой заголовок
    камера добавить не может. Служебные события (heartBeat и др.) и проезды без номера — 200 IGNORED."""
    return await _hikvision(request, response, principal, container)


@router.post(
    "/vendor/dahua",
    response_model=CameraEventResponse,
    openapi_extra=CAMERA_AUTH,
    responses={202: {"description": "принято в оффлайн-буфер"}},
)
async def dahua_event(
    request: Request,
    response: Response,
    principal: Annotated[CameraPrincipal, Depends(camera_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> CameraEventResponse:
    """JSON Dahua (Picture.Plate.PlateNumber, Picture.SnapInfo.Direction, Picture.NormalPic.Content base64)."""
    return await _dahua(request, response, principal, container)


@router.post(
    "/vendor/dahua/{camera_key}",
    response_model=CameraEventResponse,
    openapi_extra=CAMERA_AUTH,
    responses={202: {"description": "принято в оффлайн-буфер"}},
)
async def dahua_event_by_path(
    request: Request,
    response: Response,
    principal: Annotated[CameraPrincipal, Depends(camera_principal_from_path)],
    container: Annotated[Container, Depends(get_container)],
) -> CameraEventResponse:
    """Ключ камеры последним сегментом пути — для камер, которые не умеют свой заголовок."""
    return await _dahua(request, response, principal, container)


async def _dahua(
    request: Request, response: Response, principal: CameraPrincipal, container: Container
) -> CameraEventResponse:
    started = time.perf_counter()
    try:
        raw = await request.body()
        if len(raw) > container.settings.media_max_bytes + MAX_EVENT_BODY:
            raise ValidationError("request body too large", code="BODY_TOO_LARGE")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except ValueError as exc:
            raise ValidationError("malformed JSON") from exc
        image_key: str | None = None
        content = ((payload.get("Picture") or {}).get("NormalPic") or {}).get("Content")
        if content:
            image_key = await container.media.store_frame(principal, base64.b64decode(content))
        data = dahua_input(
            payload,
            code=principal.code,
            now=container.clock.now(),
            image_key=image_key,
            camera_direction=principal.direction,
        )
    except Exception as exc:
        _journal_error(container, principal, "dahua", started, exc)
        raise
    return await _ingest(container, principal, data, response, source="dahua", started=started)
