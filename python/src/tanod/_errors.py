"""Exceptions raised by the Tanod client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class TanodError(Exception):
    """Base class for every error raised by this package."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


class PaymentRequiredError(TanodError):
    """The call needs payment and the client could not (or would not) pay.

    Raised when no wallet is configured and the free daily tier is used up, or
    when a paid retry was refused. ``price_usd`` is the quoted price.
    """

    def __init__(
        self,
        message: str,
        *,
        price_usd: Decimal | None,
        amount_atomic: str | None = None,
        asset: str | None = None,
        network: str | None = None,
        pay_to: str | None = None,
        reason: str | None = None,
        payment_required: Any = None,
        status_code: int | None = 402,
        body: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.price_usd = price_usd
        self.amount_atomic = amount_atomic
        self.asset = asset
        self.network = network
        self.pay_to = pay_to
        self.reason = reason
        self.payment_required = payment_required


class PriceLimitExceededError(PaymentRequiredError):
    """The quoted price is above the client's ``max_price_usd``; nothing was signed."""


class PaymentError(TanodError):
    """Building or signing the x402 payment failed (nothing was sent)."""


class InvalidRequestError(TanodError):
    """The input was refused (422, 413, 415, or an unpaid 402 that names the input error).

    Never charged.
    """


class NotFoundError(TanodError):
    """404: unverified contract, unknown package/record, or unknown report. Never charged."""


class RateLimitError(TanodError):
    """429 after the configured retries."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ServiceUnavailableError(TanodError):
    """502/503/504 after the configured retries (busy queue, chain or facilitator down).

    Never charged.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class WalletNotConfiguredError(TanodError):
    """Payment support was requested but the wallet extra is not installed."""
