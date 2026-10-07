from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import httpx
import pytest

import tanod
from tanod import (
    AsyncTanod,
    InvalidRequestError,
    NotFoundError,
    PaymentRequiredError,
    PriceLimitExceededError,
    RateLimitError,
    ServiceUnavailableError,
    Tanod,
)

from conftest import PAY_TO, FakeAPI, response_402, settle_header

GAS = {"chain": "base", "gas_price_wei": "6000000", "gas_price_gwei": "0.006", "base_fee_gwei": "0.005"}


def make(handler, **kw) -> tuple[Tanod, FakeAPI]:
    api = FakeAPI(handler)
    kw.setdefault("use_env", False)
    client = Tanod(http_client=httpx.Client(transport=httpx.MockTransport(api)), **kw)
    return client, api


# --- free tier / no wallet -------------------------------------------------


def test_free_tier_call_without_wallet():
    def h(req, n):
        return httpx.Response(200, json=GAS, headers={"X-Free-Remaining-Today": "6", "X-Tanod-Product": "chainpeek"})

    client, api = make(h)
    out = client.gas_price("base")
    assert out.gas_price_gwei == "0.006"
    assert out.meta.free_remaining_today == 6
    assert out.meta.product == "chainpeek"
    assert out.meta.payment is None and not out.meta.paid
    assert api.requests[0].url.path == "/v1/chain/gas"
    assert api.body() == {"chain": "base"}
    assert "PAYMENT-SIGNATURE" not in api.requests[0].headers
    assert api.requests[0].headers["X-Tanod-Free"] == "1"   # free tier is opt-in over HTTP


def test_use_free_tier_false_omits_the_opt_in_header():
    client, api = make(lambda req, n: response_402("1000"), use_free_tier=False)
    assert client.use_free_tier is False
    with pytest.raises(PaymentRequiredError):
        client.gas_price("base")
    assert "X-Tanod-Free" not in api.requests[0].headers


def test_402_without_wallet_reports_price():
    client, api = make(lambda req, n: response_402("1000"))
    with pytest.raises(PaymentRequiredError) as ei:
        client.gas_price("base")
    e = ei.value
    assert e.price_usd == Decimal("0.001")
    assert e.amount_atomic == "1000"
    assert e.pay_to == PAY_TO
    assert e.network == "eip155:8453"
    assert "USD 0.001" in str(e)
    assert len(api.requests) == 1


def test_402_body_only_is_parsed():
    client, _ = make(lambda req, n: response_402("5000", header=False))
    with pytest.raises(PaymentRequiredError) as ei:
        client.check_address("0x" + "1" * 40)
    assert ei.value.price_usd == Decimal("0.005")


def test_402_input_refused_is_invalid_request_and_never_signed(throwaway_account):
    msg = "Payment required (USD 0.001 base price shown). The input would be refused: invalid_request: bad chain."
    client, api = make(lambda req, n: response_402("1000", error=msg), signer=throwaway_account)
    with pytest.raises(InvalidRequestError, match="would be refused"):
        client.gas_price("base")
    assert len(api.requests) == 1


# --- paid flow -------------------------------------------------------------


def test_paid_flow_signs_retries_and_returns_receipt(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header("1000")})

    client, api = make(h, signer=throwaway_account)
    assert client.has_wallet
    out = client.gas_price("base")
    assert len(api.requests) == 2
    assert api.body(0) == api.body(1) == {"chain": "base"}
    assert api.requests[0].headers["X-Tanod-Free"] == "1"
    assert "X-Tanod-Free" not in api.requests[1].headers   # never on the paid retry
    sig = json.loads(base64.b64decode(api.requests[1].headers["PAYMENT-SIGNATURE"]))
    assert sig["x402Version"] == 2
    assert sig["accepted"]["amount"] == "1000"
    assert sig["accepted"]["payTo"] == PAY_TO
    assert sig["payload"]["authorization"]["from"].lower() == throwaway_account.address.lower()
    receipt = out.meta.payment
    assert receipt is not None and receipt.success
    assert receipt.transaction == "0x" + "ab" * 32
    assert receipt.price_usd == Decimal("0.001")
    assert out.meta.paid


def test_private_key_argument_and_env(monkeypatch, throwaway_account):
    key = throwaway_account.key.hex()
    c1 = Tanod(private_key=key, use_env=False)
    assert c1.has_wallet
    assert key not in repr(c1) and key.removeprefix("0x") not in repr(c1)
    monkeypatch.setenv("TANOD_PRIVATE_KEY", key)
    assert Tanod().has_wallet
    assert not Tanod(use_env=False).has_wallet


def test_invalid_key_error_does_not_echo_key():
    bad = "0xnot-a-real-key-SECRET"
    with pytest.raises(tanod.PaymentError) as ei:
        Tanod(private_key=bad, use_env=False)
    assert "SECRET" not in str(ei.value)


def test_price_cap_blocks_signing(throwaway_account):
    client, api = make(lambda req, n: response_402("2000000"), signer=throwaway_account)
    with pytest.raises(PriceLimitExceededError) as ei:
        client.agents_bulk()
    assert ei.value.price_usd == Decimal("2")
    assert len(api.requests) == 1


def test_raised_cap_allows_bulk(throwaway_account):
    bulk = {"generated_at": "2026-10-07", "count": 0, "records": []}

    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("2000000")
        return httpx.Response(200, json=bulk, headers={"PAYMENT-RESPONSE": settle_header("2000000")})

    client, api = make(h, signer=throwaway_account, max_price_usd=2.5)
    out = client.agents_bulk()
    assert out.count == 0 and out.meta.payment.price_usd == Decimal("2")


def test_paid_retry_refused_raises_with_reason(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        return response_402("3000", error="price_mismatch")

    client, api = make(h, signer=throwaway_account)
    with pytest.raises(PaymentRequiredError) as ei:
        client.gas_price("base")
    assert ei.value.reason == "price_mismatch"
    assert len(api.requests) == 2


# --- retries and errors ----------------------------------------------------


def test_503_is_retried_then_succeeds():
    def h(req, n):
        if n == 1:
            return httpx.Response(503, json={"error": {"code": "busy"}}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=GAS)

    client, api = make(h)
    assert client.gas_price("base").chain == "base"
    assert len(api.requests) == 2


def test_503_exhausts_retries():
    client, api = make(lambda req, n: httpx.Response(503, json={"error": {"code": "busy"}}, headers={"Retry-After": "0"}), max_retries=2)
    with pytest.raises(ServiceUnavailableError) as ei:
        client.gas_price("base")
    assert ei.value.status_code == 503
    assert len(api.requests) == 3


def test_paid_503_restarts_flow_with_fresh_payment(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        if n == 2:
            return httpx.Response(503, json={"error": {"code": "facilitator_unavailable"}}, headers={"Retry-After": "0"})
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header()})

    client, api = make(h, signer=throwaway_account)
    out = client.gas_price("base")
    assert out.meta.paid
    assert len(api.requests) == 4  # unpaid, paid(503), unpaid, paid
    assert api.requests[1].headers["PAYMENT-SIGNATURE"] != api.requests[3].headers["PAYMENT-SIGNATURE"]


def test_429_with_long_retry_after_raises_immediately():
    client, api = make(lambda req, n: httpx.Response(429, json={"error": {"code": "rate_limited"}}, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitError) as ei:
        client.gas_price("base")
    assert ei.value.retry_after == 120
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "status,exc",
    [(404, NotFoundError), (422, InvalidRequestError), (413, InvalidRequestError), (502, ServiceUnavailableError)],
)
def test_status_mapping(status, exc):
    client, _ = make(lambda req, n: httpx.Response(status, json={"error": {"code": "x", "message": "nope"}}))
    with pytest.raises(exc, match="nope"):
        client.scan_contract_address("0x" + "a" * 40)


def test_client_side_validation_makes_no_request():
    client, api = make(lambda req, n: httpx.Response(200, json=GAS))
    with pytest.raises(InvalidRequestError):
        client.check_address("0x123")
    with pytest.raises(InvalidRequestError):
        client.gas_price("solana")  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        client.resolve_ens("vitalik.eth", address="0x" + "1" * 40)
    with pytest.raises(InvalidRequestError):
        client.scan_package()
    with pytest.raises(InvalidRequestError):
        client.agents_history(source="cdp_bazaar")
    assert api.requests == []


# --- request shapes for every route ---------------------------------------

A = "0x" + "a" * 40
B = "0x" + "b" * 40
TX = "0x" + "c" * 64

ROUTES = [
    (lambda c: c.scan_contract_source("contract C {}", include_informational=True), "POST", "/v1/scan/source",
     {"source": "contract C {}", "options": {"include_informational": True}}),
    (lambda c: c.scan_contract_source(standard_json={"language": "Solidity"}, compiler_version="0.8.26"), "POST", "/v1/scan/source",
     {"standard_json": {"language": "Solidity"}, "compiler_version": "0.8.26"}),
    (lambda c: c.scan_contract_address(A, "base"), "POST", "/v1/scan/address", {"address": A, "chain": "base"}),
    (lambda c: c.get_contract_source("ethereum", A), "GET", f"/v1/source/ethereum/{A}", None),
    (lambda c: c.check_address(A), "POST", "/v1/check/address", {"address": A, "chain": "base"}),
    (lambda c: c.scan_package("npm:left-pad@1.3.0"), "POST", "/v1/scan/package", {"source": "npm:left-pad@1.3.0"}),
    (lambda c: c.scan_package(content=b"# skill", filename="SKILL.md"), "POST", "/v1/scan/package",
     {"content_base64": base64.b64encode(b"# skill").decode(), "filename": "SKILL.md"}),
    (lambda c: c.render("https://example.com"), "POST", "/v1/render", {"url": "https://example.com", "format": "markdown"}),
    (lambda c: c.render("https://example.com", format="screenshot", width=800), "POST", "/v1/render",
     {"url": "https://example.com", "format": "screenshot", "width": 800}),
    (lambda c: c.inspect_domain("example.com", checks=["dns"]), "POST", "/v1/domain/inspect", {"domain": "example.com", "checks": ["dns"]}),
    (lambda c: c.resolve_ens("vitalik.eth"), "POST", "/v1/chain/ens", {"name": "vitalik.eth"}),
    (lambda c: c.resolve_ens(address=A), "POST", "/v1/chain/ens", {"address": A}),
    (lambda c: c.decode_calldata("0xa9059cbb" + "0" * 128), "POST", "/v1/chain/calldata", {"calldata": "0xa9059cbb" + "0" * 128}),
    (lambda c: c.token_info("base", A), "POST", "/v1/chain/token", {"chain": "base", "address": A}),
    (lambda c: c.balance("base", A, token=B), "POST", "/v1/chain/balance", {"chain": "base", "address": A, "token": B}),
    (lambda c: c.gas_price("ethereum"), "POST", "/v1/chain/gas", {"chain": "ethereum"}),
    (lambda c: c.latest_block("base"), "POST", "/v1/chain/block", {"chain": "base"}),
    (lambda c: c.extract_pdf("https://example.com/a.pdf", max_pages=5), "POST", "/v1/pdf", {"url": "https://example.com/a.pdf", "max_pages": 5}),
    (lambda c: c.page_meta("https://example.com"), "POST", "/v1/meta", {"url": "https://example.com"}),
    (lambda c: c.ocr_image("https://example.com/a.png", lang="eng"), "POST", "/v1/ocr", {"url": "https://example.com/a.png", "lang": "eng"}),
    (lambda c: c.rdap_lookup("example.com"), "POST", "/v1/rdap", {"query": "example.com"}),
    (lambda c: c.verify_email("a@example.com"), "POST", "/v1/email/verify", {"email": "a@example.com"}),
    (lambda c: c.ip_lookup("1.1.1.1"), "POST", "/v1/ip", {"ip": "1.1.1.1"}),
    (lambda c: c.token_price("base", "ETH/USD"), "POST", "/v1/chain/price", {"chain": "base", "pair": "ETH/USD"}),
    (lambda c: c.transaction("base", TX), "POST", "/v1/chain/tx", {"chain": "base", "hash": TX}),
    (lambda c: c.nft("ethereum", A, "1"), "POST", "/v1/chain/nft", {"chain": "ethereum", "contract": A, "token_id": "1"}),
    (lambda c: c.allowance("base", A, B, A), "POST", "/v1/chain/allowance", {"chain": "base", "token": A, "owner": B, "spender": A}),
    (lambda c: c.portfolio("base", A, tokens=[B]), "POST", "/v1/chain/portfolio", {"chain": "base", "address": A, "tokens": [B]}),
    (lambda c: c.swap_quote("base", A, B, amount_in="1"), "POST", "/v1/chain/quote", {"chain": "base", "token_in": A, "token_out": B, "amount_in": "1"}),
    (lambda c: c.swap_quote("base", A, B, amount_in_raw="1000000"), "POST", "/v1/chain/quote",
     {"chain": "base", "token_in": A, "token_out": B, "amount_in_raw": "1000000"}),
    (lambda c: c.web_search("x402", count=3, freshness="week"), "POST", "/v1/search", {"query": "x402", "count": 3, "freshness": "week"}),
    (lambda c: c.weather(place="Oslo, NO", hours=12), "POST", "/v1/weather", {"place": "Oslo, NO", "hours": 12}),
    (lambda c: c.weather(59.9, 10.7), "POST", "/v1/weather", {"lat": 59.9, "lon": 10.7}),
    (lambda c: c.agents_summary(), "GET", "/v1/agents/summary", None),
    (lambda c: c.agents_query(network="base", q="weather", page_size=10), "POST", "/v1/agents/query",
     {"network": "base", "q": "weather", "page_size": 10}),
    (lambda c: c.agents_history(url="https://x.example/api"), "POST", "/v1/agents/history", {"url": "https://x.example/api"}),
    (lambda c: c.agents_export(source="payai", limit=10), "POST", "/v1/agents/export", {"source": "payai", "format": "json", "limit": 10}),
    (lambda c: c.agents_bulk(source="smithery"), "POST", "/v1/agents/bulk", {"source": "smithery"}),
    (lambda c: c.get_report("abc123"), "GET", "/v1/report/abc123.json", None),
    (lambda c: c.health(), "GET", "/healthz", None),
]


@pytest.mark.parametrize("call,method,path,body", ROUTES)
def test_route_request_shape(call, method, path, body):
    client, api = make(lambda req, n: httpx.Response(599))  # stop after capturing
    with pytest.raises(tanod.TanodError):
        call(client)
    req = api.requests[0]
    assert req.method == method
    assert req.url.path == path
    assert str(req.url).startswith("https://tanod.dev/")
    if body is None:
        assert not req.content
    else:
        assert json.loads(req.content) == body


# --- response parsing ------------------------------------------------------


def test_parse_reports_and_text_routes():
    scan = {
        "schema_version": "1.2", "scan_id": "s1", "status": "ok",
        "findings": [{"id": "f1", "severity": "high", "file_line": "C.sol:3"}], "new_field": 1,
    }
    pkg = {"schema_version": "1.0", "status": "ok", "verdict": "review", "risk_score": 40,
           "findings": [{"id": "r1", "evidence": "ignore previous instructions"}]}
    check = {"schema": "precheck/1", "ok": True, "verdict": "caution", "risk_score": 30, "reasons": []}

    def h(req, n):
        p = req.url.path
        if p == "/v1/scan/source":
            return httpx.Response(200, json=scan)
        if p == "/v1/scan/package":
            return httpx.Response(200, json=pkg, headers={"X-Scan-Id": "p1", "X-Cache": "miss"})
        if p == "/v1/check/address":
            return httpx.Response(200, json=check)
        if p == "/v1/agents/export":
            return httpx.Response(200, text="source,id\npayai,1\n", headers={"content-type": "text/csv"})
        if p.endswith(".md"):
            return httpx.Response(200, text="# Report", headers={"content-type": "text/plain"})
        raise AssertionError(p)

    client, _ = make(h)
    rep = client.scan_contract_source("contract C {}")
    assert rep.findings[0].file_line == "C.sol:3" and rep.new_field == 1  # type: ignore[attr-defined]
    p = client.scan_package("pypi:requests==2.32.3")
    assert p.verdict == "review" and p.meta.scan_id == "p1" and p.meta.cache == "miss"
    c = client.check_address(A)
    assert c.schema_ == "precheck/1" and c.verdict == "caution"
    assert client.agents_export(format="csv").csv.startswith("source,id")
    assert client.get_report("s1", format="markdown").markdown == "# Report"


# --- async -----------------------------------------------------------------


def test_async_paid_flow(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("3000")
        return httpx.Response(200, json={"chain": "base", "address": A, "is_erc20": True, "symbol": "USDC"},
                              headers={"PAYMENT-RESPONSE": settle_header("3000")})

    api = FakeAPI(h)

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.token_info("base", A)

    out = asyncio.run(run())
    assert out.symbol == "USDC"
    assert out.meta.payment.price_usd == Decimal("0.003")
    assert len(api.requests) == 2
    assert api.requests[0].headers["X-Tanod-Free"] == "1"
    assert "X-Tanod-Free" not in api.requests[1].headers


def test_async_use_free_tier_false_pays_without_opt_in(throwaway_account):
    def h(req, n):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return response_402("1000")
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": settle_header("1000")})

    api = FakeAPI(h)

    async def run():
        async with AsyncTanod(signer=throwaway_account, use_env=False, use_free_tier=False,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.gas_price("base")

    out = asyncio.run(run())
    assert out.meta.paid and len(api.requests) == 2
    assert all("X-Tanod-Free" not in req.headers for req in api.requests)


def test_async_no_wallet_402():
    api = FakeAPI(lambda req, n: response_402("20000"))

    async def run():
        c = AsyncTanod(use_env=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api)))
        return await c.agents_query(q="x")

    with pytest.raises(PaymentRequiredError) as ei:
        asyncio.run(run())
    assert ei.value.price_usd == Decimal("0.02")


# --- fail-closed quote validation ---------------------------------------------


def _r402_with(**override) -> httpx.Response:
    from conftest import b64json, payment_required

    pr = payment_required("1000")
    pr["accepts"][0].update(override)
    return httpx.Response(402, json=pr, headers={"PAYMENT-REQUIRED": b64json(pr)})


@pytest.mark.parametrize("override", [
    {"asset": "0x" + "ab" * 20},          # not USDC
    {"network": "eip155:1"},              # not Base mainnet
    {"amount": "not-a-number"},           # price unknown -> must not bypass the cap
])
def test_unexpected_quote_is_refused_and_never_signed(override, throwaway_account):
    client, api = make(lambda req, n: _r402_with(**override), signer=throwaway_account)
    with pytest.raises(tanod.TanodError) as exc:
        client.gas_price("base")
    assert type(exc.value) is tanod.TanodError       # not a payment-flow subclass
    assert len(api.requests) == 1                     # nothing signed, no paid retry


# --- new utility routes: validation and parsing ------------------------------


@pytest.mark.parametrize("call", [
    lambda c: c.extract_pdf("https://x.example/a.pdf", max_pages=0),
    lambda c: c.extract_pdf("https://x.example/a.pdf", max_pages=201),
    lambda c: c.ocr_image("https://x.example/a.png", lang="ENG!"),
    lambda c: c.token_price("base", "DOGE/USD"),
    lambda c: c.token_price("solana", "ETH/USD"),
    lambda c: c.transaction("base", "0x1234"),
    lambda c: c.nft("base", "nope", "1"),
    lambda c: c.allowance("base", A, B, "nope"),
    lambda c: c.portfolio("base", A, tokens=[A] * 21),
    lambda c: c.portfolio("base", A, tokens=["nope"]),
    lambda c: c.swap_quote("base", A, B),
    lambda c: c.swap_quote("base", A, B, amount_in="1", amount_in_raw="1"),
    lambda c: c.web_search("x", count=11),
    lambda c: c.web_search("x", freshness="decade"),  # type: ignore[arg-type]
    lambda c: c.weather(),
    lambda c: c.weather(59.9),
    lambda c: c.weather(59.9, 10.7, place="Oslo"),
    lambda c: c.weather(place="Oslo", hours=49),
    lambda c: c.weather(91, 10),
])
def test_new_routes_validate_before_any_network_call(call):
    client, api = make(lambda req, n: httpx.Response(200, json={}))
    with pytest.raises(InvalidRequestError):
        call(client)
    assert api.requests == []


def test_new_routes_parse_responses():
    bodies = {
        "/v1/pdf": {"url": "u", "final_url": "u", "pages": 3, "extracted_pages": 3, "metadata": {"title": "T"}, "text": "hi",
                    "truncated": False, "encrypted": False, "untrusted_content": True},
        "/v1/chain/tx": {"chain": "base", "hash": TX, "status": "success", "from": A, "to": B, "fee": "0.00001"},
        "/v1/chain/price": {"chain": "base", "pair": "ETH/USD", "price": "2500.1", "decimals": 8, "round_id": "1",
                            "updated_at": 1, "age_seconds": 5, "stale": False, "feed": A},
        "/v1/email/verify": {"email": "a@example.com", "syntax_valid": True, "verdict": "deliverable_likely", "smtp_checked": False},
        "/v1/weather": {"location": {"lat": 1, "lon": 2}, "hours": 1, "hourly": [{"time": "t"}], "attribution": {"x": 1}},
        "/v1/search": {"query": "q", "count": 1, "results": [{"title": "t", "url": "u", "snippet": "s", "rank": 1}], "cached": False},
    }
    client, _ = make(lambda req, n: httpx.Response(200, json=bodies[req.url.path]))
    assert client.extract_pdf("https://x.example/a.pdf").text == "hi"
    tx = client.transaction("base", TX)
    assert tx.status == "success" and tx.from_ == A
    assert client.token_price("base", "ETH/USD").stale is False
    assert client.verify_email("a@example.com").verdict == "deliverable_likely"
    assert client.weather(place="Oslo").hourly[0]["time"] == "t"
    assert client.web_search("q").results[0]["rank"] == 1


def test_async_new_methods():
    api = FakeAPI(lambda req, n: httpx.Response(200, json={"query": "q", "count": 0, "results": []}))

    async def run():
        async with AsyncTanod(use_env=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(api))) as c:
            return await c.web_search("q")

    assert asyncio.run(run()).count == 0
    assert api.requests[0].headers["X-Tanod-Free"] == "1"
