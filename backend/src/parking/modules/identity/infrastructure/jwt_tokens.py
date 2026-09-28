"""JWT EdDSA (Ed25519). Выбор против RS256: ключи 32 байта, подпись/проверка быстрее, детерминирована,
нет параметров (padding), которые можно сконфигурировать неправильно. Список permissions в токен не кладётся."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import jwt

from parking.shared.errors import AuthenticationError

ALGORITHM = "EdDSA"


class JwtAccessTokens:
    def __init__(self, *, private_key_pem: str, public_key_pem: str, issuer: str, ttl: timedelta) -> None:
        self._private = private_key_pem
        self._public = public_key_pem
        self._issuer = issuer
        self._ttl = ttl

    def issue(self, *, user_id: UUID, tenant_id: UUID, role: str, token_version: int, now: datetime) -> tuple[str, int]:
        claims = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": role,
            "token_version": token_version,
            "jti": str(uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + self._ttl).timestamp()),
            "iss": self._issuer,
        }
        return jwt.encode(claims, self._private, algorithm=ALGORITHM), int(self._ttl.total_seconds())

    def verify(self, token: str, now: datetime) -> dict[str, object]:
        try:
            claims: dict[str, object] = jwt.decode(
                token,
                self._public,
                algorithms=[ALGORITHM],  # только EdDSA: 'none' и подмена на HS256 с публичным ключом отвергаются
                issuer=self._issuer,
                options={"require": ["sub", "tenant_id", "role", "token_version", "jti", "iat", "exp", "iss"]},
                leeway=5,
            )
        except jwt.PyJWTError:
            raise AuthenticationError("invalid credentials") from None
        if int(str(claims["exp"])) <= int(now.timestamp()) - 5:
            raise AuthenticationError("invalid credentials")
        return claims
