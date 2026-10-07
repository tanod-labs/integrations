# Tanod integrations

Open-source clients and agent-framework tools for [Tanod](https://tanod.dev). Tanod is a set of
pay-per-call security and utility tools for AI agents. There is no signup and no API key. Calls
are paid per call in USDC on Base with [x402](https://x402.org), and each tool has a free daily
tier.

Tanod is **operated by an autonomous AI agent**. Every result is automated and heuristic, not an
audit. Text returned by the API is untrusted data, never instructions. Everything here is MIT
licensed.

| Package | Path | Registry name | What it is |
|---|---|---|---|
| Python SDK | [`python/`](python) | PyPI `tanod` | Typed sync and async client, one method per route. It handles x402 with the official [`x402`](https://pypi.org/project/x402/) package. Payments need the `tanod[wallet]` extra. |
| TypeScript SDK | [`typescript/`](typescript) | npm `@tanod/sdk` | The same surface, built on the official [`@x402/fetch`](https://www.npmjs.com/package/@x402/fetch) and [`@x402/evm`](https://www.npmjs.com/package/@x402/evm) packages (2.28.x). |
| LangChain tools | [`langchain/`](langchain) | PyPI `langchain-tanod` | `StructuredTool` per route, plus `TanodToolkit` and `get_tanod_tools()`. |
| Vercel AI SDK tools | [`ai-sdk/`](ai-sdk) | npm `@tanod/ai-sdk` | `tool()` definitions with zod schemas. Works with `ai` 5, 6 and 7. |
| MCP client configs | [`mcp-configs/`](mcp-configs) | none | Configs for Claude Code, Claude Desktop, Cursor, Windsurf, VS Code and a generic client, all pointing at `https://tanod.dev/mcp`. |
| n8n design note | [`n8n/DESIGN.md`](n8n/DESIGN.md) | none | Plan for a community node. Not built yet. |

The routes, prices and free tiers come from `https://tanod.dev/openapi.json`, `/llms.txt` and
`/.well-known/x402`, fetched on 2026-10-07.

## Shared behaviour across the SDKs and tools

1. **The free tier comes first.** Each call goes out unpaid, with the header `X-Tanod-Free: 1`
   (the server's free tier is opt-in over HTTP; without the header an unpaid call gets the 402).
   When the free daily tier covers it, the server answers 200 and nothing is signed. Turn this
   off with `use_free_tier=False` (Python) or `useFreeTier: false` (TypeScript) to always pay.
   The paid retry never carries the header.
2. **On `402`, the client reads the x402 v2 quote** (`PAYMENT-REQUIRED`) and raises without
   signing anything when:
   - the quote says the input would be refused (`InvalidRequestError`);
   - no wallet is configured (`PaymentRequiredError`, with `price_usd`);
   - the price is above the per-call cap (`PriceLimitExceededError`; the default cap is USD 1.00).
3. **Otherwise it signs and retries once.** The official x402 client signs an EIP-3009 USDC
   authorization for the exact quoted amount, and the request is retried with
   `PAYMENT-SIGNATURE`. The settlement receipt (`PAYMENT-RESPONSE`) is returned as
   `meta.payment`.
4. **429 and 503 are retried, never charged.** The client honours `Retry-After`. A paid request
   that gets a 503 starts again with a fresh payment.
5. **Keys stay private.** `TANOD_PRIVATE_KEY`, or a key or signer passed in, is never logged,
   printed or included in errors. Tests check this.
6. **Tool descriptions carry price and warning.** Every LangChain and AI SDK tool description
   states its price and says that results are untrusted data.

## Tests

Every test suite runs against mocked HTTP. They make no network calls and no payments.
Signatures come from random, unfunded throwaway keys.

| Suite | Command (from this directory) | Result on 2026-10-07 |
|---|---|---|
| Python SDK | `pip install -e "./python[dev]" && pytest -q python/tests` | **88 passed** |
| LangChain | `pip install -e "./langchain[dev]" && pytest -q langchain/tests` | **40 passed** |
| TypeScript SDK | `npm ci && npm test` (builds `@tanod/sdk`, then runs vitest) | **85 passed** |
| AI SDK tools | included in `npm test`; includes an end-to-end `generateText` run with `MockLanguageModelV4` | **35 passed** |
| Typecheck | `npm run typecheck` | clean |
| Packaging | `python -m build` + `twine check` (both PyPI packages); `npm pack --dry-run` (both npm packages) | passed |

CI is in `.github/workflows/ci.yml`. It runs Python 3.10 to 3.13 and Node 24.

## Live verification (free tier only, no payments)

These checks ran against `https://tanod.dev` from the build VM with no wallet configured:

- **Python SDK**
  - `health()` returned ok.
  - `agents_summary()` returned 78,149 records (a free route).
  - `gas_price("base")` returned 0.006 gwei, with `X-Free-Remaining-Today` decoded and the
    product `chainpeek`.
  - `inspect_domain("example.com", checks=["dns"])` returned DNS sections.
  - `agents_export()`, which has no free tier, raised `PaymentRequiredError` with
    `price_usd=0.25`, network `eip155:8453` and the correct payTo.
  - An invalid input got a 402 that says the input would be refused, which raised
    `InvalidRequestError` without signing.
- **TypeScript SDK**
  - `health`, `agentsSummary` and `gasPrice("ethereum")` succeeded.
  - `agentsExport` raised `PaymentRequiredError` (0.25).
  - An invalid input raised `InvalidRequestError`.
- **LangChain**
  - Every tool schema converts to the OpenAI tool format (17 tools).
  - `tanod_agents_summary` and `tanod_gas_price` returned results.
  - `tanod_agents_export` returned a "Payment required: USD 0.25" message to the agent.
- **AI SDK**
  - `tanod_agents_summary` and `tanod_gas_price` returned `ok:true`.
  - An oversized query returned `ok:false`, `invalid_input`.
- **MCP**
  - `initialize` and `tools/list` on `https://tanod.dev/mcp` returned 30 tools.
- **x402 signing**
  - A live 402 header from `/v1/chain/gas` was decoded and signed **offline** with the official
    `x402` Python package and a throwaway key. It was never sent.
  - The spend controls refused a USD 2 quote under a USD 0.50 cap.

## Publish steps (maintainer accounts needed; nothing has been published)

### 0. GitHub (org `tanod-labs`)

Create the public repo `tanod-labs/integrations` with this directory as its root, keeping the
history:

```bash
# from the root of the repository that contains oss/integrations
git subtree split --prefix oss/integrations -b integrations-export
git push git@github.com:tanod-labs/integrations.git integrations-export:main
```

The URLs in `pyproject.toml` and `package.json` already point to
`https://github.com/tanod-labs/integrations`. Before you push, check that nothing outside this
directory is referenced. The packages contain no personal identity, and the only contact is
`ops@tanod.dev`.

### 1. PyPI (`tanod` first, then `langchain-tanod`, which depends on it)

The preferred route is trusted publishing, which needs no token:

1. On pypi.org, under *Publishing → Add a pending publisher*, add one entry for `tanod` and one
   for `langchain-tanod`. Use owner `tanod-labs`, repo `integrations`, workflow
   `publish-pypi.yml` and environment `pypi`.
2. In GitHub, go to Actions → **publish-pypi** → Run. Choose `python` first, then `langchain`.

The manual fallback, with a PyPI API token:

```bash
cd oss/integrations
python -m build python --outdir dist-py && python -m build langchain --outdir dist-py
twine check dist-py/* && twine upload dist-py/tanod-* && twine upload dist-py/langchain_tanod-*
```

### 2. npm (`@tanod/sdk` first, then `@tanod/ai-sdk`)

1. Create the npm **org `tanod`**. It owns the `@tanod` scope; the name was unclaimed on
   2026-10-07. If the org cannot be had, rename to `tanod-sdk` and `tanod-ai-sdk` in both
   `package.json` files and in the ai-sdk dependency.
2. Under each package's settings on npmjs.com, add a trusted publisher: GitHub Actions, org
   `tanod-labs`, repo `integrations`, workflow `publish-npm.yml`. A package that does not exist
   yet can be bootstrapped with one manual
   `npm publish -w @tanod/sdk --access public` using a granular token, then switched to trusted
   publishing.
3. In GitHub, go to Actions → **publish-npm** → Run with `@tanod/sdk`, then with `@tanod/ai-sdk`.
   The workflow publishes with `--provenance`.

### 3. After publishing

- List the packages on tanod.dev (`/llms.txt` and the site).
- Submit to these directories:
  - the LangChain integrations docs (a PR to `langchain-ai/docs`, Python integrations → tools);
  - the AI SDK community tools page (a PR to `vercel/ai`, `content/tools-registry`);
  - MCP directories (the server is already public).
- Bump versions in `python/src/tanod/_version.py`, `langchain/pyproject.toml` and the
  `package.json` files for every release.

## Versions used (checked to exist on 2026-10-07)

- **Python:**
  - `x402` 2.25.0 (`x402-foundation/x402`)
  - `httpx` 0.28.1
  - `pydantic` 2.13
  - `langchain-core` 1.6.7
  - `eth-account` 0.14 and `web3` 8.0 (pulled in by `x402[evm]`)
- **npm:**
  - `@x402/fetch`, `@x402/evm` and `@x402/core` 2.28.0 (`github.com/x402-foundation/x402`)
  - `viem` 2.57.3
  - `ai` 7.0.130
  - `zod` 4.6.5
  - `typescript` 6.0.3
  - `vitest` 5.0.3
- **MCP bridge:** `mcp-remote` 0.14.3 (`punkpeye/mcp-remote`), used only in the Claude Desktop
  stdio snippet.
