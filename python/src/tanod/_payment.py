"""x402 glue: wallet setup, 402 parsing, receipts. Uses the official ``x402`` package.

Private keys are turned into an ``eth_account`` account immediately and never
stored as strings, logged, or included in reprs or errors.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from ._errors import InvalidRequestError, PaymentError, WalletNotConfiguredError
from ._models import PaymentReceipt

BASE_MAINNET = "eip155:8453"
USDC_DECIMALS = 6
ENV_PRIVATE_KEY = "TANOD_PRIVATE_KEY"

# Tanod's own payees (x402 ``payTo``) per CAIP-2 network: the default allow-list for ``payees``.
# Source: the ``accepts`` list of an unpaid POST https://tanod.dev/v1/check/address
# (PAYMENT-REQUIRED header and JSON body agree), read 2026-10-15. EVM networks share one address.
TANOD_PAYEES: Mapping[str, str] = MappingProxyType(
    {
        "eip155:8453": "0x593857A4a4F619543ea12394137C3004ce841720",  # Base
        "eip155:137": "0x593857A4a4F619543ea12394137C3004ce841720",  # Polygon
        "eip155:56": "0x593857A4a4F619543ea12394137C3004ce841720",  # BNB Chain
        "eip155:42161": "0x593857A4a4F619543ea12394137C3004ce841720",  # Arbitrum One
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": "RMk9k3VC8dr4hhjgFh88rGhjAbzfBzHxJJ5n2fTcqus",  # Solana mainnet
    }
)

#: Allow-list of payees: per CAIP-2 network (``{"eip155:8453": "0x..."}``), or a flat list that
#: applies to every network. EVM addresses compare case-insensitively, others exactly.
Payees = Union[Mapping[str, Union[str, Sequence[str]]], Sequence[str]]

# network (CAIP-2) or "*" (a flat list: any network) -> allowed payees
PayeeTable = Mapping[str, tuple[str, ...]]

_INSTALL_HINT = 'Payments need the wallet extra: pip install "tanod[wallet]"'


def atomic_to_usd(amount: Optional[str]) -> Optional[Decimal]:
    if amount is None:
        return None
    try:
        return Decimal(int(amount)) / (Decimal(10) ** USDC_DECIMALS)
    except (TypeError, ValueError):
        return None


def usd_to_atomic(usd: Any, what: str) -> int:
    """A non-negative USD amount in USDC atomic units (6 decimals)."""
    try:
        if isinstance(usd, bool):
            raise TypeError
        value = Decimal(str(usd))
        if not value.is_finite() or value < 0:
            raise ValueError
    except (TypeError, ValueError, InvalidOperation):
        raise InvalidRequestError(f"{what} must be a non-negative number of USD, or None") from None
    return int((value * (Decimal(10) ** USDC_DECIMALS)).to_integral_value(rounding=ROUND_HALF_UP))


def payee_table(payees: Any) -> Optional[PayeeTable]:
    """Normalize ``payees`` (``None`` disables pinning); raise on an empty or malformed list."""
    if payees is None:
        return None

    def as_tuple(value: Any, where: str) -> tuple[str, ...]:
        if isinstance(value, str):
            items: Any = [value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
        else:
            items = None
        if not items or not all(isinstance(a, str) and a.strip() for a in items):
            raise InvalidRequestError(
                f"payees{where} must be an address or a non-empty list of addresses (None disables pinning)"
            )
        return tuple(a.strip() for a in items)

    if isinstance(payees, Mapping):
        table = {str(net): as_tuple(v, f"[{net!r}]") for net, v in payees.items()}
    else:
        table = {"*": as_tuple(payees, "")}
    if not table:
        raise InvalidRequestError("payees must name at least one payee (None disables pinning)")
    return MappingProxyType(table)


def payee_allowed(table: PayeeTable, network: Any, pay_to: Any) -> bool:
    if not isinstance(network, str) or not isinstance(pay_to, str):
        return False
    allowed = table.get(network) or table.get("*")
    if not allowed:
        return False
    if network.startswith("eip155:"):
        lowered = pay_to.lower()
        return any(a.lower() == lowered for a in allowed)
    return pay_to in allowed


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
