"""x402 glue: wallet setup, 402 parsing, receipts. Uses the official ``x402`` package.

Private keys are turned into an ``eth_account`` account immediately and never
stored as strings, logged, or included in reprs or errors.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Callable, Optional

from ._errors import PaymentError, WalletNotConfiguredError
from ._models import PaymentReceipt

BASE_MAINNET = "eip155:8453"
USDC_DECIMALS = 6
ENV_PRIVATE_KEY = "TANOD_PRIVATE_KEY"

_INSTALL_HINT = 'Payments need the wallet extra: pip install "tanod[wallet]"'


def atomic_to_usd(amount: Optional[str]) -> Optional[Decimal]:
    if amount is None:
        return None
    try:
        return Decimal(int(amount)) / (Decimal(10) ** USDC_DECIMALS)
    except (TypeError, ValueError):
        return None


def build_payment_client(
    *,
    private_key: Optional[str],
    signer: Any,
    payment_client: Any,
    max_price_usd: Optional[float],
    use_env: bool,
    is_async: bool,
) -> Any:
    """Return an x402 client (sync or async) or ``None`` when no wallet is configured."""
    if payment_client is not None:
        return payment_client
    if signer is None and private_key is None and use_env:
        private_key = os.environ.get(ENV_PRIVATE_KEY) or None
    if signer is None and private_key is None:
        return None
    try:
        from x402 import x402Client, x402ClientSync
        from x402.mechanisms.evm.exact.register import register_exact_evm_client
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise WalletNotConfiguredError(_INSTALL_HINT) from exc
    if signer is None:
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover
            raise WalletNotConfiguredError(_INSTALL_HINT) from exc
        key = private_key.strip()  # type: ignore[union-attr]
        if not key.startswith("0x"):
            key = "0x" + key
        try:
            signer = Account.from_key(key)
        except Exception:
            # Never echo the key (or the library's message, which may contain it).
            raise PaymentError(
                f"{ENV_PRIVATE_KEY} / private_key is not a valid 32-byte hex private key"
            ) from None
    client = x402Client() if is_async else x402ClientSync()
    register_exact_evm_client(client, signer, networks=BASE_MAINNET)
    if max_price_usd is None:
        client.set_spend_controls({"max_amount_per_payment": False})
    else:
        client.set_spend_controls({"max_amount_per_payment": f"${max_price_usd}"})
    return client


def parse_payment_required(headers: Callable[[str], Optional[str]], body: Any) -> Any:
    """Decode the x402 v2 ``PaymentRequired`` from the header (or the identical JSON body)."""
    from x402.http.utils import decode_payment_required_header
    from x402.schemas import PaymentRequired

    header = headers("PAYMENT-REQUIRED")
    if header:
        try:
            return decode_payment_required_header(header)
        except Exception:
            pass
    if isinstance(body, dict) and body.get("accepts"):
        try:
            return PaymentRequired.model_validate(body)
        except Exception:
            pass
    return None


def first_requirement(pr: Any) -> Any:
    accepts = getattr(pr, "accepts", None) or []
    for req in accepts:
        if getattr(req, "network", None) == BASE_MAINNET:
            return req
    return accepts[0] if accepts else None


def decode_receipt(
    headers: Callable[[str], Optional[str]], price_usd: Optional[Decimal], amount: Optional[str]
) -> Optional[PaymentReceipt]:
    from x402.http.utils import decode_payment_response_header

    header = headers("PAYMENT-RESPONSE") or headers("X-PAYMENT-RESPONSE")
    if not header:
        return None
    try:
        settle = decode_payment_response_header(header)
    except Exception:
        return None
    raw = settle.model_dump(by_alias=True, exclude_none=True)
    settled_amount = settle.amount or amount
    return PaymentReceipt(
        success=bool(settle.success),
        transaction=settle.transaction,
        network=str(settle.network),
        payer=settle.payer,
        amount_atomic=settled_amount,
        price_usd=atomic_to_usd(settled_amount) if settled_amount else price_usd,
        raw=raw,
    )
