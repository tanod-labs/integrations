"""Answers that tie up a payment are never retried and never paid again (settlement_unknown,
purchase_unresolved, credit_delivery_pending, settled-but-failed, no readable answer after paying).
Mocked HTTP only (httpx.MockTransport), throwaway unfunded key, no real payment."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from decimal import Decimal

import httpx
import pytest

from tanod import (
    AsyncTanod,
    CreditDeliveryPendingError,
    PaymentUnresolvedError,
    PurchaseUnresolvedError,
    ServiceUnavailableError,
    SettlementUnknownError,
    Tanod,
    TanodError,
)

from conftest import FakeAPI, b64json, response_402, settle_header

GAS = {"chain": "base", "gas_price_wei": "6000000", "gas_price_gwei": "0.006", "base_fee_gwei": "0.005"}
# The server's own messages (product/api/scanapi/paymentid.py _REFUSALS, app.py), shortened.
MESSAGES = {
    "settlement_unknown": "Settlement was submitted but its outcome is not confirmed. Keep this payment ID for "
                          "reconciliation; do not create a new payment or authorization for this unresolved purchase.",
    "purchase_unresolved": "An identical earlier purchase from this wallet is unresolved. This payment was verified "
                           "but not submitted or charged.",
    "credit_delivery_pending": "The payment is confirmed, but prepaid credit delivery is pending.",
}
CASES = [
    (503, "settlement_unknown", SettlementUnknownError, None),
    (409, "purchase_unresolved", PurchaseUnresolvedError, False),
    (503, "credit_delivery_pending", CreditDeliveryPendingError, True),
]


def make(handler, **kw) -> tuple[Tanod, FakeAPI]:
    api = FakeAPI(handler)
    kw.setdefault("use_env", False)
    return Tanod(http_client=httpx.Client(transport=httpx.MockTransport(api)), **kw), api


def paid_then(answer):
    """Unpaid attempts get a 402; every paid attempt gets ``answer()``."""
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        return answer()
    return h


def error(status: int, code: str, *, retry_after: str | None = "0", headers: dict | None = None) -> httpx.Response:
    hdrs = {"Retry-After": retry_after} if retry_after is not None else {}
    hdrs.update(headers or {})
    return httpx.Response(status, json={"error": {"code": code, "message": MESSAGES.get(code, code)}}, headers=hdrs)


def signed(api: FakeAPI, i: int) -> dict:
    return json.loads(base64.b64decode(api.requests[i].headers["PAYMENT-SIGNATURE"]))


def test_errors_are_not_service_unavailable():
    for cls in (SettlementUnknownError, PurchaseUnresolvedError, CreditDeliveryPendingError):
        assert issubclass(cls, PaymentUnresolvedError) and issubclass(cls, TanodError)
        assert not issubclass(cls, ServiceUnavailableError)   # an `except ServiceUnavailableError: retry` must miss it


@pytest.mark.parametrize("status,code,cls,charged", CASES)
def test_paid_unresolved_code_raises_without_retry_or_new_payment(throwaway_account, status, code, cls, charged):
    client, api = make(paid_then(lambda: error(status, code)), signer=throwaway_account, max_retries=3)
    with pytest.raises(cls) as ei:
        client.gas_price("base")
    e = ei.value
    assert len(api.requests) == 2                      # unpaid 402, paid: no retry, no second payment
    assert sum("PAYMENT-SIGNATURE" in r.headers for r in api.requests) == 1
    auth = signed(api, 1)["payload"]["authorization"]
    assert e.code == code and e.status_code == status and e.charged is charged
    assert e.server_message == MESSAGES[code]
    assert e.payer.lower() == throwaway_account.address.lower() == auth["from"].lower()
    assert e.nonce == auth["nonce"]
    assert e.payment_signature == api.requests[1].headers["PAYMENT-SIGNATURE"]
    assert e.price_usd == Decimal("0.001") and e.amount_atomic == "1000"
    assert code in str(e) and e.nonce in str(e) and "Do not pay again" in str(e)
    assert e.payment_signature not in str(e)
    assert client.spent_usd == Decimal("0.001")       # the signed payment stays counted


@pytest.mark.parametrize("status,code,cls,charged", CASES)
def test_unresolved_code_without_payment_is_not_retried(status, code, cls, charged):
    client, api = make(lambda req, n: error(status, code), max_retries=3)
    with pytest.raises(cls) as ei:
        client.gas_price("base")
    assert len(api.requests) == 1
    assert ei.value.nonce is None and ei.value.payment_signature is None


def test_settlement_unknown_ignores_retry_after_and_settlement_is_kept(throwaway_account):
    body = {"error": {"code": "settlement_unknown", "message": MESSAGES["settlement_unknown"]},
            "settlement": {"state": "uncertain"}}
    client, api = make(paid_then(lambda: httpx.Response(503, json=body, headers={"Retry-After": "0"})),
                       signer=throwaway_account)
    with pytest.raises(SettlementUnknownError) as ei:
        client.gas_price("base")
    assert ei.value.settlement == {"state": "uncertain"} and ei.value.body == body
    assert len(api.requests) == 2


@pytest.mark.parametrize("code", ["busy", "queue_full", "facilitator_error", "payment_identifier_unavailable"])
def test_paid_uncharged_503_still_retries_with_fresh_payment(throwaway_account, code):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        if n == 2:
            return error(503, code)
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header()})

    client, api = make(h, signer=throwaway_account)
    assert client.gas_price("base").meta.paid
    assert len(api.requests) == 4  # unpaid, paid(503), unpaid, paid
    assert signed(api, 1)["payload"]["authorization"]["nonce"] != signed(api, 3)["payload"]["authorization"]["nonce"]


def test_paid_error_with_settlement_receipt_is_charged_and_not_retried(throwaway_account):
    client, api = make(paid_then(lambda: error(503, "queue_full", headers={"PAYMENT-RESPONSE": settle_header()})),
                       signer=throwaway_account)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    e = ei.value
    assert type(e) is PaymentUnresolvedError and e.code == "queue_full" and e.charged is True
    assert e.receipt is not None and e.receipt.transaction == "0x" + "ab" * 32
    assert len(api.requests) == 2


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_paid_5xx_without_code_is_unresolved(throwaway_account, status):
    client, api = make(paid_then(lambda: httpx.Response(status, text="<html>Bad Gateway</html>",
                                                        headers={"Retry-After": "0"})),
                       signer=throwaway_account)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    assert ei.value.code is None and ei.value.charged is None and ei.value.nonce
    assert len(api.requests) == 2


def test_unpaid_5xx_without_code_is_still_service_unavailable():
    client, api = make(lambda req, n: httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(ServiceUnavailableError):
        client.gas_price("base")
    assert len(api.requests) == 1


@pytest.mark.parametrize("status,code,cls,charged", CASES)
def test_async_paid_unresolved_code(throwaway_account, status, code, cls, charged):
    api = FakeAPI(paid_then(lambda: error(status, code)))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=3,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    with pytest.raises(cls) as ei:
        asyncio.run(run())
    assert len(api.requests) == 2
    assert ei.value.charged is charged and ei.value.nonce == signed(api, 1)["payload"]["authorization"]["nonce"]


def test_async_paid_uncharged_503_still_retries(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        if n == 2:
            return error(503, "busy")
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header()})

    api = FakeAPI(h)

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    assert asyncio.run(run()).meta.paid
    assert len(api.requests) == 4


# -- M1: no answer at all after paying -------------------------------------------------------------


class _BrokenStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body that fails part-way (the connection dropped while the answer was read)."""

    def __iter__(self):
        yield b'{"chain": "ba'
        raise httpx.ReadError("connection reset by peer")

    async def __aiter__(self):
        yield b'{"chain": "ba'
        raise httpx.ReadError("connection reset by peer")


NO_ANSWER = {
    "read_timeout": lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timed out", request=req)),
    "connection_reset": lambda req: (_ for _ in ()).throw(httpx.ReadError("connection reset by peer", request=req)),
    "body_stream_error": lambda req: httpx.Response(200, stream=_BrokenStream(),
                                                    headers={"PAYMENT-RESPONSE": settle_header()}),
}


def paid_fails(failure):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        return failure(req)
    return h


def assert_no_answer(e: PaymentUnresolvedError, api: FakeAPI, kind: str) -> None:
    assert type(e) is PaymentUnresolvedError and e.code is None
    if kind == "body_stream_error":   # status and receipt arrived, the body did not: kept, as in the TS SDK
        assert e.charged is True and e.receipt is not None and e.status_code == 200
        assert "HTTP 200, body read failed" in str(e)
    else:
        assert e.charged is None and e.receipt is None and e.status_code is None
    assert isinstance(e.__cause__, httpx.HTTPError)
    auth = signed(api, 1)["payload"]["authorization"]
    assert e.nonce == auth["nonce"] and e.payer.lower() == auth["from"].lower()
    assert e.payment_signature == api.requests[1].headers["PAYMENT-SIGNATURE"]
    assert e.price_usd == Decimal("0.001") and e.amount_atomic == "1000"
    assert "no answer after the payment was sent" in str(e) and "Do not pay again" in str(e)
    assert len(api.requests) == 2
    assert sum("PAYMENT-SIGNATURE" in r.headers for r in api.requests) == 1


@pytest.mark.parametrize("kind", sorted(NO_ANSWER))
def test_paid_no_answer_is_unresolved(throwaway_account, kind):
    client, api = make(paid_fails(NO_ANSWER[kind]), signer=throwaway_account, max_retries=3)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    assert_no_answer(ei.value, api, kind)
    assert client.spent_usd == Decimal("0.001")


@pytest.mark.parametrize("kind", sorted(NO_ANSWER))
def test_async_paid_no_answer_is_unresolved(throwaway_account, kind):
    api = FakeAPI(paid_fails(NO_ANSWER[kind]))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=3,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    with pytest.raises(PaymentUnresolvedError) as ei:
        asyncio.run(run())
    assert_no_answer(ei.value, api, kind)


def test_unpaid_timeout_is_still_the_transport_error():
    client, api = make(lambda req, n: NO_ANSWER["read_timeout"](req))
    with pytest.raises(httpx.ReadTimeout):
        client.gas_price("base")


def failed_settle_header() -> str:
    return b64json({"success": False, "errorReason": "insufficient_funds", "transaction": "",
                    "network": "eip155:8453", "payer": "0x" + "11" * 20})


@pytest.mark.parametrize("status,headers,charged", [
    (200, {}, None),                                        # no receipt: unknown, not "charged"
    (200, {"PAYMENT-RESPONSE": "%%not-base64%%"}, None),    # a receipt that does not decode: unknown
    (200, {"PAYMENT-RESPONSE": "FAILED"}, None),            # a failed settlement receipt: unknown
    (503, {"PAYMENT-RESPONSE": "OK"}, True),                # an error answer whose receipt says it settled
])
def test_paid_body_read_failure_keeps_status_and_receipt(throwaway_account, status, headers, charged):
    hdrs = {k: {"OK": settle_header(), "FAILED": failed_settle_header()}.get(v, v) for k, v in headers.items()}
    client, api = make(paid_fails(lambda req: httpx.Response(status, stream=_BrokenStream(), headers=hdrs)),
                       signer=throwaway_account, max_retries=3)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    e = ei.value
    assert type(e) is PaymentUnresolvedError and e.charged is charged and e.status_code == status
    assert (e.receipt is not None) is (headers.get("PAYMENT-RESPONSE") in ("OK", "FAILED"))
    assert isinstance(e.__cause__, httpx.ReadError) and e.nonce and "body read failed" in str(e)
    assert len(api.requests) == 2 and sum("PAYMENT-SIGNATURE" in r.headers for r in api.requests) == 1


def test_async_paid_body_read_failure_keeps_receipt(throwaway_account):
    api = FakeAPI(paid_fails(NO_ANSWER["body_stream_error"]))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=3,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    with pytest.raises(PaymentUnresolvedError) as ei:
        asyncio.run(run())
    e = ei.value
    assert e.charged is True and e.receipt is not None and e.receipt.transaction == "0x" + "ab" * 32
    assert e.status_code == 200 and len(api.requests) == 2


def test_paid_answer_is_read_and_returned_as_before(throwaway_account):
    client, api = make(paid_then(lambda: httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header()})),
                       signer=throwaway_account)
    res = client.gas_price("base")
    assert res.meta.paid and res.meta.payment.transaction == "0x" + "ab" * 32 and len(api.requests) == 2


# -- Cancelled or interrupted after paying: re-raised unchanged, payer and nonce logged and noted --


class _HangingStream(httpx.AsyncByteStream):
    """A body that starts and then never finishes."""

    async def __aiter__(self):
        yield b'{"chain": "ba'
        await asyncio.Event().wait()


def assert_noted(exc: BaseException, api: FakeAPI, caplog: pytest.LogCaptureFixture) -> None:
    auth = signed(api, 1)["payload"]["authorization"]
    sig = api.requests[1].headers["PAYMENT-SIGNATURE"]
    warnings = [r.getMessage() for r in caplog.records if r.name == "tanod" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert auth["nonce"] in warnings[0] and auth["from"].lower() in warnings[0].lower()
    assert "Do not pay again" in warnings[0] and sig not in warnings[0]
    if sys.version_info >= (3, 11):
        notes = getattr(exc, "__notes__", [])
        assert len(notes) == 1 and auth["nonce"] in notes[0] and auth["from"].lower() in notes[0].lower()
        assert sig not in notes[0]
    assert len(api.requests) == 2 and sum("PAYMENT-SIGNATURE" in r.headers for r in api.requests) == 1


@pytest.mark.parametrize("where", ["send", "body"])
def test_async_cancel_after_paying_is_reraised_with_payer_and_nonce(throwaway_account, caplog, where):
    caplog.set_level(logging.INFO, logger="tanod")
    in_flight = asyncio.Event()

    async def hang(req):
        in_flight.set()
        if where == "body":
            return httpx.Response(200, stream=_HangingStream(), headers={"PAYMENT-RESPONSE": settle_header()})
        await asyncio.Event().wait()

    api = FakeAPI(paid_fails(hang))
    seen: list[BaseException] = []

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=3,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            try:
                return await c.gas_price("base")
            except BaseException as exc:
                seen.append(exc)
                raise

    async def main():
        task = asyncio.ensure_future(run())
        await asyncio.wait_for(in_flight.wait(), 5)
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError) as ei:
        asyncio.run(main())
    assert type(seen[0]) is asyncio.CancelledError   # the caller's own cancellation, not a PaymentUnresolvedError
    assert_noted(seen[0], api, caplog)
    # Python 3.10 wraps CancelledError at each Task boundary; the original
    # exception re-raised by the SDK remains in the cancellation context chain.
    cancelled = ei.value
    while cancelled is not seen[0] and isinstance(cancelled, asyncio.CancelledError):
        cancelled = cancelled.__context__
    assert cancelled is seen[0]


def test_async_wait_for_timeout_after_paying_keeps_payer_and_nonce(throwaway_account, caplog):
    async def hang(req):
        await asyncio.Event().wait()

    api = FakeAPI(paid_fails(hang))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await asyncio.wait_for(c.gas_price("base"), 0.5)

    with pytest.raises(asyncio.TimeoutError) as ei:
        asyncio.run(run())
    cancelled = ei.value.__cause__ or ei.value.__context__
    assert isinstance(cancelled, asyncio.CancelledError)
    assert_noted(cancelled, api, caplog)


def test_async_cancel_before_paying_adds_nothing(caplog):
    in_flight = asyncio.Event()

    async def hang(req, n):
        in_flight.set()
        await asyncio.Event().wait()

    api = FakeAPI(hang)

    async def main():
        async with AsyncTanod(use_env=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            task = asyncio.ensure_future(c.gas_price("base"))
            await asyncio.wait_for(in_flight.wait(), 5)
            task.cancel()
            await task

    with pytest.raises(asyncio.CancelledError) as ei:
        asyncio.run(main())
    assert not getattr(ei.value, "__notes__", None)
    assert not [r for r in caplog.records if r.name == "tanod" and r.levelno >= logging.WARNING]


class _Interrupt(BaseException):
    """Like gevent.Timeout or a signal handler's exception: not an ``Exception``."""


def test_sync_interrupt_after_paying_is_reraised_with_payer_and_nonce(throwaway_account, caplog):
    raised = _Interrupt("deadline")

    def interrupt(req):
        raise raised

    client, api = make(paid_fails(interrupt), signer=throwaway_account, max_retries=3)
    with pytest.raises(_Interrupt) as ei:
        client.gas_price("base")
    assert ei.value is raised
    assert_noted(raised, api, caplog)
    assert client.spent_usd == Decimal("0.001")


# -- S2: after paying, only documented uncharged codes are retried ---------------------------------


@pytest.mark.parametrize("status,code", [(503, "internal_error"), (503, "brand_new_code"), (500, "micro_error_x"),
                                         (429, "brand_new_limit"), (502, "upstream_hiccup")])
def test_paid_undocumented_code_is_unresolved(throwaway_account, status, code):
    client, api = make(paid_then(lambda: error(status, code)), signer=throwaway_account, max_retries=3)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    e = ei.value
    assert type(e) is PaymentUnresolvedError and e.code == code and e.charged is None and e.nonce
    assert "not a code the server documents as uncharged" in str(e)
    assert len(api.requests) == 2


def test_paid_refund_review_unavailable_is_charged_without_receipt(throwaway_account):
    client, api = make(paid_then(lambda: error(503, "refund_review_unavailable")), signer=throwaway_account)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    assert ei.value.code == "refund_review_unavailable" and ei.value.charged is True
    assert len(api.requests) == 2


@pytest.mark.parametrize("code", ["rate_limited", "unsettled_failures_in_flight", "image_uncharged_limited"])
def test_paid_uncharged_429_retries_with_fresh_payment(throwaway_account, code):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        if n == 2:
            return error(429, code)
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header()})

    client, api = make(h, signer=throwaway_account)
    assert client.gas_price("base").meta.paid
    assert len(api.requests) == 4
    assert signed(api, 1)["payload"]["authorization"]["nonce"] != signed(api, 3)["payload"]["authorization"]["nonce"]


def test_paid_uncharged_503_after_retries_is_service_unavailable(throwaway_account):
    client, api = make(paid_then(lambda: error(503, "queue_full")), signer=throwaway_account, max_retries=1)
    with pytest.raises(ServiceUnavailableError):
        client.gas_price("base")
    assert len(api.requests) == 4


def test_paid_input_refusal_is_still_invalid_request(throwaway_account):
    from tanod import InvalidRequestError

    client, api = make(paid_then(lambda: error(422, "invalid_input")), signer=throwaway_account)
    with pytest.raises(InvalidRequestError):
        client.gas_price("base")
    assert len(api.requests) == 2


# -- S4: a paid 2xx that cannot be read keeps its receipt ------------------------------------------


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(200, text="<html>not json</html>", headers={"PAYMENT-RESPONSE": settle_header()}),
    lambda: httpx.Response(200, json=["not", "an", "object"], headers={"PAYMENT-RESPONSE": settle_header()}),
    lambda: httpx.Response(200, json={"chain": 7, "gas_price_wei": {"x": 1}}, headers={"PAYMENT-RESPONSE": settle_header()}),
])
def test_paid_unreadable_2xx_is_unresolved_and_charged(throwaway_account, answer):
    client, api = make(paid_then(answer), signer=throwaway_account)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    e = ei.value
    assert e.charged is True and e.receipt is not None and e.receipt.transaction == "0x" + "ab" * 32
    assert e.nonce and e.status_code == 200 and "could not be read" in str(e)
    assert len(api.requests) == 2


def test_async_paid_unreadable_2xx_is_unresolved(throwaway_account):
    api = FakeAPI(paid_then(lambda: httpx.Response(200, text="nope", headers={"PAYMENT-RESPONSE": settle_header()})))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    with pytest.raises(PaymentUnresolvedError) as ei:
        asyncio.run(run())
    assert ei.value.charged is True and ei.value.receipt is not None


def test_unpaid_unreadable_2xx_is_a_plain_error():
    client, api = make(lambda req, n: httpx.Response(200, text="nope"))
    with pytest.raises(TanodError) as ei:
        client.gas_price("base")
    assert not isinstance(ei.value, PaymentUnresolvedError)


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(200, text="<html>free miss</html>"),
    lambda: httpx.Response(200, json=["not", "an", "object"]),
    lambda: httpx.Response(200, text="nope", headers={"PAYMENT-RESPONSE": "%%not-base64%%"}),
])
def test_paid_unreadable_2xx_without_receipt_is_not_called_charged(throwaway_account, answer):
    # The server's free-miss 200 carries no receipt and is not charged: charged stays unknown.
    client, api = make(paid_then(answer), signer=throwaway_account)
    with pytest.raises(PaymentUnresolvedError) as ei:
        client.gas_price("base")
    e = ei.value
    assert e.charged is None and e.receipt is None and e.nonce and e.status_code == 200
    assert "could not be read" in str(e) and len(api.requests) == 2


def test_async_paid_unreadable_2xx_without_receipt_is_not_called_charged(throwaway_account):
    api = FakeAPI(paid_then(lambda: httpx.Response(200, text="nope")))

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    with pytest.raises(PaymentUnresolvedError) as ei:
        asyncio.run(run())
    assert ei.value.charged is None and ei.value.receipt is None
