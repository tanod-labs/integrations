"""Sync (``Tanod``) and async (``AsyncTanod``) clients for https://tanod.dev.

Flow for every call:

1. Send the request unpaid, with the header ``X-Tanod-Free: 1`` (the server's free tier is
   opt-in over HTTP; ``use_free_tier=False`` leaves the header off, so every call is paid).
   If the free daily tier covers it, the answer is a 200.
2. On ``402 Payment Required``: if the 402 says the input would be refused, raise
   ``InvalidRequestError`` (nothing is signed). If the payee is not on the allow-list
   (``payees``, by default Tanod's own), raise ``PayeeMismatchError``. Without a wallet, raise
   ``PaymentRequiredError`` with the quoted price. Above ``max_price_usd``, raise
   ``PriceLimitExceededError``. Past this client's ``max_spend_usd``, raise
   ``BudgetExceededError``. Otherwise reserve the amount against the budget, sign an x402
   payment (USDC on Base) with the official ``x402`` package, handing it only the vetted
   requirement, and retry once with ``PAYMENT-SIGNATURE``.
3. 429 and 503 are retried (honouring ``Retry-After``). Unpaid, they are never charged. After a
   payment was sent, only the server's documented uncharged codes (``_UNCHARGED``) are retried, with
   a fresh payment; every other server error or 429, an error answer carrying a settlement receipt,
   ``settlement_unknown`` / ``purchase_unresolved`` / ``credit_delivery_pending`` (paid or not), no
   answer at all (timeout, dropped connection) and a paid 2xx that cannot be read raise
   ``PaymentUnresolvedError`` (or a subclass) with the payer and authorization nonce: never
   retried, never paid again.
4. The settlement receipt (``PAYMENT-RESPONSE``) is returned in ``result.meta.payment``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from decimal import Decimal
from typing import Any, Literal, Mapping, Optional, Sequence, TypeVar, Union, overload

import httpx

from . import _models as m
from . import _routes as r
from ._errors import (
    BudgetExceededError,
    CreditDeliveryPendingError,
    InvalidRequestError,
    NotFoundError,
    PayeeMismatchError,
    PaymentError,
    PaymentRequiredError,
    PaymentUnresolvedError,
    PriceLimitExceededError,
    PurchaseUnresolvedError,
    RateLimitError,
    ServiceUnavailableError,
    SettlementUnknownError,
    TanodError,
)
from ._payment import (
    TANOD_PAYEES,
    USDC_DECIMALS,
    Payees,
    PayeeTable,
    atomic_to_usd,
    build_payment_client,
    decode_receipt,
    first_requirement,
    parse_payment_required,
    payee_allowed,
    payee_table,
    usd_to_atomic,
)
from ._version import __version__

logger = logging.getLogger("tanod")

DEFAULT_BASE_URL = "https://tanod.dev"
DEFAULT_TIMEOUT = 120.0  # scans may take up to ~90 s server-side
DEFAULT_MAX_PRICE_USD = 1.0
# Default total a client instance may sign over its lifetime (USD). It covers ordinary use (about
# 1,000 address checks at USD 0.005, or 20 contract scans at USD 0.25) and stops a runaway loop or
# a swapped endpoint from spending more. Raise it, or pass None, for heavier use.
DEFAULT_MAX_SPEND_USD = 5.0
BASE_MAINNET = "eip155:8453"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # USDC on Base mainnet (6 decimals)
_RETRYABLE = (429, 503)
FREE_TIER_HEADER = "X-Tanod-Free"  # opt in to the server's free tier on an unpaid call
# Error codes that mean "a payment may be (or is) tied up in this request": never retried, never paid again.
_UNRESOLVED: dict[str, type[PaymentUnresolvedError]] = {
    "settlement_unknown": SettlementUnknownError,
    "purchase_unresolved": PurchaseUnresolvedError,
    "credit_delivery_pending": CreditDeliveryPendingError,
}
# What the server's code says about this payment (otherwise unknown, or True when a receipt came back).
_CHARGED: dict[str, bool] = {
    "purchase_unresolved": False,
    "credit_delivery_pending": True,
    "refund_review_unavailable": True,  # 503 after the settlement (server: service._refund_marker_unavailable)
}
# The only 5xx/429 codes that, after a payment was sent, are retried (with a fresh payment) or called
# "not charged": the server answers them before any settlement (product/api/scanapi: app._respond
# "(not charged)" messages; service UNAVAILABLE_REASONS and unavailable(); micro.py "unavailable"
# results, settled only on a worker 200; paymentid._REFUSALS "unavailable"; unsettled.PAYER_*;
# imagegenkit.LIMITED/CAPACITY; limits.py 429/503). Any other code after paying is unresolved.
_UNCHARGED = frozenset({
    "queue_full", "facilitator_error", "payment_identifier_unavailable", "payment_journal_capacity",
    "busy", "rpc_unavailable", "timeout", "rpc_budget", "response_too_large", "micro_unavailable",
    "micro_interrupted", "micro_error", "rate_limited", "host_rate_limited", "origin_cooldown",
    "agentindex_unavailable", "exchange_unavailable", "storage_unavailable", "upload_quota", "uploads_busy",
    "comments_changed", "reviews_changed", "listings_changed", "unsettled_failures_limited",
    "unsettled_failures_in_flight", "image_uncharged_limited", "image_capacity", "daily_cap", "repo_lookup_limited",
})

T = TypeVar("T", bound=m.TanodModel)


# --------------------------------------------------------------------------
# Shared, IO-free helpers
# --------------------------------------------------------------------------


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None


def _error_text(body: Any, resp: httpx.Response) -> str:
    if isinstance(body, dict):
        err = body.get("error", body.get("detail"))
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message") or err.get("detail") or json.dumps(err)[:300]
            return f"{code}: {msg}" if code else str(msg)
        if err:
            return str(err)[:500]
    return (resp.text or resp.reason_phrase or "")[:500]


def _error_field(body: Any, name: str) -> Optional[str]:
    """``error.<name>`` of a JSON error body (``{"error": {"code": ..., "message": ...}}``), else None."""
    err = body.get("error") if isinstance(body, dict) else None
    value = err.get(name) if isinstance(err, dict) else None
    return value if isinstance(value, str) else None


def _retry_after(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _int_header(resp: httpx.Response, name: str) -> Optional[int]:
    value = resp.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class _Quote:
    """A parsed 402 and the decision about it."""

    def __init__(self, resp: httpx.Response, body: Any) -> None:
        self.payment_required = parse_payment_required(resp.headers.get, body)
        req = first_requirement(self.payment_required) if self.payment_required else None
        self.requirement = req  # the one requirement this client would pay (the first Base entry)
        self.amount: Optional[str] = getattr(req, "amount", None)
        self.price_usd: Optional[Decimal] = atomic_to_usd(self.amount)
        self.asset = getattr(req, "asset", None)
        self.network = getattr(req, "network", None)
        self.pay_to = getattr(req, "pay_to", None)
        err = getattr(self.payment_required, "error", None)
        if err is None and isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), str) else None
        self.error: Optional[str] = err
        self.body = body

    @property
    def input_refused(self) -> bool:
        return bool(self.error and "would be refused" in self.error)

    def vetted(self) -> Any:
        """The 402 reduced to the vetted requirement, so the x402 client cannot pick another entry."""
        return self.payment_required.model_copy(update={"accepts": [self.requirement]})

    def error_kwargs(self) -> dict[str, Any]:
        return dict(
            price_usd=self.price_usd,
            amount_atomic=self.amount,
            asset=self.asset,
            network=self.network,
            pay_to=self.pay_to,
            reason=self.error,
            payment_required=self.payment_required,
            body=self.body,
        )


def _check_quote(
    quote: _Quote, path: str, has_wallet: bool, max_price: Optional[float], payees: Optional[PayeeTable] = None
) -> None:
    """Raise unless the client should sign a payment for this quote (the budget is checked later)."""
    if quote.input_refused:
        raise InvalidRequestError(quote.error or "input refused", status_code=402, body=quote.body)
    if quote.payment_required is None or quote.amount is None:
        raise TanodError(f"402 from {path} without usable x402 requirements", status_code=402, body=quote.body)
    # Fail closed: this SDK only ever pays USDC on Base mainnet, and only when the price is known,
    # so a malformed or unexpected quote can never slip past the max_price_usd cap.
    if quote.network != BASE_MAINNET or str(quote.asset or "").lower() != USDC_BASE:
        raise TanodError(
            f"{path} quotes an unsupported payment (network={quote.network}, asset={quote.asset}); "
            "this SDK only pays USDC on Base mainnet. Nothing was signed",
            status_code=402, body=quote.body,
        )
    if quote.price_usd is None:
        raise TanodError(f"{path} quotes an unparseable amount {quote.amount!r}; nothing was signed",
                         status_code=402, body=quote.body)
    # Payee pinning: never pay an address that is not on the allow-list (by default Tanod's own),
    # so a wrong or hijacked base_url cannot redirect payments.
    if payees is not None and not payee_allowed(payees, quote.network, quote.pay_to):
        raise PayeeMismatchError(
            f"{path} asks for payment to {quote.pay_to} on {quote.network}, which is not an allowed payee; "
            "nothing was signed. Check base_url, or pass payees= to allow another deployment's payee",
            **quote.error_kwargs(),
        )
    if not has_wallet:
        raise PaymentRequiredError(
            f"{path} costs USD {quote.price_usd} (free daily tier used up or not available). "
            "Configure a wallet (TANOD_PRIVATE_KEY or signer=...) to pay with USDC on Base.",
            **quote.error_kwargs(),
        )
    if max_price is not None and quote.price_usd is not None and quote.price_usd > Decimal(str(max_price)):
        raise PriceLimitExceededError(
            f"{path} quotes USD {quote.price_usd}, above max_price_usd={max_price}; nothing was signed",
            **quote.error_kwargs(),
        )


class _Sent:
    """The payment that left the process on this attempt (what reconciliation needs)."""

    def __init__(self, payload: Any, headers: Mapping[str, str], quote: _Quote) -> None:
        inner = getattr(payload, "payload", None)
        auth = inner.get("authorization") if isinstance(inner, dict) else None
        auth = auth if isinstance(auth, dict) else {}
        self.payer: Optional[str] = auth.get("from") if isinstance(auth.get("from"), str) else None
        self.nonce: Optional[str] = auth.get("nonce") if isinstance(auth.get("nonce"), str) else None
        self.signature: Optional[str] = next(iter(headers.values()), None)
        self.quote = quote


def _unresolved(
    cls: type[PaymentUnresolvedError],
    path: str,
    what: str,
    sent: Optional[_Sent],
    *,
    code: Optional[str] = None,
    server_message: Optional[str] = None,
    charged: Optional[bool] = None,
    resp: Optional[httpx.Response] = None,
    with_receipt: bool = False,
    charged_by_receipt: bool = False,
    body: Any = None,
) -> PaymentUnresolvedError:
    quote = sent.quote if sent else None
    receipt = decode_receipt(resp.headers.get, quote.price_usd, quote.amount) if resp is not None and quote and with_receipt else None
    if charged_by_receipt:  # charged only when a successful settlement receipt came back; otherwise unknown
        charged = True if receipt is not None and receipt.success else None
    keep = f"; keep payer {sent.payer} and authorization nonce {sent.nonce} for reconciliation" if sent else ""
    return cls(
        f"{path}: {what}. Do not pay again for this request{keep}",
        code=code,
        server_message=server_message,
        charged=charged,
        payer=sent.payer if sent else None,
        nonce=sent.nonce if sent else None,
        payment_signature=sent.signature if sent else None,
        price_usd=quote.price_usd if quote else None,
        amount_atomic=quote.amount if quote else None,
        receipt=receipt,
        settlement=body.get("settlement") if isinstance(body, dict) else None,
        status_code=resp.status_code if resp is not None else None,
        body=body,
    )


def _no_answer(path: str, sent: _Sent, exc: BaseException, resp: Optional[httpx.Response] = None) -> PaymentUnresolvedError:
    """The paid request failed before an answer was read (timeout, dropped connection): it may have settled.

    With ``resp`` the status and headers arrived but the body read failed: the receipt is kept, and
    ``charged`` is ``True`` when it decodes as a successful settlement (otherwise unknown).
    """
    if resp is None:
        return _unresolved(
            PaymentUnresolvedError, path, f"no answer after the payment was sent ({type(exc).__name__}: {exc}); it may have settled", sent
        )
    return _unresolved(
        PaymentUnresolvedError, path,
        f"no answer after the payment was sent (HTTP {resp.status_code}, body read failed: {type(exc).__name__}: {exc}); it may have settled",
        sent, resp=resp, with_receipt=True, charged_by_receipt=True,
    )


def _note_interrupted(path: str, sent: _Sent, exc: BaseException) -> None:
    """A cancellation or interrupt (``asyncio.CancelledError``, a framework timeout) after the payment was sent.

    The exception is re-raised unchanged by the caller; this logs a WARNING and adds an exception note
    (Python 3.11+) with the payer and authorization nonce, never the payment signature.
    """
    what = (f"tanod: {path}: {type(exc).__name__} after the payment was sent; it may have settled. Do not pay again "
            f"for this request; keep payer {sent.payer} and authorization nonce {sent.nonce} for reconciliation")
    logger.warning("%s", what)
    add_note = getattr(exc, "add_note", None)  # BaseException.add_note is Python 3.11+
    if callable(add_note):
        add_note(what)


def _raise_if_unresolved(resp: httpx.Response, body: Any, path: str, sent: Optional[_Sent]) -> None:
    """Raise ``PaymentUnresolvedError`` for an answer that must never be retried or paid again.

    Always: the codes ``settlement_unknown``, ``purchase_unresolved`` and ``credit_delivery_pending``.
    After a payment was sent, also any error answer that carries a settlement receipt (it settled),
    and any 5xx or 429 whose code is not one the server documents as uncharged (``_UNCHARGED``).
    Other 4xx answers are refusals before any settlement and are never retried.
    """
    status = resp.status_code
    if status < 400:
        return
    code = _error_field(body, "code")
    has_receipt = bool(resp.headers.get("PAYMENT-RESPONSE") or resp.headers.get("X-PAYMENT-RESPONSE"))
    cls = _UNRESOLVED.get(code or "")
    if cls is None:
        if sent is None or not (has_receipt or ((status >= 500 or status == 429) and code not in _UNCHARGED)):
            return
        cls = PaymentUnresolvedError
    server_message = _error_field(body, "message") or _error_field(body, "detail")
    if cls is not PaymentUnresolvedError:
        what = f"{code}: {server_message or _error_text(body, resp)}"
    elif has_receipt:
        what = f"the payment settled, but no result came back (HTTP {status}: {_error_text(body, resp)})"
    elif code is not None:
        what = f"HTTP {status} {code} after the payment was sent, not a code the server documents as uncharged; it may have settled"
    else:
        what = f"no readable answer (HTTP {status}) after the payment was sent; it may have settled"
    charged = True if has_receipt else _CHARGED.get(code or "")
    raise _unresolved(cls, path, what, sent, code=code, server_message=server_message, charged=charged,
                      resp=resp, with_receipt=has_receipt, body=body)


def _parse_paid(route: r.Route, resp: httpx.Response, body: Any, quote: Optional[_Quote], sent: Optional[_Sent]) -> Any:
    """``_parse``; a paid 2xx that cannot be read is a settled payment without a result."""
    try:
        return _parse(route, resp, body, quote)
    except Exception as exc:
        if sent is None:
            raise
        raise _unresolved(
            PaymentUnresolvedError, route.path,
            f"the payment settled, but the answer could not be read ({type(exc).__name__}: {str(exc)[:200]})",
            sent, resp=resp, with_receipt=True, charged_by_receipt=True, body=body if body is not None else resp.text[:500],
        ) from exc


def _raise_for_status(resp: httpx.Response, body: Any, path: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    text = _error_text(body, resp)
    if status in (413, 415, 422):
        raise InvalidRequestError(f"{path}: {text}", status_code=status, body=body)
    if status == 404:
        raise NotFoundError(f"{path}: {text}", status_code=status, body=body)
    if status == 429:
        raise RateLimitError(f"{path}: rate limited: {text}", retry_after=_retry_after(resp), status_code=status, body=body)
    if status in (502, 503, 504):
        raise ServiceUnavailableError(
            f"{path}: temporarily unavailable ({status}, not charged): {text}",
            retry_after=_retry_after(resp),
            status_code=status,
            body=body,
        )
    raise TanodError(f"{path}: HTTP {status}: {text}", status_code=status, body=body)


def _parse(route: r.Route, resp: httpx.Response, body: Any, quote: Optional[_Quote]) -> Any:
    receipt = decode_receipt(resp.headers.get, quote.price_usd if quote else None, quote.amount if quote else None)
    meta = m.ResponseMeta(
        status_code=resp.status_code,
        url=str(resp.request.url) if resp.request is not None else "",
        product=resp.headers.get("X-Tanod-Product"),
        free_remaining_today=_int_header(resp, "X-Free-Remaining-Today"),
        scan_id=resp.headers.get("X-Scan-Id"),
        cache=resp.headers.get("X-Cache"),
        report_markdown_url=resp.headers.get("X-Report-Markdown"),
        report_json_url=resp.headers.get("X-Report-Json"),
        payment=receipt,
    )
    if route.kind == "csv":
        model: m.TanodModel = m.CsvExport(csv=resp.text)
    elif route.kind == "markdown":
        model = m.TextReport(markdown=resp.text)
    else:
        if not isinstance(body, dict):
            raise TanodError(f"{route.path}: expected a JSON object", status_code=resp.status_code, body=resp.text[:500])
        model = route.model.model_validate(body)
    model._meta = meta
    return model


class _Base:
    def __init__(
        self,
        *,
        base_url: str,
        max_price_usd: Optional[float],
        max_retries: int,
        max_retry_wait: float,
        use_free_tier: bool = True,
        payees: Optional[Payees] = TANOD_PAYEES,
        max_spend_usd: Optional[float] = DEFAULT_MAX_SPEND_USD,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.use_free_tier = bool(use_free_tier)
        self.max_price_usd = max_price_usd
        self.max_retries = max(0, int(max_retries))
        self.max_retry_wait = max_retry_wait
        self._payer: Any = None
        self._payees = payee_table(payees)
        self.max_spend_usd = max_spend_usd
        self._budget_atomic = None if max_spend_usd is None else usd_to_atomic(max_spend_usd, "max_spend_usd")
        self._spent_atomic = 0
        self._spend_lock = threading.Lock()

    @property
    def has_wallet(self) -> bool:
        """True when a payment signer is configured."""
        return self._payer is not None

    @property
    def spent_usd(self) -> Decimal:
        """USD committed by this client so far: the sum of the payments it has signed.

        A payment counts as spent once signed, even if the request then failed (it may still
        settle), so this is an upper bound on what was charged.
        """
        return Decimal(self._spent_atomic) / (Decimal(10) ** USDC_DECIMALS)

    def _reserve(self, quote: _Quote, path: str) -> int:
        """Reserve the quoted amount against ``max_spend_usd`` before signing; return the atomic amount."""
        amount = int(quote.amount)  # type: ignore[arg-type]  # digits, checked by _check_quote
        with self._spend_lock:
            if self._budget_atomic is not None and self._spent_atomic + amount > self._budget_atomic:
                raise BudgetExceededError(
                    f"{path} quotes USD {quote.price_usd}, but this client has already signed USD {self.spent_usd} "
                    f"of its max_spend_usd={self.max_spend_usd}; nothing was signed",
                    spent_usd=self.spent_usd,
                    max_spend_usd=Decimal(str(self.max_spend_usd)),
                    **quote.error_kwargs(),
                )
            self._spent_atomic += amount
        return amount

    def _release(self, amount: int) -> None:
        with self._spend_lock:
            self._spent_atomic -= amount

    def _check_signed(self, payload: Any, quote: _Quote, path: str) -> None:
        """The signed payload must be for exactly the vetted requirement (guards custom payment clients)."""
        accepted = getattr(payload, "accepted", None)
        network, pay_to = getattr(accepted, "network", None), getattr(accepted, "pay_to", None)
        if self._payees is not None and not payee_allowed(self._payees, network, pay_to):
            raise PayeeMismatchError(
                f"{path}: the payment client signed a payment to {pay_to} on {network}, "
                "which is not an allowed payee; nothing was sent",
                **{**quote.error_kwargs(), "pay_to": pay_to, "network": network},
            )
        req = quote.requirement
        signed = (network, pay_to, getattr(accepted, "asset", None), getattr(accepted, "amount", None))
        if accepted is None or signed != (req.network, req.pay_to, req.asset, req.amount):
            raise PaymentError(f"{path}: the signed payment does not match the vetted quote; nothing was sent")

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": f"tanod-python/{__version__}", "Accept": "application/json, text/plain, */*"}

    def _unpaid_headers(self) -> Optional[dict[str, str]]:
        """Extra headers of the unpaid attempt: the free-tier opt-in (never sent on the paid retry)."""
        return {FREE_TIER_HEADER: "1"} if self.use_free_tier else None

    def _wait_for(self, resp: httpx.Response, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None to stop retrying."""
        if resp.status_code not in _RETRYABLE or attempt >= self.max_retries:
            return None
        wait = _retry_after(resp)
        if wait is None:
            wait = min(2.0**attempt, 8.0)
        if wait > self.max_retry_wait:
            return None
        return wait

    def __repr__(self) -> str:  # never show key material
        return f"{type(self).__name__}(base_url={self.base_url!r}, wallet={'yes' if self.has_wallet else 'no'})"


# --------------------------------------------------------------------------
# Sync client
# --------------------------------------------------------------------------


class Tanod(_Base):
    """Synchronous Tanod client.

    Args:
        private_key: hex private key of the paying wallet (USDC on Base). Defaults to
            the ``TANOD_PRIVATE_KEY`` environment variable. Never logged or stored as text.
        signer: an ``eth_account`` account or an x402 ``ClientEvmSigner`` (instead of a key).
        payment_client: a fully configured ``x402.x402ClientSync`` (advanced; overrides both).
            Payee pinning and the spend budget still apply: the client vets the quote first, hands
            the payment client only the vetted requirement, and refuses to send a payload that
            names anything else.
        max_price_usd: refuse to sign any single payment above this (default USD 1.00;
            ``agents_bulk`` costs USD 2.00). ``None`` disables the cap.
        payees: only ever pay these payees (default ``TANOD_PAYEES``, Tanod's own): a mapping
            of CAIP-2 network to address(es), or a flat list for every network. A quote naming
            any other ``payTo`` raises ``PayeeMismatchError`` and nothing is signed. Set it when
            ``base_url`` points at another deployment; ``None`` disables pinning.
        max_spend_usd: total USD this client instance may sign, summed over all calls (default
            USD 5.00). A payment that would go past it raises ``BudgetExceededError`` and nothing
            is signed. ``None`` disables the total cap. ``spent_usd`` shows the running total.
        use_env: read ``TANOD_PRIVATE_KEY`` when no key/signer is given (default True).
        max_retries: retries on 429/503 (after paying, only uncharged codes; ``PaymentUnresolvedError`` never). Default 2.
        max_retry_wait: give up instead of sleeping longer than this many seconds.
        use_free_tier: send ``X-Tanod-Free: 1`` on the unpaid first attempt so the free daily
            tier is used when it covers the call (default True). False: every paid call is paid
            (the unpaid attempt only fetches the 402 quote).
        timeout: HTTP timeout in seconds (scans can take ~90 s).
        http_client: your own ``httpx.Client`` (for proxies, testing).

    Without a wallet the client still works for free-tier calls and the free
    ``agents_summary``; past the free tier it raises ``PaymentRequiredError`` with the price.
    """

    def __init__(
        self,
        *,
        private_key: Optional[str] = None,
        signer: Any = None,
        payment_client: Any = None,
        max_price_usd: Optional[float] = DEFAULT_MAX_PRICE_USD,
        payees: Optional[Payees] = TANOD_PAYEES,
        max_spend_usd: Optional[float] = DEFAULT_MAX_SPEND_USD,
        use_env: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 2,
        max_retry_wait: float = 30.0,
        use_free_tier: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            base_url=base_url, max_price_usd=max_price_usd, max_retries=max_retries, max_retry_wait=max_retry_wait,
            use_free_tier=use_free_tier, payees=payees, max_spend_usd=max_spend_usd,
        )
        self._payer = build_payment_client(
            private_key=private_key,
            signer=signer,
            payment_client=payment_client,
            max_price_usd=max_price_usd,
            use_env=use_env,
            is_async=False,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "Tanod":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _build(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Request:
        headers = self._headers()
        if extra:
            headers.update(extra)
        return self._http.build_request(route.method, self.base_url + route.path, json=route.json, headers=headers)

    def _request(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Response:
        return self._http.send(self._build(route, extra))

    def _call(self, route: r.Route) -> Any:
        attempt = 0
        while True:
            resp = self._request(route, self._unpaid_headers())
            quote: Optional[_Quote] = None
            sent: Optional[_Sent] = None
            if resp.status_code == 402:
                quote = _Quote(resp, _json_or_none(resp))
                _check_quote(quote, route.path, self.has_wallet, self.max_price_usd, self._payees)
                reserved = self._reserve(quote, route.path)
                try:
                    payload = self._payer.create_payment_payload(quote.vetted())
                    self._check_signed(payload, quote, route.path)
                    from x402.http.x402_http_client_base import x402HTTPClientBase

                    pay_headers = x402HTTPClientBase().encode_payment_signature_header(payload)
                except Exception as exc:
                    # Nothing left the process, so the reservation is released. After the send below
                    # it never is: the payment may settle even if the request then fails.
                    self._release(reserved)
                    if isinstance(exc, TanodError):
                        raise
                    raise PaymentError(f"could not build the x402 payment: {type(exc).__name__}: {exc}") from exc
                logger.info("tanod: paying USD %s for %s %s", quote.price_usd, route.method, route.path)
                sent = _Sent(payload, pay_headers, quote)
                paid: Optional[httpx.Response] = None
                try:
                    paid = self._http.send(self._build(route, pay_headers), stream=True)
                    try:
                        paid.read()  # apart from the send, so a failed body read keeps the status and receipt
                    finally:
                        paid.close()
                except Exception as exc:  # sent, no answer read: never retried, never paid again
                    raise _no_answer(route.path, sent, exc, paid) from exc
                except BaseException as exc:  # interrupted after sending: re-raised unchanged, with payer and nonce
                    _note_interrupted(route.path, sent, exc)
                    raise
                resp = paid
                if resp.status_code == 402:
                    again = _Quote(resp, _json_or_none(resp))
                    raise PaymentRequiredError(
                        f"{route.path}: payment refused: {again.error or 'no reason given'}",
                        **again.error_kwargs(),
                    )
            body = None if route.kind != "json" and resp.status_code < 400 else _json_or_none(resp)
            _raise_if_unresolved(resp, body, route.path, sent)  # before any retry: never a second payment
            wait = self._wait_for(resp, attempt)
            if wait is not None:
                logger.info("tanod: %s %s -> %s, retrying in %.1fs", route.method, route.path, resp.status_code, wait)
                time.sleep(wait)
                attempt += 1
                continue
            _raise_for_status(resp, body, route.path)
            return _parse_paid(route, resp, body, quote, sent)

    # -- pactlint ----------------------------------------------------------

    def scan_contract_source(
        self,
        source: Optional[str] = None,
        *,
        standard_json: Optional[dict[str, Any]] = None,
        filename: Optional[str] = None,
        compiler_version: Optional[str] = None,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        """pactlint: static analysis of Solidity source (one file, or solc standard JSON).

        USD 0.25 up to 3,000 nSLOC, USD 0.75 up to 15,000. Free tier: 3 scans/IP/day.
        """
        return self._call(
            r.scan_contract_source(
                source, standard_json, filename, compiler_version, include_informational, include_noisy, include_dependencies
            )
        )

    def scan_contract_address(
        self,
        address: str,
        chain: m.Chain = "ethereum",
        *,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        """pactlint: scan a verified contract (Sourcify) on Ethereum or Base. Unverified: 404, free."""
        return self._call(
            r.scan_contract_address(address, chain, include_informational, include_noisy, include_dependencies)
        )

    def get_contract_source(self, chain: m.Chain, address: str) -> m.ContractSource:
        """Verified source files, ABI and compiler settings. USD 0.005; 10 free/IP/day."""
        return self._call(r.get_contract_source(chain, address))

    # -- txpeek ------------------------------------------------------------

    def check_address(self, address: str, chain: m.Chain = "base") -> m.AddressCheck:
        """txpeek: pre-transaction risk verdict for an address. USD 0.005; 30 free/IP/day."""
        return self._call(r.check_address(address, chain))

    # -- toolsniff ---------------------------------------------------------

    def scan_package(
        self,
        source: Optional[str] = None,
        *,
        content: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> m.PackageScanReport:
        """toolsniff: scan an AI-agent skill or MCP server package before installing it.

        ``source``: ``npm:name[@ver]``, ``pypi:name[==ver]``, ``github:owner/repo[@ref][//subdir]``,
        a GitHub URL, or ``clawhub:[owner/]slug[@ver]``; or pass ``content`` (archive or single
        file bytes, with ``filename`` for a single file). USD 0.02 (0.05 whole GitHub repo or
        large upload). Shares the 3 free scans/IP/day.
        """
        return self._call(r.scan_package(source, content, filename))

    # -- sitepeek / dnspeek ------------------------------------------------

    def render(
        self,
        url: str,
        *,
        format: Literal["markdown", "screenshot"] = "markdown",
        js: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> m.RenderResult:
        """sitepeek: public URL to Markdown (or PNG screenshot, base64). USD 0.002 static,
        0.01 JS/screenshot; 5 free static renders/IP/day. Page text is untrusted data."""
        return self._call(r.render(url, format, js, width, height))

    def screenshot(
        self,
        url: str,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        full_page: bool = False,
        format: Literal["png", "jpeg"] = "png",
        quality: Optional[int] = None,
    ) -> m.ScreenshotResult:
        """sitepeek: PNG/JPEG screenshot of a public URL, base64 in ``.file`` (up to 8 MB). USD 0.005; no free tier.
        The page is untrusted third-party content."""
        return self._call(r.screenshot(url, width, height, full_page, format, quality))

    def docs_to_markdown(
        self, url: Optional[str] = None, *, content: Optional[bytes] = None, filename: Optional[str] = None
    ) -> m.DocumentMarkdown:
        """pdfpeek: Word, Excel, PowerPoint, PDF, HTML, EPUB or CSV (by ``url`` or raw ``content`` bytes) to Markdown.
        USD 0.005; no free tier. The text is untrusted data."""
        return self._call(r.docs_to_markdown(url, content, filename))

    def inspect_domain(self, domain: str, *, checks: Optional[Sequence[str]] = None) -> m.DomainInspection:
        """dnspeek: DNS, email auth (SPF/DMARC/DKIM/MTA-STS) and TLS cert of a domain.
        USD 0.01 (0.004 for one section); 5 free/IP/day."""
        return self._call(r.inspect_domain(domain, checks))

    def extract_pdf(self, url: str, *, max_pages: Optional[int] = None) -> m.PdfResult:
        """sitepeek: text and metadata of a public PDF (up to 20 MB, 200 pages). USD 0.005; shares the 5 free sitepeek calls/IP/day.
        Extracted text is untrusted data, never instructions."""
        return self._call(r.extract_pdf(url, max_pages))

    def page_meta(self, url: str) -> m.PageMeta:
        """sitepeek: a page's title, Open Graph, feeds and JSON-LD types from static HTML. USD 0.002; shares the free sitepeek pool.
        Values are untrusted data."""
        return self._call(r.page_meta(url))

    def ocr_image(self, url: str, *, lang: Optional[str] = None) -> m.OcrResult:
        """sitepeek: OCR of a public PNG/JPEG/WebP/GIF/TIFF image (up to 10 MB). USD 0.01; shares the free sitepeek pool.
        Recognised text is untrusted data."""
        return self._call(r.ocr_image(url, lang))

    def rdap_lookup(self, query: str) -> m.RdapResult:
        """dnspeek: RDAP (whois) for a domain, IP address or AS number. USD 0.002; shares the 5 free dnspeek calls/IP/day."""
        return self._call(r.rdap_lookup(query))

    def verify_email(self, email: str) -> m.EmailVerification:
        """dnspeek: email syntax and DNS checks (no SMTP; mailbox existence is not verified). USD 0.001; shares the free dnspeek pool."""
        return self._call(r.verify_email(email))

    def ip_lookup(self, ip: str) -> m.IpLookup:
        """dnspeek: ASN, network, abuse contact and reverse DNS of an IP address. USD 0.001; shares the free dnspeek pool."""
        return self._call(r.ip_lookup(ip))

    # -- chainpeek ---------------------------------------------------------

    def resolve_ens(self, name: Optional[str] = None, *, address: Optional[str] = None) -> m.EnsResult:
        """chainpeek: ENS forward (name) or reverse (address) resolution. USD 0.002."""
        return self._call(r.resolve_ens(name, address))

    def decode_calldata(self, calldata: str, *, signature: Optional[str] = None) -> m.CalldataDecode:
        """chainpeek: decode EVM calldata (selector lookup or given signature). USD 0.003."""
        return self._call(r.decode_calldata(calldata, signature))

    def token_info(self, chain: m.Chain, address: str) -> m.TokenInfo:
        """chainpeek: ERC-20 metadata. USD 0.003."""
        return self._call(r.token_info(chain, address))

    def balance(self, chain: m.Chain, address: str, *, token: Optional[str] = None) -> m.Balance:
        """chainpeek: native or ERC-20 balance. USD 0.002."""
        return self._call(r.balance(chain, address, token))

    def gas_price(self, chain: m.Chain) -> m.GasPrice:
        """chainpeek: current gas price and base fee. USD 0.001."""
        return self._call(r.gas_price(chain))

    def latest_block(self, chain: m.Chain) -> m.BlockHeader:
        """chainpeek: latest block header. USD 0.001. (chainpeek: 10 free reads/IP/day, shared.)"""
        return self._call(r.latest_block(chain))

    def token_price(self, chain: m.Chain, pair: str) -> m.TokenPrice:
        """chainpeek: Chainlink on-chain price with a staleness flag (not a DEX spot price). USD 0.002."""
        return self._call(r.token_price(chain, pair))

    def transaction(self, chain: m.Chain, hash: str) -> m.Transaction:
        """chainpeek: transaction and receipt summary with the fee split. USD 0.002."""
        return self._call(r.transaction(chain, hash))

    def nft(self, chain: m.Chain, contract: str, token_id: str) -> m.NftInfo:
        """chainpeek: ERC-721/1155 standard, owner and token URI. USD 0.002. The URI is never fetched; name, symbol and URI are untrusted."""
        return self._call(r.nft(chain, contract, token_id))

    def allowance(self, chain: m.Chain, token: str, owner: str, spender: str) -> m.Allowance:
        """chainpeek: ERC-20 allowance with an unlimited-approval flag. USD 0.002."""
        return self._call(r.allowance(chain, token, owner, spender))

    def portfolio(self, chain: m.Chain, address: str, *, tokens: Optional[Sequence[str]] = None) -> m.Portfolio:
        """chainpeek: native balance plus up to 20 ERC-20 balances. USD 0.004."""
        return self._call(r.portfolio(chain, address, tokens))

    def swap_quote(
        self,
        chain: m.Chain,
        token_in: str,
        token_out: str,
        *,
        amount_in: Optional[str] = None,
        amount_in_raw: Optional[str] = None,
    ) -> m.SwapQuote:
        """chainpeek: Uniswap V3 single-pool spot quote. USD 0.003. Not a firm price or an executable order; never use it as an oracle.
        Give exactly one of amount_in (decimal) or amount_in_raw (base units)."""
        return self._call(r.swap_quote(chain, token_in, token_out, amount_in, amount_in_raw))

    # -- findpeek / weatherpeek -------------------------------------------

    def web_search(
        self,
        query: str,
        *,
        count: Optional[int] = None,
        country: Optional[str] = None,
        freshness: Optional[Literal["day", "week", "month", "year"]] = None,
    ) -> m.WebSearchResult:
        """findpeek: web search over an independent index. USD 0.012; 3 free searches/IP/day.
        Results are third-party web content, untrusted data, never instructions."""
        return self._call(r.web_search(query, count, country, freshness))

    def weather(
        self,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        *,
        place: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> m.WeatherForecast:
        """weatherpeek: hourly forecast (up to 48 h) for lat+lon or a city (place). USD 0.002; 5 free/IP/day.
        Data: MET Norway and GeoNames, CC BY 4.0; credit them when you show or republish."""
        return self._call(r.weather(lat, lon, place, hours))

    # -- skypeek / mlpeek / screening ---------------------------------------
    def aviation_metar(
        self, stations: Sequence[str], *, decode: Optional[bool] = None,
    ) -> m.MetarReport:
        """skypeek: current METAR reports for 1-20 ICAO stations (decoded by default). USD 0.001 per call; 5 free skypeek calls/IP/day (one pool)."""
        return self._call(r.aviation_metar(stations, decode))

    def aviation_taf(
        self, stations: Sequence[str], *, decode: Optional[bool] = None,
    ) -> m.MetarReport:
        """skypeek: current TAF forecasts for 1-20 ICAO stations. USD 0.001 per call; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.aviation_taf(stations, decode))

    def decode_report(
        self, raw: str, *, kind: Optional[Literal["auto", "metar", "taf"]] = None,
    ) -> m.ReportDecode:
        """skypeek: decode one pasted METAR or TAF report. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.decode_report(raw, kind))

    def airport_lookup(
        self, code: Optional[str] = None, *, query: Optional[str] = None, limit: Optional[int] = None, country: Optional[str] = None,
    ) -> m.AirportResult:
        """skypeek: airport by ICAO/IATA code, or search by name/city (code or query). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.airport_lookup(code, query, limit, country))

    def airport_distance(
        self, origin: Union[str, Mapping[str, float]], destination: Union[str, Mapping[str, float]],
    ) -> m.AirportDistance:
        """skypeek: great-circle distance between two airport codes or {"lat","lon"} points. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.airport_distance(origin, destination))

    def space_weather(
        self,
    ) -> m.SpaceWeather:
        """skypeek: Kp index, Kp forecast, solar wind and NOAA alerts. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.space_weather())

    def aurora(
        self, lat: float, lon: float,
    ) -> m.AuroraNowcast:
        """skypeek: aurora nowcast probability at a location. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.aurora(lat, lon))

    def asteroids(
        self, *, days: Optional[int] = None, dist_max_au: Optional[float] = None,
    ) -> m.AsteroidApproaches:
        """skypeek: asteroid and comet close approaches (1-30 days ahead). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.asteroids(days, dist_max_au))

    def sun_moon(
        self, lat: float, lon: float, *, date: Optional[str] = None, tz: Optional[str] = None,
    ) -> m.SunMoon:
        """skypeek: sun and moon times for a place and date (YYYY-MM-DD, IANA tz). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.sun_moon(lat, lon, date, tz))

    def satellite_passes(
        self, lat: float, lon: float, *, alt_m: Optional[float] = None, norad_id: Optional[int] = None, days: Optional[int] = None, min_elevation: Optional[float] = None, visible_only: Optional[bool] = None,
    ) -> m.SatellitePasses:
        """skypeek: pass predictions for the ISS (default) or another supported satellite. USD 0.002; shares the 5 free skypeek calls/IP/day."""
        return self._call(r.satellite_passes(lat, lon, alt_m, norad_id, days, min_elevation, visible_only))

    def embed(
        self, texts: Sequence[str], *, model: Optional[Literal["small-en", "multilingual"]] = None, normalize: Optional[bool] = None, encoding: Optional[Literal["float", "base64"]] = None, input_type: Optional[Literal["none", "query", "passage"]] = None,
    ) -> m.EmbedResult:
        """mlpeek: 384-dimension embeddings of 1-64 texts. USD 0.0005 per text, at least USD 0.001 per call; 5 free mlpeek calls/IP/day (one pool)."""
        return self._call(r.embed(texts, model, normalize, encoding, input_type))

    def rerank(
        self, query: str, documents: Sequence[str], *, top_k: Optional[int] = None,
    ) -> m.RerankResult:
        """mlpeek: cross-encoder rerank of up to 100 documents against a query. USD 0.002 per call; shares the 5 free mlpeek calls/IP/day."""
        return self._call(r.rerank(query, documents, top_k))

    def similarity(
        self, a: Optional[str] = None, b: Optional[str] = None, *, pairs: Optional[Sequence[Any]] = None, model: Optional[Literal["small-en", "multilingual"]] = None,
    ) -> m.SimilarityResult:
        """mlpeek: cosine similarity of one pair (a, b) or 1-50 pairs. USD 0.0005 per pair, at least USD 0.001 per call; shares the 5 free mlpeek calls/IP/day."""
        return self._call(r.similarity(a, b, pairs, model))

    def ner(
        self, text: str, *, labels: Optional[Sequence[str]] = None,
    ) -> m.NerResult:
        """mlpeek: named entities in English text (18 OntoNotes types). USD 0.001; shares the 5 free mlpeek calls/IP/day."""
        return self._call(r.ner(text, labels))

    def classify_zero_shot(
        self, text: str, labels: Sequence[str], *, multi_label: Optional[bool] = None, hypothesis_template: Optional[str] = None,
    ) -> m.ZeroShotResult:
        """mlpeek: zero-shot classification into 1-10 caller labels. USD 0.001; shares the 5 free mlpeek calls/IP/day."""
        return self._call(r.classify_zero_shot(text, labels, multi_label, hypothesis_template))

    def check_url(
        self, url: Optional[str] = None, *, domain: Optional[str] = None,
    ) -> m.UrlCheck:
        """Screening: is a URL or domain on two public phishing/scam lists? Only parsed, never fetched; not listed is not cleared. USD 0.001; shares the 10 free chain reads/IP/day."""
        return self._call(r.check_url(url, domain))

    def check_urls(
        self, items: Sequence[str],
    ) -> m.UrlCheckBatch:
        """Screening: the URL check for 1-1,000 URLs or domains. USD 0.0002 per item, at least USD 0.001 per call; no free tier."""
        return self._call(r.check_urls(items))

    def sanctions_batch(
        self, addresses: Sequence[str],
    ) -> m.SanctionsBatch:
        """Screening: US OFAC SDN digital-currency-address check for 1-1,000 addresses. USD 0.0005 per address, at least USD 0.002 per call; no free tier. Not legal advice."""
        return self._call(r.sanctions_batch(addresses))

    # -- agentscan ---------------------------------------------------------

    def agents_summary(self) -> m.AgentsSummary:
        """agentscan: free aggregate summary of the x402/MCP index (no payment, no quota)."""
        return self._call(r.agents_summary())

    def agents_query(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        min_price_usd: Optional[float] = None,
        max_price_usd: Optional[float] = None,
        q: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> m.AgentsQueryResult:
        """agentscan: filtered, paginated index records. USD 0.02; 10 free/IP/day."""
        return self._call(
            r.agents_query(source, category, network, min_price_usd, max_price_usd, q, page, page_size)
        )

    def agents_history(
        self, *, source: Optional[str] = None, id: Optional[str] = None, url: Optional[str] = None
    ) -> m.AgentsHistoryResult:
        """agentscan: presence and price history of one record. USD 0.05; 5 free/IP/day."""
        return self._call(r.agents_history(source, id, url))

    @overload
    def agents_export(
        self,
        *,
        source: Optional[str] = ...,
        category: Optional[str] = ...,
        network: Optional[str] = ...,
        format: Literal["json"] = ...,
        limit: Optional[int] = ...,
    ) -> m.AgentsExport: ...

    @overload
    def agents_export(
        self,
        *,
        source: Optional[str] = ...,
        category: Optional[str] = ...,
        network: Optional[str] = ...,
        format: Literal["csv"],
        limit: Optional[int] = ...,
    ) -> m.CsvExport: ...

    def agents_export(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        format: Literal["json", "csv"] = "json",
        limit: Optional[int] = None,
    ) -> Any:
        """agentscan: filtered slice of the index (<= 5,000 rows), JSON or CSV. USD 0.25; no free tier."""
        return self._call(r.agents_export(source, category, network, format, limit))

    def agents_bulk(self, *, source: Optional[str] = None) -> m.AgentsBulk:
        """agentscan: full current snapshot. USD 2.00 (raise max_price_usd); no free tier."""
        return self._call(r.agents_bulk(source))

    # -- shared ------------------------------------------------------------

    def get_report(self, scan_id: str, *, format: Literal["json", "markdown"] = "json") -> Any:
        """A stored pactlint/toolsniff report (kept 30 days; free to re-read)."""
        return self._call(r.get_report(scan_id, format))

    def health(self) -> m.Health:
        """Liveness and configuration summary (free)."""
        return self._call(r.health())


# --------------------------------------------------------------------------
# Async client
# --------------------------------------------------------------------------


class AsyncTanod(_Base):
    """Asynchronous Tanod client; same arguments and methods as :class:`Tanod`.

    ``payment_client`` must be an async ``x402.x402Client`` when given.
    """

    def __init__(
        self,
        *,
        private_key: Optional[str] = None,
        signer: Any = None,
        payment_client: Any = None,
        max_price_usd: Optional[float] = DEFAULT_MAX_PRICE_USD,
        payees: Optional[Payees] = TANOD_PAYEES,
        max_spend_usd: Optional[float] = DEFAULT_MAX_SPEND_USD,
        use_env: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 2,
        max_retry_wait: float = 30.0,
        use_free_tier: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            base_url=base_url, max_price_usd=max_price_usd, max_retries=max_retries, max_retry_wait=max_retry_wait,
            use_free_tier=use_free_tier, payees=payees, max_spend_usd=max_spend_usd,
        )
        self._payer = build_payment_client(
            private_key=private_key,
            signer=signer,
            payment_client=payment_client,
            max_price_usd=max_price_usd,
            use_env=use_env,
            is_async=True,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncTanod":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _build(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Request:
        headers = self._headers()
        if extra:
            headers.update(extra)
        return self._http.build_request(route.method, self.base_url + route.path, json=route.json, headers=headers)

    async def _request(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Response:
        return await self._http.send(self._build(route, extra))

    async def _call(self, route: r.Route) -> Any:
        attempt = 0
        while True:
            resp = await self._request(route, self._unpaid_headers())
            quote: Optional[_Quote] = None
            sent: Optional[_Sent] = None
            if resp.status_code == 402:
                quote = _Quote(resp, _json_or_none(resp))
                _check_quote(quote, route.path, self.has_wallet, self.max_price_usd, self._payees)
                reserved = self._reserve(quote, route.path)
                try:
                    payload = self._payer.create_payment_payload(quote.vetted())
                    if asyncio.iscoroutine(payload):
                        payload = await payload
                    self._check_signed(payload, quote, route.path)
                    from x402.http.x402_http_client_base import x402HTTPClientBase

                    pay_headers = x402HTTPClientBase().encode_payment_signature_header(payload)
                except Exception as exc:
                    # Nothing left the process, so the reservation is released (see the sync client).
                    self._release(reserved)
                    if isinstance(exc, TanodError):
                        raise
                    raise PaymentError(f"could not build the x402 payment: {type(exc).__name__}: {exc}") from exc
                logger.info("tanod: paying USD %s for %s %s", quote.price_usd, route.method, route.path)
                sent = _Sent(payload, pay_headers, quote)
                paid: Optional[httpx.Response] = None
                try:
                    paid = await self._http.send(self._build(route, pay_headers), stream=True)
                    try:
                        await paid.aread()  # see the sync client
                    finally:
                        await paid.aclose()
                except Exception as exc:  # sent, no answer read: never retried, never paid again
                    raise _no_answer(route.path, sent, exc, paid) from exc
                except BaseException as exc:  # asyncio.CancelledError (wait_for, framework timeout): re-raised unchanged
                    _note_interrupted(route.path, sent, exc)
                    raise
                resp = paid
                if resp.status_code == 402:
                    again = _Quote(resp, _json_or_none(resp))
                    raise PaymentRequiredError(
                        f"{route.path}: payment refused: {again.error or 'no reason given'}",
                        **again.error_kwargs(),
                    )
            body = None if route.kind != "json" and resp.status_code < 400 else _json_or_none(resp)
            _raise_if_unresolved(resp, body, route.path, sent)  # before any retry: never a second payment
            wait = self._wait_for(resp, attempt)
            if wait is not None:
                logger.info("tanod: %s %s -> %s, retrying in %.1fs", route.method, route.path, resp.status_code, wait)
                await asyncio.sleep(wait)
                attempt += 1
                continue
            _raise_for_status(resp, body, route.path)
            return _parse_paid(route, resp, body, quote, sent)

    async def scan_contract_source(
        self,
        source: Optional[str] = None,
        *,
        standard_json: Optional[dict[str, Any]] = None,
        filename: Optional[str] = None,
        compiler_version: Optional[str] = None,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        return await self._call(
            r.scan_contract_source(
                source, standard_json, filename, compiler_version, include_informational, include_noisy, include_dependencies
            )
        )

    async def scan_contract_address(
        self,
        address: str,
        chain: m.Chain = "ethereum",
        *,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        return await self._call(
            r.scan_contract_address(address, chain, include_informational, include_noisy, include_dependencies)
        )

    async def get_contract_source(self, chain: m.Chain, address: str) -> m.ContractSource:
        return await self._call(r.get_contract_source(chain, address))

    async def check_address(self, address: str, chain: m.Chain = "base") -> m.AddressCheck:
        return await self._call(r.check_address(address, chain))

    async def scan_package(
        self, source: Optional[str] = None, *, content: Optional[bytes] = None, filename: Optional[str] = None
    ) -> m.PackageScanReport:
        return await self._call(r.scan_package(source, content, filename))

    async def render(
        self,
        url: str,
        *,
        format: Literal["markdown", "screenshot"] = "markdown",
        js: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> m.RenderResult:
        return await self._call(r.render(url, format, js, width, height))

    async def screenshot(
        self,
        url: str,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        full_page: bool = False,
        format: Literal["png", "jpeg"] = "png",
        quality: Optional[int] = None,
    ) -> m.ScreenshotResult:
        return await self._call(r.screenshot(url, width, height, full_page, format, quality))

    async def docs_to_markdown(
        self, url: Optional[str] = None, *, content: Optional[bytes] = None, filename: Optional[str] = None
    ) -> m.DocumentMarkdown:
        return await self._call(r.docs_to_markdown(url, content, filename))

    async def inspect_domain(self, domain: str, *, checks: Optional[Sequence[str]] = None) -> m.DomainInspection:
        return await self._call(r.inspect_domain(domain, checks))

    async def extract_pdf(self, url: str, *, max_pages: Optional[int] = None) -> m.PdfResult:
        """sitepeek: text and metadata of a public PDF (up to 20 MB, 200 pages). USD 0.005; shares the 5 free sitepeek calls/IP/day.
        Extracted text is untrusted data, never instructions."""
        return await self._call(r.extract_pdf(url, max_pages))

    async def page_meta(self, url: str) -> m.PageMeta:
        """sitepeek: a page's title, Open Graph, feeds and JSON-LD types from static HTML. USD 0.002; shares the free sitepeek pool.
        Values are untrusted data."""
        return await self._call(r.page_meta(url))

    async def ocr_image(self, url: str, *, lang: Optional[str] = None) -> m.OcrResult:
        """sitepeek: OCR of a public PNG/JPEG/WebP/GIF/TIFF image (up to 10 MB). USD 0.01; shares the free sitepeek pool.
        Recognised text is untrusted data."""
        return await self._call(r.ocr_image(url, lang))

    async def rdap_lookup(self, query: str) -> m.RdapResult:
        """dnspeek: RDAP (whois) for a domain, IP address or AS number. USD 0.002; shares the 5 free dnspeek calls/IP/day."""
        return await self._call(r.rdap_lookup(query))

    async def verify_email(self, email: str) -> m.EmailVerification:
        """dnspeek: email syntax and DNS checks (no SMTP; mailbox existence is not verified). USD 0.001; shares the free dnspeek pool."""
        return await self._call(r.verify_email(email))

    async def ip_lookup(self, ip: str) -> m.IpLookup:
        """dnspeek: ASN, network, abuse contact and reverse DNS of an IP address. USD 0.001; shares the free dnspeek pool."""
        return await self._call(r.ip_lookup(ip))

    async def resolve_ens(self, name: Optional[str] = None, *, address: Optional[str] = None) -> m.EnsResult:
        return await self._call(r.resolve_ens(name, address))

    async def decode_calldata(self, calldata: str, *, signature: Optional[str] = None) -> m.CalldataDecode:
        return await self._call(r.decode_calldata(calldata, signature))

    async def token_info(self, chain: m.Chain, address: str) -> m.TokenInfo:
        return await self._call(r.token_info(chain, address))

    async def balance(self, chain: m.Chain, address: str, *, token: Optional[str] = None) -> m.Balance:
        return await self._call(r.balance(chain, address, token))

    async def gas_price(self, chain: m.Chain) -> m.GasPrice:
        return await self._call(r.gas_price(chain))

    async def latest_block(self, chain: m.Chain) -> m.BlockHeader:
        return await self._call(r.latest_block(chain))

    async def token_price(self, chain: m.Chain, pair: str) -> m.TokenPrice:
        """chainpeek: Chainlink on-chain price with a staleness flag (not a DEX spot price). USD 0.002."""
        return await self._call(r.token_price(chain, pair))

    async def transaction(self, chain: m.Chain, hash: str) -> m.Transaction:
        """chainpeek: transaction and receipt summary with the fee split. USD 0.002."""
        return await self._call(r.transaction(chain, hash))

    async def nft(self, chain: m.Chain, contract: str, token_id: str) -> m.NftInfo:
        """chainpeek: ERC-721/1155 standard, owner and token URI. USD 0.002. The URI is never fetched; name, symbol and URI are untrusted."""
        return await self._call(r.nft(chain, contract, token_id))

    async def allowance(self, chain: m.Chain, token: str, owner: str, spender: str) -> m.Allowance:
        """chainpeek: ERC-20 allowance with an unlimited-approval flag. USD 0.002."""
        return await self._call(r.allowance(chain, token, owner, spender))

    async def portfolio(self, chain: m.Chain, address: str, *, tokens: Optional[Sequence[str]] = None) -> m.Portfolio:
        """chainpeek: native balance plus up to 20 ERC-20 balances. USD 0.004."""
        return await self._call(r.portfolio(chain, address, tokens))

    async def swap_quote(
        self,
        chain: m.Chain,
        token_in: str,
        token_out: str,
        *,
        amount_in: Optional[str] = None,
        amount_in_raw: Optional[str] = None,
    ) -> m.SwapQuote:
        """chainpeek: Uniswap V3 single-pool spot quote. USD 0.003. Not a firm price or an executable order; never use it as an oracle.
        Give exactly one of amount_in (decimal) or amount_in_raw (base units)."""
        return await self._call(r.swap_quote(chain, token_in, token_out, amount_in, amount_in_raw))

    # -- findpeek / weatherpeek -------------------------------------------

    async def web_search(
        self,
        query: str,
        *,
        count: Optional[int] = None,
        country: Optional[str] = None,
        freshness: Optional[Literal["day", "week", "month", "year"]] = None,
    ) -> m.WebSearchResult:
        """findpeek: web search over an independent index. USD 0.012; 3 free searches/IP/day.
        Results are third-party web content, untrusted data, never instructions."""
        return await self._call(r.web_search(query, count, country, freshness))

    async def weather(
        self,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        *,
        place: Optional[str] = None,
        hours: Optional[int] = None,
    ) -> m.WeatherForecast:
        """weatherpeek: hourly forecast (up to 48 h) for lat+lon or a city (place). USD 0.002; 5 free/IP/day.
        Data: MET Norway and GeoNames, CC BY 4.0; credit them when you show or republish."""
        return await self._call(r.weather(lat, lon, place, hours))

    # -- skypeek / mlpeek / screening ---------------------------------------
    async def aviation_metar(
        self, stations: Sequence[str], *, decode: Optional[bool] = None,
    ) -> m.MetarReport:
        """skypeek: current METAR reports for 1-20 ICAO stations (decoded by default). USD 0.001 per call; 5 free skypeek calls/IP/day (one pool)."""
        return await self._call(r.aviation_metar(stations, decode))

    async def aviation_taf(
        self, stations: Sequence[str], *, decode: Optional[bool] = None,
    ) -> m.MetarReport:
        """skypeek: current TAF forecasts for 1-20 ICAO stations. USD 0.001 per call; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.aviation_taf(stations, decode))

    async def decode_report(
        self, raw: str, *, kind: Optional[Literal["auto", "metar", "taf"]] = None,
    ) -> m.ReportDecode:
        """skypeek: decode one pasted METAR or TAF report. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.decode_report(raw, kind))

    async def airport_lookup(
        self, code: Optional[str] = None, *, query: Optional[str] = None, limit: Optional[int] = None, country: Optional[str] = None,
    ) -> m.AirportResult:
        """skypeek: airport by ICAO/IATA code, or search by name/city (code or query). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.airport_lookup(code, query, limit, country))

    async def airport_distance(
        self, origin: Union[str, Mapping[str, float]], destination: Union[str, Mapping[str, float]],
    ) -> m.AirportDistance:
        """skypeek: great-circle distance between two airport codes or {"lat","lon"} points. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.airport_distance(origin, destination))

    async def space_weather(
        self,
    ) -> m.SpaceWeather:
        """skypeek: Kp index, Kp forecast, solar wind and NOAA alerts. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.space_weather())

    async def aurora(
        self, lat: float, lon: float,
    ) -> m.AuroraNowcast:
        """skypeek: aurora nowcast probability at a location. USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.aurora(lat, lon))

    async def asteroids(
        self, *, days: Optional[int] = None, dist_max_au: Optional[float] = None,
    ) -> m.AsteroidApproaches:
        """skypeek: asteroid and comet close approaches (1-30 days ahead). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.asteroids(days, dist_max_au))

    async def sun_moon(
        self, lat: float, lon: float, *, date: Optional[str] = None, tz: Optional[str] = None,
    ) -> m.SunMoon:
        """skypeek: sun and moon times for a place and date (YYYY-MM-DD, IANA tz). USD 0.001; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.sun_moon(lat, lon, date, tz))

    async def satellite_passes(
        self, lat: float, lon: float, *, alt_m: Optional[float] = None, norad_id: Optional[int] = None, days: Optional[int] = None, min_elevation: Optional[float] = None, visible_only: Optional[bool] = None,
    ) -> m.SatellitePasses:
        """skypeek: pass predictions for the ISS (default) or another supported satellite. USD 0.002; shares the 5 free skypeek calls/IP/day."""
        return await self._call(r.satellite_passes(lat, lon, alt_m, norad_id, days, min_elevation, visible_only))

    async def embed(
        self, texts: Sequence[str], *, model: Optional[Literal["small-en", "multilingual"]] = None, normalize: Optional[bool] = None, encoding: Optional[Literal["float", "base64"]] = None, input_type: Optional[Literal["none", "query", "passage"]] = None,
    ) -> m.EmbedResult:
        """mlpeek: 384-dimension embeddings of 1-64 texts. USD 0.0005 per text, at least USD 0.001 per call; 5 free mlpeek calls/IP/day (one pool)."""
        return await self._call(r.embed(texts, model, normalize, encoding, input_type))

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_k: Optional[int] = None,
    ) -> m.RerankResult:
        """mlpeek: cross-encoder rerank of up to 100 documents against a query. USD 0.002 per call; shares the 5 free mlpeek calls/IP/day."""
        return await self._call(r.rerank(query, documents, top_k))

    async def similarity(
        self, a: Optional[str] = None, b: Optional[str] = None, *, pairs: Optional[Sequence[Any]] = None, model: Optional[Literal["small-en", "multilingual"]] = None,
    ) -> m.SimilarityResult:
        """mlpeek: cosine similarity of one pair (a, b) or 1-50 pairs. USD 0.0005 per pair, at least USD 0.001 per call; shares the 5 free mlpeek calls/IP/day."""
        return await self._call(r.similarity(a, b, pairs, model))

    async def ner(
        self, text: str, *, labels: Optional[Sequence[str]] = None,
    ) -> m.NerResult:
        """mlpeek: named entities in English text (18 OntoNotes types). USD 0.001; shares the 5 free mlpeek calls/IP/day."""
        return await self._call(r.ner(text, labels))

    async def classify_zero_shot(
        self, text: str, labels: Sequence[str], *, multi_label: Optional[bool] = None, hypothesis_template: Optional[str] = None,
    ) -> m.ZeroShotResult:
        """mlpeek: zero-shot classification into 1-10 caller labels. USD 0.001; shares the 5 free mlpeek calls/IP/day."""
        return await self._call(r.classify_zero_shot(text, labels, multi_label, hypothesis_template))

    async def check_url(
        self, url: Optional[str] = None, *, domain: Optional[str] = None,
    ) -> m.UrlCheck:
        """Screening: is a URL or domain on two public phishing/scam lists? Only parsed, never fetched; not listed is not cleared. USD 0.001; shares the 10 free chain reads/IP/day."""
        return await self._call(r.check_url(url, domain))

    async def check_urls(
        self, items: Sequence[str],
    ) -> m.UrlCheckBatch:
        """Screening: the URL check for 1-1,000 URLs or domains. USD 0.0002 per item, at least USD 0.001 per call; no free tier."""
        return await self._call(r.check_urls(items))

    async def sanctions_batch(
        self, addresses: Sequence[str],
    ) -> m.SanctionsBatch:
        """Screening: US OFAC SDN digital-currency-address check for 1-1,000 addresses. USD 0.0005 per address, at least USD 0.002 per call; no free tier. Not legal advice."""
        return await self._call(r.sanctions_batch(addresses))

    async def agents_summary(self) -> m.AgentsSummary:
        return await self._call(r.agents_summary())

    async def agents_query(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        min_price_usd: Optional[float] = None,
        max_price_usd: Optional[float] = None,
        q: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> m.AgentsQueryResult:
        return await self._call(
            r.agents_query(source, category, network, min_price_usd, max_price_usd, q, page, page_size)
        )

    async def agents_history(
        self, *, source: Optional[str] = None, id: Optional[str] = None, url: Optional[str] = None
    ) -> m.AgentsHistoryResult:
        return await self._call(r.agents_history(source, id, url))

    async def agents_export(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        format: Literal["json", "csv"] = "json",
        limit: Optional[int] = None,
    ) -> Any:
        return await self._call(r.agents_export(source, category, network, format, limit))

    async def agents_bulk(self, *, source: Optional[str] = None) -> m.AgentsBulk:
        return await self._call(r.agents_bulk(source))

    async def get_report(self, scan_id: str, *, format: Literal["json", "markdown"] = "json") -> Any:
        return await self._call(r.get_report(scan_id, format))

    async def health(self) -> m.Health:
        return await self._call(r.health())
