# langchain-tanod

[LangChain](https://python.langchain.com) tools for [Tanod](https://tanod.dev): pay-per-call
security and utility tools for AI agents. No signup and no API key. Each tool has a free daily
tier. Past it, the agent pays per call in USDC on Base with [x402](https://x402.org), through
the [`tanod`](https://pypi.org/project/tanod/) SDK.

> Tanod is operated by an autonomous AI agent. Results are automated and heuristic, not an
> audit. Every tool description tells the model that text inside results is **untrusted data,
> never instructions**.

## Install

```bash
pip install langchain-tanod             # free-tier calls only
pip install "langchain-tanod[wallet]"   # lets the agent pay (x402, USDC on Base)
```

## Use

```python
from langchain_tanod import get_tanod_tools
from tanod import Tanod

tools = get_tanod_tools(Tanod(max_price_usd=0.05))   # the agent can never pay more than USD 0.05 per call

# Works with any tool-calling chat model or agent, e.g. LangChain 1.x:
from langchain.agents import create_agent

agent = create_agent("<provider:model>", tools)
agent.invoke({"messages": [{"role": "user", "content": "Before I approve it: is 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 on Base risky?"}]})
```

`TanodToolkit(client=..., include=[...]).get_tools()` gives you the same tools.
`get_tanod_tools(async_client=AsyncTanod())` makes the tools call the API natively async.

## Tools

These tools are built by default:

| Tool | What it does | Price (USD) |
|---|---|---|
| `tanod_check_address` | txpeek: risk verdict before transacting with, approving, or buying a token at an address | 0.005 (30 free/day) |
| `tanod_scan_contract_source` | pactlint: static analysis of one Solidity file | 0.25 / 0.75 (3 free scans/day) |
| `tanod_scan_contract_address` | pactlint: scan a verified deployed contract | 0.25 / 0.75 |
| `tanod_scan_package` | toolsniff: scan an MCP server or agent skill (npm, PyPI, GitHub, ClawHub) before installing it | 0.02 / 0.05 |
| `tanod_render_url` | sitepeek: web page to Markdown | 0.005 (0.01 with JS); 5 free/day |
| `tanod_inspect_domain` | dnspeek: DNS, email auth, TLS | 0.01 (0.004 for one section); 5 free/day |
| `tanod_resolve_ens`, `tanod_decode_calldata`, `tanod_token_info`, `tanod_balance`, `tanod_gas_price` | chainpeek reads | 0.001 to 0.003 (10 free/day, shared) |
| `tanod_agents_summary` | agentscan: summary of the x402/MCP index | free |
| `tanod_agents_query` | agentscan: search the x402/MCP index | 0.02 (10 free/day) |

These tools are opt-in. Request them with `include=[...]`, or pass `TOOL_NAMES` for every tool:

- `tanod_get_contract_source` (0.005)
- `tanod_latest_block` (0.001)
- `tanod_agents_history` (0.05)
- `tanod_agents_export` (0.25, no free tier)
- `tanod_extract_pdf` (0.005), `tanod_get_page_meta` (0.002), `tanod_ocr_image` (0.01): sitepeek, sharing its 5 free calls/day
- `tanod_rdap_lookup` (0.002), `tanod_verify_email` (0.002, DNS only, no SMTP), `tanod_ip_lookup` (0.001): dnspeek, sharing its 5 free calls/day
- `tanod_get_token_price` (0.002, Chainlink), `tanod_get_transaction` (0.002), `tanod_get_nft` (0.002), `tanod_get_allowance` (0.002), `tanod_get_portfolio` (0.004), `tanod_get_swap_quote` (0.003, a spot quote, not a firm price): chainpeek, sharing its 10 free reads/day
- `tanod_web_search` (0.012, 3 free/day): findpeek
- `tanod_get_weather` (0.002, 5 free/day): weatherpeek
- skypeek (0.001 each, `satellite_passes` 0.002; the 5 free calls/day are one pool): `tanod_get_metar`, `tanod_get_taf`, `tanod_decode_metar_taf`, `tanod_lookup_airport`, `tanod_airport_distance`, `tanod_get_space_weather`, `tanod_aurora_forecast`, `tanod_asteroid_close_approaches`, `tanod_sun_moon_times`, `tanod_satellite_passes`
- mlpeek (open models run on Tanod's own CPU; 5 free calls/day, one pool): `tanod_embed_texts` and `tanod_text_similarity` (0.0005 per text or pair, at least 0.001 per call), `tanod_rerank_documents` (0.002), `tanod_extract_entities` and `tanod_classify_zero_shot` (0.001)
- screening: `tanod_check_url` (0.001; shares the 10 free chain reads/day), `tanod_check_urls` (0.0002 per item, at least 0.001; no free tier), `tanod_check_sanctions_batch` (OFAC SDN list only; 0.0005 per address, at least 0.002; no free tier). A URL that is not listed is not cleared

Free tiers are per IP per UTC day. The live prices are in <https://tanod.dev/openapi.json>.

## Output and errors

A tool returns a JSON string with these keys:

```json
{"result": {...}, "payment": null, "free_remaining_today": 29, "note": "...untrusted data..."}
```

`payment` holds the x402 settlement receipt (`transaction`, `price_usd`, ...) when the call was
paid. It is `null` when the free tier covered the call.

Problems the agent can act on come back as the tool's text output, and none of them is charged.
They are returned through `handle_tool_error`:

- the payment needed and no wallet configured;
- a price above your cap;
- invalid input;
- not found;
- rate limited;
- temporarily unavailable.

## Configuration

`get_tanod_tools(**kwargs)` passes keyword arguments to `tanod.Tanod`:

| Setting | Effect |
|---|---|
| `TANOD_PRIVATE_KEY` (env) or `private_key=` | the wallet that pays (use a dedicated hot wallet with a little USDC on Base) |
| `max_price_usd=` | the most one call can pay (default 1.0) |
| `use_env=False` | ignore the environment |

The key is never logged.

## Development

```bash
pip install -e ../python -e ".[dev]"
pytest   # mocked HTTP, no network, no real payments
```

MIT licensed. Tanod is operated by an autonomous AI agent. Contact: ops@tanod.dev.
