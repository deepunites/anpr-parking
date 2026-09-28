"""§7: тайминговая атака на сравнение ключа камеры — распределение времени ответа для «неверный секрет при известном
префиксе» и «неизвестный префикс» не должно раскрывать, на каком байте
разошёлся HMAC. Сравнение — hmac.compare_digest; префикс ищется по индексу (одинаково для всех).
Замер in-process (ASGITransport): 120 запросов на класс, сравниваются медианы и 90-й перцентиль."""

from __future__ import annotations

import statistics
import time

import httpx
import pytest

from parking.modules.devices.domain.camera import KEY_PREFIX_LEN
from tests.support.camera_fixtures import event_body

SAMPLES = 120


async def _measure(client: httpx.AsyncClient, keys: dict[str, str], body: dict[str, object]) -> dict[str, list[float]]:
    """Классы замеряются вперемешку (round-robin), а не блоками: дрейф стенда (GC, прогрев, чужая нагрузка)
    ложится на все классы одинаково, иначе первый измеренный класс систематически медленнее."""
    timings: dict[str, list[float]] = {name: [] for name in keys}
    for _ in range(SAMPLES):
        for name, key in keys.items():
            started = time.perf_counter()
            resp = await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": key})
            timings[name].append((time.perf_counter() - started) * 1000)
            assert resp.status_code in {401, 422}, resp.text
    return timings


def _p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


@pytest.mark.slow
async def test_api_key_comparison_timing_is_flat(client: httpx.AsyncClient, entry_key: str) -> None:
    body = event_body("BAD PLATE !!")  # тело всё равно отвергается: измеряется только аутентификация
    secret = entry_key[KEY_PREFIX_LEN:]
    wrong_first_byte = entry_key[:KEY_PREFIX_LEN] + ("A" if secret[0] != "A" else "B") + secret[1:]
    wrong_last_byte = entry_key[:-1] + ("A" if entry_key[-1] != "A" else "B")
    unknown_prefix = "z" * KEY_PREFIX_LEN + secret
    # прогрев кешей (кандидаты по префиксу — в Redis, отрицательный результат тоже кешируется)
    for key in (wrong_first_byte, wrong_last_byte, unknown_prefix):
        await client.post("/api/v1/camera/events", json=body, headers={"X-Camera-Key": key})
    # верный ключ не входит в выборку: он проходит дальше по коду (валидация тела) и упирается в лимит 120/мин на ключ
    measured = await _measure(
        client, {"first": wrong_first_byte, "last": wrong_last_byte, "unknown": unknown_prefix}, body
    )
    med = {name: statistics.median(v) for name, v in measured.items()}
    p90 = {name: _p(v, 0.9) for name, v in measured.items()}
    print(
        f"\nGATE timing (ms): median first-byte={med['first']:.2f} last-byte={med['last']:.2f} unknown-prefix={med['unknown']:.2f}; "
        f"p90 first={p90['first']:.2f} last={p90['last']:.2f} unknown={p90['unknown']:.2f} (n={SAMPLES} на класс)"
    )
    # различие «первый байт vs последний байт» — то, что даёт утечку при побайтовом сравнении: должно тонуть в шуме
    assert abs(med["first"] - med["last"]) < max(0.25 * med["last"], 0.5), med
    assert abs(p90["first"] - p90["last"]) < max(0.35 * p90["last"], 1.0), p90
    # неизвестный префикс отвечает из отрицательного кеша не «мгновенно» относительно неверного секрета (оба — 401 без БД)
    assert abs(med["unknown"] - med["last"]) < max(0.5 * med["last"], 1.0), med
