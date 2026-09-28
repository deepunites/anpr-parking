"""argon2id. Параметры — из конфига; дефолты подобраны на машине разработки (docs/backend/argon2-bench.out):
t=2, m=64 MiB, p=1 → 249 мс на Celeron N4020. На боевом железе пересчитать `scripts/bench_argon2.py` (цель 100–250 мс).
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


class Argon2Hasher:
    def __init__(self, *, time_cost: int = 2, memory_kib: int = 64 * 1024, parallelism: int = 1) -> None:
        self._ph = _Argon2(
            time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism, hash_len=32, salt_len=16, type=Type.ID
        )
        # хеш заведомо не совпадающего пароля: verify() над ним занимает столько же, сколько над настоящим
        self._dummy = self._ph.hash("dummy-password-for-constant-time-" + "x" * 32)

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._ph.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._ph.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    @property
    def dummy_hash(self) -> str:
        return self._dummy
