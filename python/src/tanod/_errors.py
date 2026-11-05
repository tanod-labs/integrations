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


class PayeeMismatchError(PaymentRequiredError):
    """The quote names a payee (``pay_to``) that is not on the client's allow-list; nothing was signed.

    The allow-list is ``payees`` (by default Tanod's own published payees). ``pay_to`` and
    ``network`` hold the quoted values. This usually means ``base_url`` points somewhere other
    than Tanod.
    """

    code = "payee_mismatch"


class BudgetExceededError(PaymentRequiredError):
    """Paying this quote would take the client's total past ``max_spend_usd``; nothing was signed.

    ``spent_usd`` is what the client had already signed; ``max_spend_usd`` is its budget.
    """

    code = "budget_exceeded"

    def __init__(self, message: str, *, spent_usd: Decimal, max_spend_usd: Decimal, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.spent_usd = spent_usd
        self.max_spend_usd = max_spend_usd


class PaymentError(TanodError):
    """Building or signing the x402 payment failed, or the signed payload did not match the
    vetted quote (nothing was sent)."""


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

    Never charged: answers that may be charged raise ``PaymentUnresolvedError`` instead.
    """

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class PaymentUnresolvedError(TanodError):
    """A payment for this request was sent and its outcome must be reconciled. Do NOT pay again.

    The client never retries this and never signs a new payment for it. Keep ``payer`` and
    ``nonce`` (the signed EIP-3009 authorization; this client sends no x402 payment-identifier)
    and ``server_message`` for reconciliation with ops@tanod.dev.

    ``charged``: ``True`` the payment settled but no result came back (``receipt`` holds it);
    ``None`` unknown; ``False`` this payment was not charged, but an earlier identical one is
    unresolved. Raised as is (``code`` is the server's code or None) after a payment was sent for:
    an error answer that carries a settlement receipt; a 5xx or 429 whose code the server does not
    document as uncharged (or no code); no answer at all (timeout, dropped connection); a 2xx that
    cannot be read. The subclasses below cover the server's documented codes.
    """

    code: str | None = None
    charged: bool | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        server_message: str | None = None,
        charged: bool | None = None,
        payer: str | None = None,
        nonce: str | None = None,
        payment_signature: str | None = None,
        price_usd: Decimal | None = None,
        amount_atomic: str | None = None,
        receipt: Any = None,
        settlement: Any = None,
        status_code: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        if code is not None:
            self.code = code
        if charged is not None:
            self.charged = charged
        self.server_message = server_message
        self.payer = payer
        self.nonce = nonce
        self.payment_signature = payment_signature  # the PAYMENT-SIGNATURE header that was sent
        self.price_usd = price_usd
        self.amount_atomic = amount_atomic
        self.receipt = receipt
        self.settlement = settlement


class SettlementUnknownError(PaymentUnresolvedError):
    """503 ``settlement_unknown``: the settlement was submitted and its outcome is unconfirmed.

    You may have been charged. Do not pay again; keep ``payer`` and ``nonce`` for reconciliation.
    """

    code = "settlement_unknown"


class PurchaseUnresolvedError(PaymentUnresolvedError):
    """409 ``purchase_unresolved``: an identical earlier purchase from this wallet is unresolved.

    This payment was verified but not submitted or charged (``charged`` is False). Do not pay
    again for this request until the earlier one is reconciled; a different input is a new purchase.
    """

    code = "purchase_unresolved"
    charged = False


class CreditDeliveryPendingError(PaymentUnresolvedError):
    """503 ``credit_delivery_pending``: the payment is confirmed but prepaid credit delivery is pending.

    Keep the original payment; do not pay again.
    """

    code = "credit_delivery_pending"
    charged = True


class WalletNotConfiguredError(TanodError):
    """Payment support was requested but the wallet extra is not installed."""
