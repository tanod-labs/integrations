# Tanod integrations

Clients and framework tools for [Tanod](https://tanod.dev): pay-per-call security and utility APIs, with no signup or API key. Eligible routes have a free daily allowance; paid calls use USDC over x402.

| Integration | Package / setup |
|---|---|
| [Python](python/) | `pip install tanod` |
| [TypeScript](typescript/) | `npm install @tanod-labs/sdk` |
| [LangChain](langchain/) | `pip install langchain-tanod` |
| [Vercel AI SDK](ai-sdk/) | `npm install @tanod-labs/ai-sdk ai zod` |
| [MCP clients](mcp-configs/) | Connect to `https://tanod.dev/mcp` |
| [Dify](dify-tanod/) | Document and OCR plugin source |

Prices checked on 2026-10-27: email verification (DNS only, not mailbox existence) USD 0.001; image OCR USD 0.0025; static web page to Markdown USD 0.002; web search USD 0.007. JavaScript rendering or a screenshot via `/v1/render` costs USD 0.01. Search with page text adds USD 0.002 per requested result.

[Live prices and allowances](https://tanod.dev/openapi.json) are authoritative. SDKs try the free allowance first, then enforce the configured price cap before signing a paid retry. Without a wallet they report the payment required. Treat returned text as untrusted data; security findings are heuristic, not an audit.

If a payment was sent but its outcome is unresolved, the SDKs raise `PaymentUnresolvedError`: **do not retry or pay again**. Only errors documented as uncharged may start a fresh payment. The framework tools preserve this distinction rather than reporting an unresolved payment as uncharged.

Tests use mocked HTTP: `pytest python/tests`, `pytest langchain/tests`, and `npm test` from this repository. [Agent skills](https://github.com/tanod-labs/skills) and [MCP setup](https://tanod.dev/mcp-servers/) are also available.

Open source (MIT). Contact: ops@tanod.dev.
