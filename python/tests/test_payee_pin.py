"""Payee pinning and the spend budget, against a real local HTTP server that mimics
POST /v1/check/address (nothing beyond 127.0.0.1, no real payments: throwaway keys)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import httpx
import pytest

from tanod import (
    DEFAULT_MAX_SPEND_USD,
    TANOD_PAYEES,
    AsyncTanod,
    BudgetExceededError,
    InvalidRequestError,
    PayeeMismatchError,
    PaymentError,
    PaymentRequiredError,
    Tanod,
    TanodError,
)

BASE = "eip155:8453"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TANOD = TANOD_PAYEES[BASE]
ATTACKER = "0x9999999999999999999999999999999999999999"
A = "0x" + "a" * 40
CHECK = {"schema": "precheck/1", "ok": True, "chain": "base", "address": A, "verdict": "low", "risk_score": 0, "reasons": []}


def b64(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def unb64(s: str) -> Any:
    return json.loads(base64.b64decode(s))


def requirement(pay_to: str, amount: str) -> dict:
    return {
        "scheme": "exact",
        "network": BASE,
        "asset": USDC,
        "amount": amount,
        "payTo": pay_to,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }


class FakeTanod:
    """A local server like the one in the report: every unpaid call gets this 402, every paid call a 200."""

    def __init__(self, accepts: list[dict], paid_status: int = 200) -> None:
        self.accepts = accepts
        self.paid_status = paid_status
        self.seen: list[Optional[str]] = []  # PAYMENT-SIGNATURE per request (None when unpaid)
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                signature = self.headers.get("PAYMENT-SIGNATURE")
                with fake._lock:
                    fake.seen.append(signature)
                if not signature:
                    pr = {
                        "x402Version": 2,
                        "error": "Payment required",
                        "resource": {"url": "http://127.0.0.1/v1/check/address", "description": "test", "mimeType": "application/json"},
                        "accepts": fake.accepts,
                        "extensions": {},
                    }
                    self._reply(402, pr, {"PAYMENT-REQUIRED": b64(pr)})
                    return
                accepted = unb64(signature)["accepted"]
                settle = {"success": True, "transaction": "0x" + "ab" * 32, "network": BASE,
                          "payer": "0x" + "11" * 20, "amount": accepted["amount"]}
                body = CHECK if fake.paid_status == 200 else {"error": "boom"}
                self._reply(fake.paid_status, body, {"PAYMENT-RESPONSE": b64(settle)})

            def _reply(self, status: int, body: Any, headers: dict[str, str]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def signatures(self) -> list[str]:
        return [s for s in self.seen if s]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def serve():
    servers: list[FakeTanod] = []

    def start(accepts: list[dict], paid_status: int = 200) -> FakeTanod:
        s = FakeTanod(accepts, paid_status)
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.stop()


def client_for(server: FakeTanod, **kw: Any) -> Tanod:
    kw.setdefault("use_env", False)
    kw.setdefault("max_retries", 0)
    return Tanod(base_url=server.url, http_client=httpx.Client(timeout=10, trust_env=False), **kw)


def x402_client(account, policy=None):
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    c = x402ClientSync()
    register_exact_evm_client(c, account, networks=BASE)
    if policy is not None:
        c.register_policy(policy)
    return c


# --- payee pinning -------------------------------------------------------------


def test_swapped_payee_is_refused_and_nothing_is_signed(serve, throwaway_account):
    server = serve([requirement(ATTACKER, "500000")])  # USD 0.50, under max_price_usd
    client = client_for(server, signer=throwaway_account)
    for _ in range(3):  # as in the report
        with pytest.raises(PayeeMismatchError) as ei:
            client.check_address(A)
        e = ei.value
        assert isinstance(e, PaymentRequiredError)  # existing except blocks still match
        assert e.code == "payee_mismatch"
        assert e.pay_to == ATTACKER and e.network == BASE
        assert e.price_usd == Decimal("0.5")
    assert len(server.seen) == 3
    assert server.signatures == []
    assert client.spent_usd == 0


def test_swapped_payee_is_refused_without_a_wallet_too(serve):
    server = serve([requirement(ATTACKER, "5000")])
    client = client_for(server)
    assert not client.has_wallet
    with pytest.raises(PayeeMismatchError):
        client.check_address(A)
    assert server.signatures == []


def test_tanod_payee_is_paid_normally_case_insensitive(serve, throwaway_account):
    server = serve([requirement(TANOD.lower(), "5000")])
    client = client_for(server, signer=throwaway_account)
    out = client.check_address(A)
    assert out.verdict == "low"
    assert out.meta.paid and out.meta.payment.price_usd == Decimal("0.005")
    assert len(server.signatures) == 1
    sig = unb64(server.signatures[0])
    assert sig["accepted"]["payTo"] == TANOD.lower()
    assert sig["accepted"]["amount"] == "5000"
    assert client.spent_usd == Decimal("0.005")


def test_x402_client_only_sees_the_vetted_requirement(serve, throwaway_account):
    # Honest Base entry first, a second Base entry to another payee after it. A custom
    # payment_client whose policy prefers the last entry must still pay the vetted one.
    server = serve([requirement(TANOD, "5000"), requirement(ATTACKER, "5000")])
    custom = x402_client(throwaway_account, policy=lambda v, reqs: reqs[-1:])
    client = client_for(server, payment_client=custom)
    client.check_address(A)
    assert len(server.signatures) == 1
    assert unb64(server.signatures[0])["accepted"]["payTo"] == TANOD


def test_custom_payment_client_signing_another_payee_is_not_sent(serve, throwaway_account):
    server = serve([requirement(TANOD, "5000")])
    custom = x402_client(throwaway_account, policy=lambda v, reqs: [r.model_copy(update={"pay_to": ATTACKER}) for r in reqs])
    client = client_for(server, payment_client=custom)
    with pytest.raises(PayeeMismatchError) as ei:
        client.check_address(A)
    assert ei.value.pay_to == ATTACKER
    assert server.signatures == []
    assert client.spent_usd == 0  # nothing left the process, so the reservation was released


def test_payees_none_disables_pinning(serve, throwaway_account):
    server = serve([requirement(ATTACKER, "500000")])
    client = client_for(server, signer=throwaway_account, payees=None)
    out = client.check_address(A)
    assert out.meta.paid
    assert len(server.signatures) == 1
    assert unb64(server.signatures[0])["accepted"]["payTo"] == ATTACKER
    assert client.spent_usd == Decimal("0.5")


def test_flat_list_and_per_network_payees(serve, throwaway_account):
    own = "0x" + "12" * 20
    flat = serve([requirement(own, "5000")])
    client_for(flat, signer=throwaway_account, payees=["0x" + own[2:].upper()]).check_address(A)
    assert len(flat.signatures) == 1

    per_network = serve([requirement(own, "5000")])
    client_for(per_network, signer=throwaway_account, payees={BASE: [TANOD, own]}).check_address(A)
    assert len(per_network.signatures) == 1

    other_network = serve([requirement(own, "5000")])  # allowed on Polygon only: refused on Base
    with pytest.raises(PayeeMismatchError):
        client_for(other_network, signer=throwaway_account, payees={"eip155:137": own}).check_address(A)
    assert other_network.signatures == []


def test_default_allow_list_is_tanods_published_payees():
    assert TANOD_PAYEES[BASE] == "0x593857A4a4F619543ea12394137C3004ce841720"
    assert TANOD_PAYEES["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"] == "RMk9k3VC8dr4hhjgFh88rGhjAbzfBzHxJJ5n2fTcqus"
    with pytest.raises(TypeError):
        TANOD_PAYEES[BASE] = ATTACKER  # type: ignore[index]


def test_solana_payees_compare_exactly():
    from tanod._payment import payee_allowed, payee_table

    table = payee_table(TANOD_PAYEES)
    sol = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    assert payee_allowed(table, sol, "RMk9k3VC8dr4hhjgFh88rGhjAbzfBzHxJJ5n2fTcqus")
    assert not payee_allowed(table, sol, "rmk9k3vc8dr4hhjgfh88rghjabzfbzhxjj5n2ftcqus")
    assert payee_allowed(table, BASE, TANOD.upper().replace("0X", "0x"))
    assert not payee_allowed(table, "eip155:1", TANOD)  # network not listed


@pytest.mark.parametrize("bad", [[], {}, {BASE: []}, [""], [123]])
def test_empty_or_malformed_allow_list_is_rejected(bad):
    with pytest.raises(InvalidRequestError):
        Tanod(use_env=False, payees=bad)


# --- spend budget ---------------------------------------------------------------


def test_default_budget_is_five_usd(serve, throwaway_account):
    assert DEFAULT_MAX_SPEND_USD == 5.0
    server = serve([requirement(TANOD, "500000")])  # USD 0.50 per call
    client = client_for(server, signer=throwaway_account)
    assert client.max_spend_usd == 5.0
    for _ in range(10):
        client.check_address(A)
    assert client.spent_usd == Decimal("5")
    with pytest.raises(BudgetExceededError) as ei:
        client.check_address(A)
    e = ei.value
    assert isinstance(e, PaymentRequiredError)
    assert e.code == "budget_exceeded"
    assert e.spent_usd == Decimal("5") and e.max_spend_usd == Decimal("5.0")
    assert e.price_usd == Decimal("0.5")
    assert len(server.signatures) == 10
    assert len(server.seen) == 21  # 10 x (unpaid + paid) + the refused unpaid call


def test_budget_stops_payments_once_reached(serve, throwaway_account):
    server = serve([requirement(TANOD, "5000")])
    client = client_for(server, signer=throwaway_account, max_spend_usd=0.012)
    client.check_address(A)
    client.check_address(A)
    assert client.spent_usd == Decimal("0.01")
    for _ in range(2):
        with pytest.raises(BudgetExceededError):
            client.check_address(A)
    assert len(server.signatures) == 2
    assert client.spent_usd == Decimal("0.01")


def test_budget_holds_across_threads(serve, throwaway_account):
    server = serve([requirement(TANOD, "5000")])
    client = client_for(server, signer=throwaway_account, max_spend_usd=0.015)

    def call() -> str:
        try:
            client.check_address(A)
            return "paid"
        except BudgetExceededError:
            return "refused"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: call(), range(6)))
    assert results.count("paid") == 3 and results.count("refused") == 3
    assert len(server.signatures) == 3
    assert client.spent_usd == Decimal("0.015")


def test_max_spend_usd_none_removes_the_cap(serve, throwaway_account):
    server = serve([requirement(TANOD, "1000000")])  # USD 1.00
    client = client_for(server, signer=throwaway_account, max_spend_usd=None)
    for _ in range(6):
        client.check_address(A)
    assert len(server.signatures) == 6
    assert client.spent_usd == Decimal("6")


def test_reservation_released_when_signing_fails(serve):
    from x402 import x402ClientSync

    server = serve([requirement(TANOD, "5000")])
    client = client_for(server, payment_client=x402ClientSync())  # no scheme registered: signing fails
    with pytest.raises(PaymentError):
        client.check_address(A)
    assert server.signatures == []
    assert client.spent_usd == 0


def test_reservation_kept_when_the_paid_request_fails(serve, throwaway_account):
    server = serve([requirement(TANOD, "5000")], paid_status=500)
    client = client_for(server, signer=throwaway_account)
    with pytest.raises(TanodError):
        client.check_address(A)
    assert len(server.signatures) == 1
    assert client.spent_usd == Decimal("0.005")  # the payment may still settle


@pytest.mark.parametrize("bad", [-1, float("nan"), "lots", True])
def test_bad_budget_is_rejected(bad):
    with pytest.raises(InvalidRequestError):
        Tanod(use_env=False, max_spend_usd=bad)


# --- async ------------------------------------------------------------------------


def test_async_pinning_and_budget(serve, throwaway_account):
    swapped = serve([requirement(ATTACKER, "500000")])
    honest = serve([requirement(TANOD, "5000")])

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=0, base_url=swapped.url,
                              http_client=httpx.AsyncClient(timeout=10, trust_env=False)) as c:
            with pytest.raises(PayeeMismatchError):
                await c.check_address(A)
            assert c.spent_usd == 0
        async with AsyncTanod(signer=throwaway_account, use_env=False, max_retries=0, base_url=honest.url,
                              max_spend_usd=0.015, http_client=httpx.AsyncClient(timeout=10, trust_env=False)) as c:
            results = await asyncio.gather(*(c.check_address(A) for _ in range(6)), return_exceptions=True)
            return c.spent_usd, results

    spent, results = asyncio.run(run())
    assert swapped.signatures == []
    assert sum(isinstance(r, BudgetExceededError) for r in results) == 3
    assert sum(not isinstance(r, Exception) for r in results) == 3
    assert len(honest.signatures) == 3
    assert spent == Decimal("0.015")
