"""Minimal stand-in for the Toss Payments REST API, used by E2E servers.

Run directly: ``python -m tests.e2e.toss_stub <port>`` (port 0 picks a free one and prints it).
"""

import base64
import socket
import sys
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

STUB_SECRET = "test_sk_stub"
STUB_CLIENT = "test_ck_stub"
_EXPECTED_AUTH = "Basic " + base64.b64encode(f"{STUB_SECRET}:".encode()).decode("ascii")

app = FastAPI(title="toss-stub")
payments: dict[str, dict[str, Any]] = {}
billing_keys: dict[str, str] = {}
state: dict[str, bool] = {"decline": False}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"code": code, "message": message or code}, status_code=status)


@app.middleware("http")
async def require_secret_key(request: Request, call_next: Any) -> Response:
    if not request.url.path.startswith("/__stub/") and (
        request.headers.get("authorization") != _EXPECTED_AUTH
    ):
        return _error(401, "UNAUTHORIZED_KEY", "invalid secret key")
    response: Response = await call_next(request)
    return response


@app.get("/__stub/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/__stub/decline")
async def set_decline(request: Request) -> dict[str, bool]:
    body = await request.json()
    state["decline"] = bool(body.get("decline"))
    return {"decline": state["decline"]}


def _store_payment(payment_key: str, order_id: str, amount: int) -> dict[str, Any]:
    payment = {
        "paymentKey": payment_key,
        "orderId": order_id,
        "totalAmount": amount,
        "status": "DONE",
        "approvedAt": _now(),
    }
    payments[payment_key] = payment
    return payment


@app.post("/v1/payments/confirm")
async def confirm(request: Request) -> Any:
    body = await request.json()
    return _store_payment(str(body["paymentKey"]), str(body["orderId"]), int(body["amount"]))


@app.get("/v1/payments/{payment_key}")
def lookup(payment_key: str) -> Any:
    payment = payments.get(payment_key)
    return payment if payment else _error(404, "NOT_FOUND_PAYMENT")


@app.post("/v1/payments/{payment_key}/cancel")
async def cancel(payment_key: str, request: Request) -> Any:
    payment = payments.get(payment_key)
    if payment is None:
        return _error(404, "NOT_FOUND_PAYMENT")
    body = await request.json()
    payment["status"] = "CANCELED"
    payment["cancels"] = [{"cancelReason": body.get("cancelReason", ""), "canceledAt": _now()}]
    return payment


@app.post("/v1/billing/authorizations/issue")
async def issue(request: Request) -> Any:
    body = await request.json()
    auth_key = str(body.get("authKey", ""))
    customer_key = str(body.get("customerKey", ""))
    if not auth_key.startswith("stub_auth"):
        return _error(400, "NOT_FOUND_BILLING_AUTH")
    billing_key = "stub_bk_" + customer_key
    billing_keys[billing_key] = customer_key
    return {
        "billingKey": billing_key,
        "customerKey": customer_key,
        "card": {"company": "테스트카드", "number": "1234****5678"},
    }


@app.post("/v1/billing/{billing_key}")
async def charge(billing_key: str, request: Request) -> Any:
    if billing_key not in billing_keys:
        return _error(404, "NOT_FOUND_BILLING_KEY")
    if state["decline"]:
        return _error(400, "REJECT_CARD_PAYMENT")
    body = await request.json()
    order_id = str(body["orderId"])
    return _store_payment("stubpay_" + order_id, order_id, int(body["amount"]))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("localhost", 0))
            port = int(sock.getsockname()[1])
    print(f"STUB_PORT={port}", flush=True)
    uvicorn.run(app, host="localhost", port=port, log_level="warning")


if __name__ == "__main__":
    main()
