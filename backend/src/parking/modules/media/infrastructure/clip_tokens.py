"""Ссылка на видеоролик сессии: тег <video> не умеет заголовок Authorization, поэтому право смотреть ролик —
в самой ссылке, как у presigned GET фото. Подпись — тем же ключом Ed25519, что и токены входа, но с отдельной
аудиторией: ссылка на ролик не годится как токен входа, а токен входа — как ссылка. Живёт минуты; внутри — только
камера и окно времени (что именно показать), их проверил сервис при выдаче (сессия своя и в скоупе площадок)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from parking.modules.media.application.video import ClipRef
from parking.shared.errors import AuthenticationError

ALGORITHM = "EdDSA"
AUDIENCE = "parking-video-clip"


class JwtClipTokens:
    def __init__(self, *, private_key_pem: str, public_key_pem: str, issuer: str, ttl: timedelta) -> None:
        self._private = private_key_pem
        self._public = public_key_pem
        self._issuer = issuer
        self._ttl = ttl

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue(self, ref: ClipRef, now: datetime) -> str:
        claims = {
            "sub": str(ref.session_id),
            "cam": str(ref.camera_id),
            "from": int(ref.start.timestamp()),
            "to": int(ref.end.timestamp()),
            "iat": int(now.timestamp()),
            "exp": int((now + self._ttl).timestamp()),
            "iss": self._issuer,
            "aud": AUDIENCE,
        }
        return jwt.encode(claims, self._private, algorithm=ALGORITHM)

    def verify(self, token: str, now: datetime) -> ClipRef:
        try:
            claims = jwt.decode(
                token,
                self._public,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=AUDIENCE,
                options={"require": ["sub", "cam", "from", "to", "iat", "exp", "iss", "aud"], "verify_exp": False},
            )
            if int(claims["exp"]) <= int(now.timestamp()):  # часы сервиса, а не time.time() библиотеки (тесты)
                raise AuthenticationError("video link expired")
            return ClipRef(
                session_id=UUID(str(claims["sub"])),
                camera_id=UUID(str(claims["cam"])),
                start=datetime.fromtimestamp(int(claims["from"]), UTC),
                end=datetime.fromtimestamp(int(claims["to"]), UTC),
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AuthenticationError("invalid video link") from None
