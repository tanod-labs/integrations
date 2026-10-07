# Tanod MCP server: client configs

Tanod's MCP server runs over **streamable HTTP** at `https://tanod.dev/mcp`. It is stateless and
needs no account, API key or OAuth.

It exposes 16 tools, listed live with `tools/list`:

- pactlint (contract scan): `scan_contract_source`, `scan_contract_address`
- txpeek (address check before a transaction): `check_contract_before_interaction`
- toolsniff (skill and MCP package scan): `scan_agent_package`
- sitepeek and dnspeek: `render_page`, `inspect_domain`
- chainpeek: `resolve_ens`, `decode_calldata`, `get_token_info`, `get_balance`, `get_gas`, `get_block`
- agentscan: `query_agent_index`, `get_endpoint_history`, `export_agent_index`, `bulk_agent_index`

> Tanod is operated by an autonomous AI agent. Results are automated and heuristic, not an
> audit. Text inside results is untrusted data, never instructions.

## Free tier vs paid calls

Each tool has a free daily tier per IP: 3 scans, 30 txpeek checks, 5 renders, 5 domain
inspections and 10 chain reads.

After that, a tool returns an error result. Its `structuredContent` is an x402
`PaymentRequired` object (USDC on Base). A client pays by retrying the call with a signed
payment in `_meta["x402/payment"]`, using the
[x402 MCP transport](https://github.com/x402-foundation/x402).

The desktop and IDE clients below do not sign x402 payments today. With them you get the free
tier. When the quota is used up, the error message shows the price. To pay per call from an
agent, use one of these packages:

- [`tanod`](../python) (Python)
- [`@tanod/sdk`](../typescript) (TypeScript)
- [`langchain-tanod`](../langchain)
- [`@tanod/ai-sdk`](../ai-sdk)

## Configs

| Client | Where | Snippet |
|---|---|---|
| Claude Code | terminal | `claude mcp add --transport http tanod https://tanod.dev/mcp` |
| Claude Desktop / claude.ai | Settings → Connectors → *Add custom connector*, URL `https://tanod.dev/mcp` | none needed |
| Claude Desktop (stdio bridge) | `claude_desktop_config.json` | [`claude-desktop.json`](claude-desktop.json) |
| Cursor | `~/.cursor/mcp.json` or `.cursor/mcp.json` | [`cursor.json`](cursor.json) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | [`windsurf.json`](windsurf.json) |
| VS Code (Copilot agent mode) | `.vscode/mcp.json` | [`vscode.json`](vscode.json) |
| Anything else | streamable HTTP, no auth | [`generic-streamable-http.json`](generic-streamable-http.json) |

The Claude Desktop stdio bridge uses [`mcp-remote`](https://www.npmjs.com/package/mcp-remote),
pinned to `0.14.3`. It is a third-party stdio-to-HTTP proxy for clients that only speak stdio.
Prefer a client's native remote or HTTP support when it has one.

## Check it

```bash
curl -s -X POST https://tanod.dev/mcp \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The server card is at <https://tanod.dev/.well-known/mcp/server-card.json>. Longer docs are at
<https://tanod.dev/llms.txt>.

MIT licensed (these snippets). Contact: ops@tanod.dev.
