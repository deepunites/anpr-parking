"""Подбор параметров argon2id под целевое время хеширования 100–250 мс на целевой машине (фаза 3.2)."""

from __future__ import annotations

import statistics
import time

from argon2 import PasswordHasher
from argon2.low_level import Type

CANDIDATES = [
    (2, 19 * 1024, 1),
    (2, 32 * 1024, 1),
    (3, 24 * 1024, 1),
    (1, 64 * 1024, 1),
    (2, 64 * 1024, 1),
    (3, 64 * 1024, 1),
    (2, 128 * 1024, 1),
]


def main() -> None:
    for time_cost, memory_kib, parallelism in CANDIDATES:
        ph = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism, hash_len=32, salt_len=16, type=Type.ID
        )
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            ph.hash("correct horse battery staple")
            samples.append((time.perf_counter() - started) * 1000)
        print(
            f"t={time_cost} m={memory_kib // 1024}MiB p={parallelism}: median {statistics.median(samples):.0f} ms, max {max(samples):.0f} ms"
        )


if __name__ == "__main__":
    main()
