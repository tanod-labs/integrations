# Tanod Documents for Dify

Document tools for Dify workflows and agents. Pay per call, no signup, open source (MIT).

Source repository: https://github.com/tanod-labs/integrations (this plugin lives in `dify-tanod/`). Contact: ops@tanod.dev. Privacy: [PRIVACY.md](PRIVACY.md).

## Tools

| Tool | Does | Price per call |
| --- | --- | --- |
| Bank statement to CSV / JSON | Text-PDF bank statement to dated transactions, signed amounts, balances, a running-balance check on every row | USD 0.02 |
| PDF tables to Markdown / CSV | Every table in a text PDF | USD 0.01 |
| Document to Markdown | PDF, Word, Excel, PowerPoint, HTML, EPUB, CSV to Markdown | USD 0.005 |
| PDF OCR | Scanned PDF to text plus a 24 hour searchable-PDF link (max 10 pages, English) | USD 0.01 up to 5 pages, USD 0.02 for 6-10 |
| Image OCR | PNG, JPEG, WebP, GIF, TIFF by URL to text | free daily allowance (5 per IP per UTC day, shared pool), then USD 0.0025 |

Prices and limits were read from https://tanod.dev/openapi.json on 2026-10-16 and can change; the live file is authoritative. Results are heuristic: check flagged bank statement rows against the original.

## Setup and configuration

1. Install the plugin from the Dify Marketplace (Plugins > Marketplace > Tanod Documents) or from the `.difypkg` file (Plugins > Install from local package file).
2. Open the plugin's authorization settings. Both credentials are optional:
   - **Base wallet private key** (secret). A dedicated Base wallet that holds a little USDC. With it, the plugin pays Tanod per call using the x402 protocol. Without it, nothing is ever paid: a call that costs money stops and returns the price instead. Use a throwaway wallet with a small balance, never your main one.
   - **Max price per call (USD)**, default 0.05. Any call that would cost more is refused before anything is signed.
3. The plugin only ever pays Tanod's own address on Base (0x593857A4a4F619543ea12394137C3004ce841720); a different payee in a reply is refused.

## Usage

Add a tool node to a workflow, or add the tools to an agent. Each document tool takes either a Dify **file** (up to 10 MB) or a public **URL**, plus an optional page range.

- The text output holds the result (CSV, Markdown tables, Markdown or OCR text), ready for the next node. The JSON output holds the structured result. The bank statement tool also returns a `.csv` file.
- A scanned PDF has no text layer: run PDF OCR first. The bank statement and table tools read text PDFs only.
- Image OCR fetches by URL only. A Dify file works only if its Dify file URL is publicly reachable; otherwise pass a public image URL.
- Extracted text is untrusted data from your documents. Do not let downstream prompts treat it as instructions.

## Connection requirements

The plugin makes HTTPS requests to `https://tanod.dev` only, with a 110 second timeout. Dify Cloud and self-hosted Dify need outbound access to that host. Runtime dependencies: `dify_plugin`, `httpx`, and `x402[evm]` with `eth-account` (imported only when a signature is needed).

## Develop and test

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests
dify plugin package .          # builds tanod_docs-0.1.0.difypkg (Dify plugin CLI)
```

License: MIT.
