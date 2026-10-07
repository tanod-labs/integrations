"""Test helpers: an in-memory fake of the Tanod HTTP API (no network, no real payments)."""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Optional

import httpx
import pytest

PAY_TO = "0x593857A4a4F619543ea12394137C3004ce841720"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def b64json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def payment_required(amount: str = "1000", error: Optional[str] = None, url: str = "https://tanod.dev/v1/chain/gas") -> dict:
    """Same shape as a live Tanod 402 (x402 v2)."""
    return {
        "x402Version": 2,
        "error": error or "Payment required",
        "resource": {"url": url, "description": "test", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": USDC_BASE,
                "amount": amount,
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {},
    }


def response_402(amount: str = "1000", error: Optional[str] = None, header: bool = True) -> httpx.Response:
    pr = payment_required(amount, error)
    headers = {"PAYMENT-REQUIRED": b64json(pr)} if header else {}
    return httpx.Response(402, json=pr, headers=headers)


def settle_header(amount: str = "1000") -> str:
    return b64json(
        {
            "success": True,
            "transaction": "0x" + "ab" * 32,
            "network": "eip155:8453",
            "payer": "0x" + "11" * 20,
            "amount": amount,
        }
    )


class FakeAPI:
    """Collects requests; a handler decides each response."""

    def __init__(self, handler: Callable[[httpx.Request, int], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request, len(self.requests))

    def body(self, i: int = 0) -> Any:
        return json.loads(self.requests[i].content or b"null")


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANOD_PRIVATE_KEY", raising=False)


@pytest.fixture
def throwaway_account():
    from eth_account import Account

    return Account.create()  # random, unfunded; signatures are never sent anywhere real
