"""Смоук после деплоя (фаза 9 §5): реальное camera-событие в staging/production и проверка полного пути через API:
ENTRY → сессия ACTIVE → EXIT → обязательство (сага) → наличные кассиром → сессия CLOSED → платёж виден в API.
Окружение: SMOKE_BASE_URL, SMOKE_CAMERA_KEY_ENTRY/EXIT (ключи камер выделенной смоук-площадки), SMOKE_CASHIER_USER/PASSWORD
(кассир со сменой на этой площадке), SMOKE_CAMERA_CODE_ENTRY/EXIT (коды камер ключей), опционально SMOKE_LOT_ID. Возвращает 0 только при пройденном пути; иначе — откат (cd.yml).

Локально: python scripts/smoke_staging.py --stack   (берёт стенд e2e_stack.py: frontend/e2e/.stack.json)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


def wait(predicate: Any, *, limit: float, every: float = 0.5, what: str = "") -> float:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if predicate():
            return time.monotonic() - started
        time.sleep(every)
    raise SystemExit(f"smoke: {what or 'condition'} not met within {limit}s")


class Smoke:
    def __init__(
        self,
        base: str,
        keys: dict[str, str],
        codes: dict[str, str],
        user: str,
        password: str,
        lot_id: str | None,
        timeout: float,
    ) -> None:
        self.base, self.keys, self.codes, self.lot_id, self.timeout = base.rstrip("/"), keys, codes, lot_id, timeout
        self.http = httpx.Client(timeout=15)
        self.plate = f"01S{uuid.uuid4().int % 900 + 100:03d}MK"  # уникальный номер смоука
        self.run_id = f"smoke-{datetime.now(tz=UTC):%Y%m%dT%H%M%S}"
        login = self.http.post(f"{self.base}/api/v1/auth/login", json={"email": user, "password": password})
        if login.status_code != 200:
            raise SystemExit(f"smoke: login {login.status_code}")
        self.auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    def event(self, kind: str, at: datetime) -> dict[str, Any]:
        resp = self.http.post(
            f"{self.base}/api/v1/camera/events",
            headers={"X-Camera-Key": self.keys[kind], "X-Correlation-Id": str(uuid.uuid4())},
            json={
                "camera_code": self.codes[kind],
                "event_type": kind,
                "plate_number": self.plate,
                "confidence": 97,
                "event_time": at.isoformat(),
                "direction": "IN" if kind == "ENTRY" else "OUT",
                "recognition": {"provider": "smoke", "version": self.run_id},
            },
        )
        if resp.status_code not in (200, 202):
            raise SystemExit(f"smoke: {kind} → {resp.status_code} {resp.text[:200]}")
        return dict(resp.json())

    def session(self, sid: str) -> dict[str, Any]:
        r = self.http.get(f"{self.base}/api/v1/sessions/{sid}", headers=self.auth)
        return dict(r.json()) if r.status_code == 200 else {}

    def pay_cash(self, sid: str) -> None:
        def payment_id() -> str | None:
            r = self.http.get(f"{self.base}/api/v1/sessions/{sid}/exit-saga", headers=self.auth)
            return r.json().get("payment_id") if r.status_code == 200 and r.json() else None

        wait(lambda: payment_id() is not None, limit=self.timeout, what="obligation")
        pid = payment_id()
        obligation = self.http.get(f"{self.base}/api/v1/payments/{pid}", headers=self.auth).json()
        if self.lot_id:  # смена кассира на смоук-площадке (409 — уже открыта)
            shift = self.http.post(
                f"{self.base}/api/v1/shifts", headers=self.auth, json={"lot_id": self.lot_id, "opening_float": 0}
            )
            if shift.status_code not in (200, 201, 409):
                raise SystemExit(f"smoke: shift {shift.status_code} {shift.text[:200]}")
        paid = self.http.post(
            f"{self.base}/api/v1/payments/{pid}/confirm-cash",
            headers=self.auth,
            json={"amount_minor": obligation["amount_minor"], "reason": f"smoke {self.run_id}"},
        )
        if paid.status_code not in (200, 201) or paid.json().get("status") != "PAID":
            raise SystemExit(f"smoke: cash {paid.status_code} {paid.text[:200]}")

    def run(self) -> str:
        started = time.monotonic()
        # 1. ENTRY (9 минут назад: обычный въезд, не «поздний») → сессия ACTIVE
        entered = self.event("ENTRY", datetime.now(tz=UTC) - timedelta(minutes=9))
        if entered["status"] != "ENTRY_CONFIRMED" or not entered.get("session_id"):
            raise SystemExit(f"smoke: ENTRY не подтверждён: {entered}")
        sid = entered["session_id"]
        wait(lambda: self.session(sid).get("status") == "ACTIVE", limit=self.timeout, what="session ACTIVE")
        # 2. EXIT → сага → обязательство и наличные, либо бесплатный выезд (льгота/грейс)
        exited = self.event("EXIT", datetime.now(tz=UTC))
        if exited["status"] == "PAYMENT_REQUIRED":
            self.pay_cash(sid)
        elif exited["status"] not in ("EXIT_CONFIRMED", "EXIT_IN_PROGRESS"):
            raise SystemExit(f"smoke: EXIT неожиданный статус {exited}")
        # 3. сессия закрыта (шлагбаум открыт контроллером или SKIPPED без шлагбаума), платёж виден в API
        wait(lambda: self.session(sid).get("status") == "CLOSED", limit=self.timeout, what="session CLOSED")
        payments = self.http.get(f"{self.base}/api/v1/sessions/{sid}/payments", headers=self.auth)
        if payments.status_code != 200:
            raise SystemExit(f"smoke: payments {payments.status_code}")
        return (
            f"SMOKE OK {self.run_id}: {self.base} ENTRY→ACTIVE→EXIT→{exited['status']}→CLOSED, payments={len(payments.json())}, "
            f"exit_kind={self.session(sid).get('exit_kind')}, {time.monotonic() - started:.1f}s"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", action="store_true", help="локальный стенд e2e_stack.py вместо переменных окружения")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    if args.stack:
        state = json.loads((ROOT / "frontend" / "e2e" / ".stack.json").read_text())
        cashier = state["users"]["cashier"]
        smoke = Smoke(
            state["api"],
            {"ENTRY": state["camera_keys"]["entry"], "EXIT": state["camera_keys"]["exit"]},
            {"ENTRY": "ENTRY_01", "EXIT": "EXIT_01"},
            cashier["email"],
            cashier["password"],
            state["lot_id"],
            args.timeout,
        )
    else:
        env = os.environ
        smoke = Smoke(
            env["SMOKE_BASE_URL"],
            {"ENTRY": env["SMOKE_CAMERA_KEY_ENTRY"], "EXIT": env["SMOKE_CAMERA_KEY_EXIT"]},
            {
                "ENTRY": env.get("SMOKE_CAMERA_CODE_ENTRY", "SMOKE_IN"),
                "EXIT": env.get("SMOKE_CAMERA_CODE_EXIT", "SMOKE_OUT"),
            },
            env["SMOKE_CASHIER_USER"],
            env["SMOKE_CASHIER_PASSWORD"],
            env.get("SMOKE_LOT_ID"),
            args.timeout,
        )
    print(smoke.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
