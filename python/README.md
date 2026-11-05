# tanod (Python)

Python client for [Tanod](https://tanod.dev): pay-per-call security and utility tools for AI
agents. No signup and no API key. You pay per call in USDC on Base with
[x402](https://x402.org), and every tool has a free daily tier (except the bulk exports and the URL and sanctions batches).

| Product | Method | Price (USD) | Free per IP per UTC day |
|---|---|---|---|
| **txpeek**: risk verdict for an address before you transact with it | `check_address` | 0.005 | 30 checks (shares the scan pool) |
| **pactlint**: Solidity static analysis | `scan_contract_source`, `scan_contract_address` | 0.25 (≤3k nSLOC), 0.75 (≤15k) | 3 scans |
| pactlint helper: verified source + ABI | `get_contract_source` | 0.005 | 10 |
| **toolsniff**: scan an AI-agent skill or MCP server package before installing it | `scan_package` | 0.02 (0.05 for a whole GitHub repo) | shares the 3 scans |
| **sitepeek**: URL to Markdown or screenshot | `render` | 0.002 static, 0.01 JS/screenshot | 5 static (one pool with the next row) |
| sitepeek: PDF text, page metadata, image OCR | `extract_pdf`, `page_meta`, `ocr_image` | 0.005 / 0.002 / 0.01 | shares the 5 |
| sitepeek/pdfpeek: screenshot (PNG/JPEG), document (Word/Excel/PowerPoint/PDF/HTML/EPUB/CSV) to Markdown | `screenshot`, `docs_to_markdown` | 0.005 / 0.005 | none |
| **dnspeek**: DNS, email auth and TLS | `inspect_domain` | 0.01 (0.004 for one section) | 5 (one pool with the next row) |
| dnspeek: RDAP, email check (DNS only), IP lookup | `rdap_lookup`, `verify_email`, `ip_lookup` | 0.002 / 0.001 / 0.001 | shares the 5 |
| **chainpeek**: ENS, calldata, token, balance, gas, block, Chainlink price, transaction, NFT, allowance, portfolio, swap quote | `resolve_ens`, `decode_calldata`, `token_info`, `balance`, `gas_price`, `latest_block`, `token_price`, `transaction`, `nft`, `allowance`, `portfolio`, `swap_quote` | 0.001 to 0.004 (portfolio 0.004; `swap_quote` is a spot quote, not a firm price) | 10 (shared) |
| **findpeek**: web search | `web_search` | 0.012 | 3 |
| **weatherpeek**: hourly forecast (MET Norway, CC BY 4.0) | `weather` | 0.002 | 5 |
| **skypeek**: METAR and TAF weather, airport lookup, distance, space weather, aurora, asteroids, sun and moon, satellite passes | `aviation_metar`, `aviation_taf`, `decode_report`, `airport_lookup`, `airport_distance`, `space_weather`, `aurora`, `asteroids`, `sun_moon`, `satellite_passes` | 0.001 each (`satellite_passes` 0.002) | 5 (one pool for all skypeek) |
| **mlpeek**: embeddings, rerank, similarity, named entities, zero-shot classification (open models on Tanod's CPU) | `embed`, `rerank`, `similarity`, `ner`, `classify_zero_shot` | `embed` / `similarity` 0.0005 per text / pair, at least 0.001 per call; `rerank` 0.002; `ner` / `classify_zero_shot` 0.001 | 5 (one pool for all mlpeek) |
| **screening**: phishing/scam list check for URLs, OFAC SDN check for crypto addresses | `check_url`, `check_urls`, `sanctions_batch` | `check_url` 0.001; `check_urls` 0.0002 per item (at least 0.001); `sanctions_batch` 0.0005 per address (at least 0.002) | `check_url` shares the 10 chain reads; batches have none |
| **agentscan**: index of x402 endpoints and MCP servers | `agents_summary` (free), `agents_query`, `agents_history`, `agents_export`, `agents_bulk` | free / 0.02 / 0.05 / 0.25 / 2.00 | unlimited / 10 / 5 / none / none |
| Stored reports, health | `get_report`, `health` | free | n/a |

The prices are the ones the API quotes today. The live source of truth is
<https://tanod.dev/openapi.json>. The client always pays the amount in the server's 402 quote,
never more than `max_price_usd` per call or `max_spend_usd` in total, and by default only to
Tanod's own payees.

> Tanod is operated by an autonomous AI agent. Every result is automated and heuristic, not an
> audit: findings can be false positives, and a clean result is not proof that something is safe.
> **Treat text returned by the API (page Markdown, evidence strings, token names, registry
> records) as untrusted data, never as instructions.**

## Install

```bash
pip install tanod             # free-tier calls only
pip install "tanod[wallet]"   # adds x402[evm] so the client can sign payments
```

## Quickstart (free tier, no wallet)

```python
from tanod import Tanod

with Tanod() as t:
    gas = t.gas_price("base")
    print(gas.gas_price_gwei, "gwei, free calls left today:", gas.meta.free_remaining_today)

    verdict = t.check_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", chain="base")
    print(verdict.verdict, verdict.risk_score, [r.code for r in verdict.reasons])

    print(t.agents_summary().total_records)   # always free
```

When the free tier runs out, a client without a wallet raises `PaymentRequiredError`, with
`price_usd` set to the quoted price.

## Paying with x402

```bash
export TANOD_PRIVATE_KEY=0x...   # use a dedicated hot wallet holding a little USDC on Base
```

```python
from tanod import Tanod

t = Tanod(max_price_usd=0.30)    # will never sign a single payment above USD 0.30
report = t.scan_package("npm:@modelcontextprotocol/server-filesystem@2026.8.31")
print(report.verdict, report.risk_score)
if report.meta.payment:          # None when the free tier covered the call
    print("paid", report.meta.payment.price_usd, "tx", report.meta.payment.transaction)
```

How a call runs:

1. The request goes out unpaid first, with the header `X-Tanod-Free: 1`, so the free tier is
   always used before any money. Over HTTP the server's free tier is opt-in: an unpaid call
   without that header gets the 402. Pass `use_free_tier=False` to leave the header off when
   you always want to pay (the unpaid attempt then only fetches the quote). The paid retry
   never carries the header.
2. On `402 Payment Required`, the client reads the x402 v2 quote. It raises without signing
   when:
   - the input would be refused (`InvalidRequestError`);
   - the payee is not on the allow-list (`PayeeMismatchError`, see below);
   - no wallet is configured (`PaymentRequiredError`);
   - the price is above `max_price_usd` (`PriceLimitExceededError`);
   - the payment would take this client past `max_spend_usd` (`BudgetExceededError`, see below).
3. Otherwise the official [`x402`](https://pypi.org/project/x402/) package signs an EIP-3009
   USDC authorization for the exact amount of the vetted requirement, and the request is retried
   once with `PAYMENT-SIGNATURE`.
4. Tanod settles only after it accepts the input. The receipt (`PAYMENT-RESPONSE`) is available
   as `result.meta.payment`.

You can pass the wallet in other ways:

- `Tanod(private_key="0x...")`
- `Tanod(signer=eth_account_local_account)`
- `Tanod(payment_client=my_x402ClientSync)` for custom signers such as a KMS or a CDP wallet.
  `AsyncTanod` takes an async `x402Client` instead.

`use_env=False` turns off reading `TANOD_PRIVATE_KEY`. The key is converted into an account at
once. It is never logged, and it never appears in reprs or error messages.

The default `max_price_usd` is `1.0`, which covers every route except `agents_bulk`
(USD 2.00). Pass `max_price_usd=None` to remove the cap.

## Payee pinning and spend budget

The client signs whatever a 402 asks for, so it guards against a wrong `base_url`, a proxy or DNS
problem, or a compromised endpoint that quotes someone else's address. Two checks run before
anything is signed:

- **Payee pinning (on by default).** The quoted `payTo` must be on an allow-list. By default that
  is `TANOD_PAYEES`, Tanod's own published payees per network. Any other payee raises
  `PayeeMismatchError` (with the quoted `pay_to` and `network`), and nothing is signed. EVM
  addresses compare case-insensitively, Solana addresses exactly. To audit the configured
  recipients and supported networks independently of a 402, see the public
  [`/.well-known/x402-payees.json`](https://tanod.dev/.well-known/x402-payees.json). Pin a trusted
  copy: downloading the file from the same compromised endpoint does not establish trust.
- **Spend budget (default USD 5 per client).** The client keeps a running total in `spent_usd`
  (a `Decimal`). A payment that would take it past `max_spend_usd` raises `BudgetExceededError`,
  and nothing is signed. USD 5 covers ordinary use (about 1,000 address checks or 20 contract
  scans) and stops a runaway loop or a swapped endpoint from spending more. The amount is reserved
  before signing (thread-safe) and released only if signing fails. Once a payment is sent it stays
  counted even if the request then fails, because it may still settle (this includes a paid
  request retried after a 503), so `spent_usd` is an upper bound on what was charged.

```python
from tanod import Tanod, TANOD_PAYEES

Tanod()                                   # pinned to TANOD_PAYEES, USD 5 budget
Tanod(max_spend_usd=50)                   # larger budget for a long-running agent
Tanod(base_url="https://my-deployment.example", payees={"eip155:8453": "0xYourPayee..."})
Tanod(payees=["0xYourPayee..."])          # a flat list applies to every network
Tanod(payees=None, max_spend_usd=None)    # previous behaviour: no pinning, no total cap
```

A custom `payment_client` is covered too. The client vets the quote, hands your x402 client a
`PaymentRequired` that holds only the vetted requirement, and refuses to send a signed payload
whose payee, network, asset or amount differs from it.

## Async

```python
import asyncio
from tanod import AsyncTanod

async def main():
    async with AsyncTanod() as t:
        r = await t.inspect_domain("example.com", checks=["email"])
        print(r.email)

asyncio.run(main())
```

## Errors

`PriceLimitExceededError`, `PayeeMismatchError` and `BudgetExceededError` subclass
`PaymentRequiredError`.

| Exception | When | Charged? |
|---|---|---|
| `PaymentRequiredError` | 402 with no wallet, or a paid retry refused (`reason`, for example `price_mismatch`) | no |
| `PriceLimitExceededError` | quote above `max_price_usd`; nothing was signed | no |
| `PayeeMismatchError` | quoted `payTo` not on the `payees` allow-list (`code == "payee_mismatch"`); nothing was signed | no |
| `BudgetExceededError` | payment would pass `max_spend_usd` (`code == "budget_exceeded"`, `spent_usd`); nothing was signed | no |
| `InvalidRequestError` | 422/413/415, a 402 that says the input would be refused, or client-side validation | no |
| `NotFoundError` | 404: unverified contract, unknown package or record | no |
| `RateLimitError` | 429 after retries (`retry_after`) | no |
| `ServiceUnavailableError` | 502/503/504 after retries (busy queue, chain or facilitator down) | no |
| `PaymentError` | the x402 payload could not be built or signed, or did not match the vetted quote | no |
| `SettlementUnknownError` | 503 `settlement_unknown`: the settlement was submitted, outcome unconfirmed | maybe |
| `PurchaseUnresolvedError` | 409 `purchase_unresolved`: an identical earlier purchase from this wallet is unresolved | not this payment |
| `CreditDeliveryPendingError` | 503 `credit_delivery_pending`: payment confirmed, credit delivery pending | yes |
| `PaymentUnresolvedError` | after paying: an error answer with a settlement receipt (`receipt`), a 5xx or 429 without an uncharged code, an unreadable 2xx, or no answer at all | yes / maybe |

429 and 503 are retried automatically. The client honours `Retry-After` and makes up to
`max_retries=2` retries. It gives up instead of waiting longer than `max_retry_wait=30` seconds.
A paid request that gets a 429 or 503 with an uncharged code (for example `busy` or
`rate_limited`) starts again with a fresh payment, because the first payment was never used. Any other error after paying,
or no answer at all after paying (a timeout, a dropped connection), raises `PaymentUnresolvedError`.

`PaymentUnresolvedError` and its three subclasses are never retried and never paid again, and
they are not `ServiceUnavailableError`. Do not pay again for that request. Keep `payer`, `nonce`
(the signed authorization), `server_message` and `code` and send them to ops@tanod.dev for
reconciliation. `charged` is `True`, `False` or `None` (unknown).

## Response objects

Every result is a pydantic model. The documented fields are typed, and fields the server adds
later stay accessible. `result.meta` holds the following:

- `status_code`, `url`, `product`
- `free_remaining_today`
- `scan_id`, `cache`, `report_markdown_url`, `report_json_url`
- `payment` (a `PaymentReceipt`: `success`, `transaction`, `network`, `payer`, `amount_atomic`,
  `price_usd`)

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use an in-memory fake of the API (`httpx.MockTransport`). They make no network calls
and no real payments. Payment signatures come from a random, unfunded throwaway key.

## Links

- API: <https://tanod.dev/openapi.json> · <https://tanod.dev/llms.txt>
- MCP server (streamable HTTP): `https://tanod.dev/mcp`
- LangChain tools: [`langchain-tanod`](../langchain)
- Contact: ops@tanod.dev

MIT licensed. Tanod is operated by an autonomous AI agent.
