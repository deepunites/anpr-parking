"""Протоколы входящих вызовов провайдеров: разбор, проверка подписи, рендер ответа. Никакой бизнес-логики —
она в WebhookService. Написано по публичной документации Payme Merchant API и Click SHOP API (требует
подтверждения на тестовых аккаунтах)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from parking.modules.billing.application.webhook_types import (
    Action,
    InboundEvent,
    Outcome,
    WebhookProtocol,
    WebhookResult,
)
from parking.modules.billing.domain.payment import Provider
from parking.shared.errors import ValidationError

PAYME_METHODS = {
    "CheckPerformTransaction": Action.CHECK,
    "CreateTransaction": Action.CREATE,
    "PerformTransaction": Action.PERFORM,
    "CancelTransaction": Action.CANCEL,
    "CheckTransaction": Action.STATUS,
    "GetStatement": Action.STATEMENT,
}
PAYME_ERRORS: dict[Outcome, tuple[int, str]] = {
    Outcome.SIGNATURE: (-32504, "Insufficient privilege to perform this method"),
    Outcome.REPLAY: (-31008, "Request time is out of the allowed window"),
    Outcome.ORDER_NOT_FOUND: (-31050, "Order not found"),
    Outcome.AMOUNT_MISMATCH: (-31001, "Wrong amount"),
    Outcome.TX_NOT_FOUND: (-31003, "Transaction not found"),
    Outcome.ALREADY_PAID: (-31051, "Order is already paid"),
    Outcome.ORDER_BUSY: (-31099, "Order is busy with another transaction"),
    Outcome.CANNOT_PERFORM: (-31008, "Unable to perform operation"),
    Outcome.UNKNOWN_ACTION: (-32601, "Method not found"),
    Outcome.MALFORMED: (-32600, "Invalid request"),
}
CLICK_ERRORS: dict[Outcome, tuple[int, str]] = {
    Outcome.SIGNATURE: (-1, "SIGN CHECK FAILED!"),
    Outcome.REPLAY: (-8, "Error in request from click"),
    Outcome.ORDER_NOT_FOUND: (-5, "User does not exist"),
    Outcome.AMOUNT_MISMATCH: (-2, "Incorrect parameter amount"),
    Outcome.TX_NOT_FOUND: (-6, "Transaction does not exist"),
    Outcome.ALREADY_PAID: (-4, "Already paid"),
    Outcome.ORDER_BUSY: (-4, "Already paid"),
    Outcome.CANNOT_PERFORM: (-9, "Transaction cancelled"),
    Outcome.UNKNOWN_ACTION: (-3, "Action not found"),
    Outcome.MALFORMED: (-8, "Error in request from click"),
}


def _ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


class PaymeMerchantApi:
    """JSON-RPC 2.0 от Payme; auth — Basic Paycom:<key>. Мерчант — ?merchant=<id> в URL колбэка (настраивается в
    кабинете), tenant — по нему. Суммы — тийины. Идентификатор события — <method>:<params.id> (id транзакции Payme),
    для CheckPerformTransaction/GetStatement — method + хеш параметров."""

    provider = Provider.PAYME

    def parse(self, headers: dict[str, str], body: bytes, query: dict[str, str]) -> InboundEvent:
        try:
            data = json.loads(body)
            method = str(data["method"])
            params = dict(data.get("params") or {})
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("malformed JSON-RPC request") from exc
        merchant = query.get("merchant", "")
        action = PAYME_METHODS.get(method)
        if action is None:
            raise ValidationError("unknown method", code="UNKNOWN_ACTION")
        tx_id = str(params["id"]) if params.get("id") is not None else None
        order = (params.get("account") or {}).get("order_id")
        payment_id = _uuid(order)
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:24]
        event_id = f"{method}:{tx_id}" if tx_id else f"{method}:{digest}"
        time_ms = params.get("time")
        event_time = datetime.fromtimestamp(int(time_ms) / 1000, tz=UTC) if time_ms else None
        return InboundEvent(
            provider=Provider.PAYME,
            merchant_id=merchant,
            provider_event_id=event_id,
            action=action,
            provider_transaction_id=tx_id,
            payment_id=payment_id,
            amount=int(params["amount"]) if params.get("amount") is not None else None,
            event_time=event_time,
            rpc_id=data.get("id"),
            payload={"method": method, "params": params},
            extra={"reason": params.get("reason"), "from": params.get("from"), "to": params.get("to")},
        )

    def verify(self, event: InboundEvent, headers: dict[str, str], secret_json: str) -> bool:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        expected = "Paycom:" + json.loads(secret_json)["key"]
        return hmac.compare_digest(decoded.encode(), expected.encode())

    def render(self, event: InboundEvent, result: WebhookResult) -> tuple[int, dict[str, Any]]:
        if result.is_error:
            code, message = PAYME_ERRORS[result.outcome]
            return 200, {"jsonrpc": "2.0", "id": event.rpc_id, "error": {"code": code, "message": message}}
        d = result.data
        body: dict[str, Any]
        if result.outcome is Outcome.ALLOW:
            body = {"allow": True}
        elif result.outcome is Outcome.CREATED:
            body = {"create_time": _ms(d.get("create_time")), "transaction": d.get("transaction"), "state": 1}
        elif result.outcome is Outcome.PERFORMED:
            body = {"transaction": d.get("transaction"), "perform_time": _ms(d.get("perform_time")), "state": 2}
        elif result.outcome is Outcome.CANCELLED:
            body = {
                "transaction": d.get("transaction"),
                "cancel_time": _ms(d.get("cancel_time")),
                "state": int(d.get("state", -1)),
            }
        elif result.outcome is Outcome.STATUS:
            body = {
                "create_time": _ms(d.get("create_time")),
                "perform_time": _ms(d.get("perform_time")),
                "cancel_time": _ms(d.get("cancel_time")),
                "transaction": d.get("transaction"),
                "state": int(d.get("state", 0)),
                "reason": d.get("reason"),
            }
        else:
            body = {"transactions": list(d.get("transactions") or [])}
        return 200, {"jsonrpc": "2.0", "id": event.rpc_id, "result": body}


class ClickShopApi:
    """Click SHOP API: Prepare (action=0) и Complete (action=1) как form-urlencoded; sign_string =
    md5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id + [merchant_prepare_id] + amount + action + sign_time).
    Мерчант — service_id из тела. Суммы Click в сумах ("5000.00") → тийины."""

    provider = Provider.CLICK

    def parse(self, headers: dict[str, str], body: bytes, query: dict[str, str]) -> InboundEvent:
        content_type = headers.get("content-type", "")
        try:
            if "json" in content_type:
                raw: dict[str, Any] = json.loads(body)
                form = {k: str(v) for k, v in raw.items()}
            else:
                form = {k: v[-1] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()}
            action_raw = int(form["action"])
            amount = round(float(form["amount"]) * 100)
            click_tx = form["click_trans_id"]
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            raise ValidationError("malformed Click request") from exc
        error = int(form.get("error", "0") or 0)
        if action_raw == 0:
            action = Action.CHECK
        elif action_raw == 1:
            action = Action.CANCEL if error < 0 else Action.PERFORM
        else:
            raise ValidationError("unknown action", code="UNKNOWN_ACTION")
        try:
            event_time: datetime | None = datetime.strptime(form["sign_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except (KeyError, ValueError):
            event_time = None
        payload = {k: v for k, v in form.items() if k != "sign_string"}
        return InboundEvent(
            provider=Provider.CLICK,
            merchant_id=form.get("service_id", ""),
            provider_event_id=f"{click_tx}:{action_raw}",
            action=action,
            provider_transaction_id=click_tx,
            payment_id=_uuid(form.get("merchant_trans_id")),
            amount=amount,
            event_time=event_time,
            payload=payload,
            extra={
                "sign_string": form.get("sign_string", ""),
                "merchant_prepare_id": form.get("merchant_prepare_id"),
                "error": error,
            },
        )

    def verify(self, event: InboundEvent, headers: dict[str, str], secret_json: str) -> bool:
        secret = json.loads(secret_json)["secret_key"]
        p = event.payload
        prepare = p.get("merchant_prepare_id", "") if p.get("action") == "1" else ""
        raw = f"{p.get('click_trans_id', '')}{p.get('service_id', '')}{secret}{p.get('merchant_trans_id', '')}{prepare}{p.get('amount', '')}{p.get('action', '')}{p.get('sign_time', '')}"
        expected = hashlib.md5(raw.encode()).hexdigest()  # noqa: S324 # nosec B324
        return hmac.compare_digest(expected, str(event.extra.get("sign_string", "")))

    def render(self, event: InboundEvent, result: WebhookResult) -> tuple[int, dict[str, Any]]:
        base = {
            "click_trans_id": event.provider_transaction_id,
            "merchant_trans_id": event.payload.get("merchant_trans_id"),
        }
        if result.is_error:
            code, note = CLICK_ERRORS[result.outcome]
            return 200, {**base, "error": code, "error_note": note}
        if result.outcome is Outcome.ALLOW:
            return 200, {
                **base,
                "merchant_prepare_id": result.data.get("merchant_prepare_id"),
                "error": 0,
                "error_note": "Success",
            }
        if result.outcome is Outcome.CANCELLED:  # Complete с error<0 от Click: подтверждаем отмену кодом -9
            return 200, {**base, "merchant_confirm_id": None, "error": -9, "error_note": "Transaction cancelled"}
        return 200, {
            **base,
            "merchant_confirm_id": result.data.get("merchant_confirm_id"),
            "error": 0,
            "error_note": "Success",
        }


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


PROTOCOLS: dict[Provider, WebhookProtocol] = {Provider.PAYME: PaymeMerchantApi(), Provider.CLICK: ClickShopApi()}
