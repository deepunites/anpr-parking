"""Ссылка на фото номера через API: на сервере площадки хранилище (MinIO) браузеру и телефонам кассиров не видно —
presigned GET указывал на внутренний адрес minio:9000, и фото в карточке не открывалось. Право смотреть — в самой
ссылке (тег <img> не шлёт Authorization), как у роликов: подпись Ed25519 с отдельной аудиторией, живёт минуты."""

from __future__ import annotations

from datetime import datetime, timedelta

import jwt

from parking.shared.errors import AuthenticationError

ALGORITHM = "EdDSA"
AUDIENCE = "parking-media-image"


class JwtImageLinks:
    def __init__(self, *, private_key_pem: str, public_key_pem: str, issuer: str, ttl: timedelta) -> None:
        self._private = private_key_pem
        self._public = public_key_pem
        self._issuer = issuer
        self._ttl = ttl

    def issue(self, key: str, now: datetime) -> str:
        claims = {
            "sub": key,
            "iat": int(now.timestamp()),
            "exp": int((now + self._ttl).timestamp()),
            "iss": self._issuer,
            "aud": AUDIENCE,
        }
        return jwt.encode(claims, self._private, algorithm=ALGORITHM)

    def verify(self, token: str, now: datetime) -> str:
        try:
            claims = jwt.decode(
                token,
                self._public,
                algorithms=[ALGORITHM],
                issuer=self._issuer,
                audience=AUDIENCE,
                options={"require": ["sub", "iat", "exp", "iss", "aud"], "verify_exp": False},
            )
        except jwt.PyJWTError:
            raise AuthenticationError("invalid image link") from None
        if int(claims["exp"]) <= int(now.timestamp()):
            raise AuthenticationError("image link expired")
        return str(claims["sub"])
