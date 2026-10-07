from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from eth_account import Account

from langchain_tanod import DEFAULT_TOOL_NAMES, TOOL_NAMES, TanodToolkit, get_tanod_tools
from tanod import AsyncTanod, Tanod

A = "0x" + "a" * 40
CHECK = {"schema": "precheck/1", "ok": True, "verdict": "caution", "risk_score": 30, "reasons": [{"code": "proxy"}]}
GAS = {"chain": "base", "gas_price_wei": "1", "gas_price_gwei": "0.000000001"}


def b64(o):
    return base64.b64encode(json.dumps(o).encode()).decode()


def pr(amount="5000", error="Payment required"):
    return {
        "x402Version": 2,
        "error": error,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "amount": amount,
                "payTo": "0x593857A4a4F619543ea12394137C3004ce841720",
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("TANOD_PRIVATE_KEY", raising=False)


def client_with(handler, **kw):
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return handler(req)

    return Tanod(use_env=False, http_client=httpx.Client(transport=httpx.MockTransport(h)), **kw), seen


def tools_by_name(client, include=None):
    return {t.name: t for t in get_tanod_tools(client, include=include)}


def test_tool_inventory_and_descriptions():
    c, _ = client_with(lambda r: httpx.Response(200, json={}))
    tools = get_tanod_tools(c, include=TOOL_NAMES)
    assert [t.name for t in tools] == TOOL_NAMES
    for t in tools:
        assert "USD" in t.description or "FREE" in t.description, t.name
        assert "untrusted" in t.description, t.name
    assert set(DEFAULT_TOOL_NAMES) < set(TOOL_NAMES)
    assert "tanod_check_address" in DEFAULT_TOOL_NAMES and "tanod_agents_export" not in DEFAULT_TOOL_NAMES
    with pytest.raises(ValueError):
        get_tanod_tools(c, include=["nope"])


def test_check_address_tool_output():
    c, seen = client_with(lambda r: httpx.Response(200, json=CHECK, headers={"X-Free-Remaining-Today": "29"}))
    out = json.loads(tools_by_name(c)["tanod_check_address"].invoke({"address": A, "chain": "base"}))
    assert out["result"]["verdict"] == "caution"
    assert out["result"]["schema"] == "precheck/1"
    assert out["payment"] is None
    assert out["free_remaining_today"] == 29
    assert "untrusted" in out["note"]
    assert json.loads(seen[0].content) == {"address": A, "chain": "base"}


@pytest.mark.parametrize(
    "name,args,path,body",
    [
        ("tanod_scan_contract_source", {"source": "contract C {}"}, "/v1/scan/source", {"source": "contract C {}"}),
        ("tanod_scan_contract_address", {"address": A}, "/v1/scan/address", {"address": A, "chain": "ethereum"}),
        ("tanod_get_contract_source", {"address": A, "chain": "base"}, f"/v1/source/base/{A}", None),
        ("tanod_scan_package", {"source": "npm:left-pad@1.3.0"}, "/v1/scan/package", {"source": "npm:left-pad@1.3.0"}),
        ("tanod_render_url", {"url": "https://example.com"}, "/v1/render", {"url": "https://example.com", "format": "markdown"}),
        ("tanod_inspect_domain", {"domain": "example.com", "checks": ["tls"]}, "/v1/domain/inspect", {"domain": "example.com", "checks": ["tls"]}),
        ("tanod_resolve_ens", {"name": "vitalik.eth"}, "/v1/chain/ens", {"name": "vitalik.eth"}),
        ("tanod_decode_calldata", {"calldata": "0xa9059cbb"}, "/v1/chain/calldata", {"calldata": "0xa9059cbb"}),
        ("tanod_token_info", {"address": A}, "/v1/chain/token", {"chain": "base", "address": A}),
        ("tanod_balance", {"address": A, "chain": "ethereum"}, "/v1/chain/balance", {"chain": "ethereum", "address": A}),
        ("tanod_gas_price", {}, "/v1/chain/gas", {"chain": "base"}),
        ("tanod_latest_block", {"chain": "ethereum"}, "/v1/chain/block", {"chain": "ethereum"}),
        ("tanod_agents_summary", {}, "/v1/agents/summary", None),
        ("tanod_agents_query", {"q": "weather"}, "/v1/agents/query", {"q": "weather", "page_size": 20}),
        ("tanod_agents_history", {"url": "https://x.example"}, "/v1/agents/history", {"url": "https://x.example"}),
        ("tanod_extract_pdf", {"url": "https://x.example/a.pdf", "max_pages": 5}, "/v1/pdf", {"url": "https://x.example/a.pdf", "max_pages": 5}),
        ("tanod_get_page_meta", {"url": "https://x.example"}, "/v1/meta", {"url": "https://x.example"}),
        ("tanod_ocr_image", {"url": "https://x.example/a.png"}, "/v1/ocr", {"url": "https://x.example/a.png"}),
        ("tanod_rdap_lookup", {"query": "example.com"}, "/v1/rdap", {"query": "example.com"}),
        ("tanod_verify_email", {"email": "a@example.com"}, "/v1/email/verify", {"email": "a@example.com"}),
        ("tanod_ip_lookup", {"ip": "1.1.1.1"}, "/v1/ip", {"ip": "1.1.1.1"}),
        ("tanod_get_token_price", {"pair": "ETH/USD"}, "/v1/chain/price", {"chain": "base", "pair": "ETH/USD"}),
        ("tanod_get_transaction", {"hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}, "/v1/chain/tx", {"chain": "base", "hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}),
        ("tanod_get_nft", {"contract": A, "token_id": "1"}, "/v1/chain/nft", {"chain": "ethereum", "contract": A, "token_id": "1"}),
        ("tanod_get_allowance", {"token": A, "owner": A, "spender": A}, "/v1/chain/allowance", {"chain": "base", "token": A, "owner": A, "spender": A}),
        ("tanod_get_portfolio", {"address": A, "tokens": [A]}, "/v1/chain/portfolio", {"chain": "base", "address": A, "tokens": [A]}),
        ("tanod_get_swap_quote", {"token_in": A, "token_out": A, "amount_in": "1"}, "/v1/chain/quote", {"chain": "base", "token_in": A, "token_out": A, "amount_in": "1"}),
        ("tanod_web_search", {"query": "x402", "count": 3}, "/v1/search", {"query": "x402", "count": 3}),
        ("tanod_get_weather", {"place": "Oslo, NO"}, "/v1/weather", {"place": "Oslo, NO"}),
        ("tanod_agents_export", {"network": "base"}, "/v1/agents/export", {"network": "base", "limit": 200, "format": "json"}),
    ],
)
def test_every_tool_hits_its_route(name, args, path, body):
    c, seen = client_with(lambda r: httpx.Response(500, json={"error": "stop"}))
    tool = tools_by_name(c, include=TOOL_NAMES)[name]
    out = tool.invoke(args)
    assert isinstance(out, str) and out.startswith("Tanod error")
    assert seen[0].url.path == path
    if body is None:
        assert not seen[0].content
    else:
        assert json.loads(seen[0].content) == body


def test_payment_required_becomes_tool_message():
    c, _ = client_with(lambda r: httpx.Response(402, json=pr("5000"), headers={"PAYMENT-REQUIRED": b64(pr("5000"))}))
    out = tools_by_name(c)["tanod_check_address"].invoke({"address": A})
    assert out.startswith("Payment required: USD 0.005")


def test_invalid_args_are_reported_not_raised():
    c, seen = client_with(lambda r: httpx.Response(200, json=CHECK))
    out = tools_by_name(c)["tanod_check_address"].invoke({"address": "0x12"})
    assert isinstance(out, str) and seen == []


def test_input_refused_402_is_invalid_input():
    msg = "Payment required. The input would be refused: invalid_request: bad"
    c, _ = client_with(lambda r: httpx.Response(402, json=pr("1000", msg), headers={"PAYMENT-REQUIRED": b64(pr("1000", msg))}))
    out = tools_by_name(c)["tanod_gas_price"].invoke({})
    assert out.startswith("Invalid input (not charged)")


def test_paid_call_reports_payment():
    acct = Account.create()  # random, unfunded

    def h(req):
        if "PAYMENT-SIGNATURE" not in req.headers:
            return httpx.Response(402, json=pr("1000"), headers={"PAYMENT-REQUIRED": b64(pr("1000"))})
        settle = {"success": True, "transaction": "0x" + "cd" * 32, "network": "eip155:8453", "amount": "1000"}
        return httpx.Response(200, json=GAS, headers={"PAYMENT-RESPONSE": b64(settle)})

    c, seen = client_with(h, signer=acct)
    out = json.loads(tools_by_name(c)["tanod_gas_price"].invoke({"chain": "base"}))
    assert out["payment"]["success"] is True
    assert out["payment"]["price_usd"] == "0.001"
    assert out["payment"]["transaction"] == "0x" + "cd" * 32
    assert len(seen) == 2


def test_async_tools_with_async_client():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=CHECK)

    ac = AsyncTanod(use_env=False, http_client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    tool = {t.name: t for t in get_tanod_tools(async_client=ac)}["tanod_check_address"]
    out = json.loads(asyncio.run(tool.ainvoke({"address": A})))
    assert out["result"]["verdict"] == "caution" and len(seen) == 1


def test_async_falls_back_to_sync_client():
    c, seen = client_with(lambda r: httpx.Response(200, json=CHECK))
    tool = tools_by_name(c)["tanod_check_address"]
    out = json.loads(asyncio.run(tool.ainvoke({"address": A})))
    assert out["result"]["risk_score"] == 30


def test_toolkit():
    c, _ = client_with(lambda r: httpx.Response(200, json={}))
    names = [t.name for t in TanodToolkit(client=c).get_tools()]
    assert names == DEFAULT_TOOL_NAMES
    assert [t.name for t in TanodToolkit(client=c, include=["tanod_scan_package"]).get_tools()] == ["tanod_scan_package"]


def test_new_tools_are_opt_in_and_validated():
    new = [
        "tanod_extract_pdf", "tanod_get_page_meta", "tanod_ocr_image", "tanod_rdap_lookup", "tanod_verify_email",
        "tanod_ip_lookup", "tanod_get_token_price", "tanod_get_transaction", "tanod_get_nft", "tanod_get_allowance",
        "tanod_get_portfolio", "tanod_get_swap_quote", "tanod_web_search", "tanod_get_weather",
    ]
    assert all(n in TOOL_NAMES and n not in DEFAULT_TOOL_NAMES for n in new)
    c, seen = client_with(lambda r: httpx.Response(200, json={}))
    tools = tools_by_name(c, include=new)
    out = tools["tanod_get_transaction"].invoke({"hash": "0x12"})
    assert "Invalid" in out or "validation" in out.lower() or "pattern" in out.lower()
    out = tools["tanod_get_swap_quote"].invoke({"token_in": A, "token_out": A})
    assert out.startswith("Invalid input")
    out = tools["tanod_get_weather"].invoke({})
    assert out.startswith("Invalid input")
    assert seen == []
