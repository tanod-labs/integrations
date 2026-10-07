import { describe, expect, it } from "vitest";
import { generateText } from "ai";
import { MockLanguageModelV4 } from "ai/test";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import type { z } from "zod/v4";

import { DEFAULT_TOOL_NAMES, TOOL_NAMES, Tanod, allTanodTools, tanodTools, type TanodToolResult } from "../src/index.js";

const A = "0x" + "a".repeat(40);
const CHECK = { schema: "precheck/1", ok: true, verdict: "caution", risk_score: 30, reasons: [{ code: "proxy" }] };
const GAS = { chain: "base", gas_price_wei: "1", gas_price_gwei: "0.000000001" };
const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");
const pr = (amount = "5000") => ({
  x402Version: 2,
  error: "Payment required",
  accepts: [
    {
      scheme: "exact",
      network: "eip155:8453",
      asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      amount,
      payTo: "0x593857A4a4F619543ea12394137C3004ce841720",
      maxTimeoutSeconds: 300,
      extra: { name: "USD Coin", version: "2" },
    },
  ],
});
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

function fake(handler: (url: string, init: RequestInit | undefined) => Response) {
  const calls: { url: string; body: unknown; headers: Headers }[] = [];
  const fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: new Headers(init?.headers) });
    return handler(String(input), init);
  }) as typeof globalThis.fetch;
  return { fetch, calls };
}

const opts = { toolCallId: "t1", messages: [] } as never;

describe("tool set", () => {
  it("default and full inventories with priced, untrusted-data descriptions", () => {
    const client = new Tanod({ useEnv: false });
    expect(Object.keys(tanodTools({ client }))).toEqual(DEFAULT_TOOL_NAMES);
    const all = allTanodTools({ client });
    expect(Object.keys(all)).toEqual([...TOOL_NAMES]);
    for (const [name, t] of Object.entries(all)) {
      expect(t.description, name).toMatch(/USD|FREE/);
      expect(t.description, name).toContain("untrusted");
    }
    expect(() => tanodTools({ client, include: ["nope" as never] })).toThrow(/unknown/);
  });

  it("input schemas validate", () => {
    const all = allTanodTools({ client: new Tanod({ useEnv: false }) });
    const schema = all.tanod_check_address.inputSchema as unknown as z.ZodType;
    expect(schema.safeParse({ address: "0x12" }).success).toBe(false);
    expect(schema.safeParse({ address: A })).toMatchObject({ success: true, data: { address: A, chain: "base" } });
  });
});

describe("execute", () => {
  it("returns ok result with free-tier info", async () => {
    const f = fake(() => json(200, CHECK, { "X-Free-Remaining-Today": "29" }));
    const tools = tanodTools({ useEnv: false, fetch: f.fetch });
    const out = (await tools.tanod_check_address!.execute!({ address: A, chain: "base" }, opts)) as TanodToolResult;
    expect(out).toMatchObject({ ok: true, result: CHECK, payment: null, freeRemainingToday: 29 });
    expect(f.calls[0]!.body).toEqual({ address: A, chain: "base" });
  });

  it("payment needed without wallet -> structured, uncharged error", async () => {
    const f = fake(() => json(402, pr("20000"), { "PAYMENT-REQUIRED": b64(pr("20000")) }));
    const tools = tanodTools({ useEnv: false, fetch: f.fetch });
    const out = (await tools.tanod_agents_query!.execute!({ q: "x", page_size: 20 }, opts)) as TanodToolResult;
    expect(out).toMatchObject({ ok: false, error: "payment_required", priceUsd: 0.02, charged: false });
  });

  it("price cap -> price_limit", async () => {
    const f = fake(() => json(402, pr("250000"), { "PAYMENT-REQUIRED": b64(pr("250000")) }));
    const tools = allTanodTools({ signer: privateKeyToAccount(generatePrivateKey()), maxPriceUsd: 0.05, useEnv: false, fetch: f.fetch });
    const out = (await tools.tanod_agents_export.execute!({ limit: 10 }, opts)) as TanodToolResult;
    expect(out).toMatchObject({ ok: false, error: "price_limit", priceUsd: 0.25 });
    expect(f.calls).toHaveLength(1);
  });

  it("paid call reports the receipt", async () => {
    const settle = b64({ success: true, transaction: "0x" + "cd".repeat(32), network: "eip155:8453", amount: "1000" });
    const f = fake((_u, init) =>
      new Headers(init?.headers).has("payment-signature")
        ? json(200, GAS, { "PAYMENT-RESPONSE": settle })
        : json(402, pr("1000"), { "PAYMENT-REQUIRED": b64(pr("1000")) }),
    );
    const tools = tanodTools({ signer: privateKeyToAccount(generatePrivateKey()), useEnv: false, fetch: f.fetch });
    const out = (await tools.tanod_gas_price!.execute!({ chain: "base" }, opts)) as TanodToolResult;
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.payment).toMatchObject({ success: true, priceUsd: 0.001, transaction: "0x" + "cd".repeat(32) });
      expect(out.payment).not.toHaveProperty("raw");
    }
    expect(f.calls).toHaveLength(2);
  });

  it.each([
    ["tanod_scan_contract_source", { source: "contract C {}" }, "/v1/scan/source", { source: "contract C {}" }],
    ["tanod_scan_contract_address", { address: A, chain: "ethereum" }, "/v1/scan/address", { address: A, chain: "ethereum" }],
    ["tanod_get_contract_source", { address: A, chain: "base" }, `/v1/source/base/${A}`, undefined],
    ["tanod_scan_package", { source: "pypi:requests==2.32.3" }, "/v1/scan/package", { source: "pypi:requests==2.32.3" }],
    ["tanod_render_url", { url: "https://example.com", js: false }, "/v1/render", { url: "https://example.com", format: "markdown" }],
    ["tanod_inspect_domain", { domain: "example.com", checks: ["email"] }, "/v1/domain/inspect", { domain: "example.com", checks: ["email"] }],
    ["tanod_resolve_ens", { name: "vitalik.eth" }, "/v1/chain/ens", { name: "vitalik.eth" }],
    ["tanod_decode_calldata", { calldata: "0xa9059cbb" }, "/v1/chain/calldata", { calldata: "0xa9059cbb" }],
    ["tanod_token_info", { address: A, chain: "base" }, "/v1/chain/token", { chain: "base", address: A }],
    ["tanod_balance", { address: A, chain: "base" }, "/v1/chain/balance", { chain: "base", address: A }],
    ["tanod_latest_block", { chain: "ethereum" }, "/v1/chain/block", { chain: "ethereum" }],
    ["tanod_extract_pdf", { url: "https://x.example/a.pdf", max_pages: 3 }, "/v1/pdf", { url: "https://x.example/a.pdf", max_pages: 3 }],
    ["tanod_get_page_meta", { url: "https://github.com/" }, "/v1/meta", { url: "https://github.com/" }],
    ["tanod_ocr_image", { url: "https://x.example/a.png" }, "/v1/ocr", { url: "https://x.example/a.png" }],
    ["tanod_rdap_lookup", { query: "example.com" }, "/v1/rdap", { query: "example.com" }],
    ["tanod_verify_email", { email: "a@example.com" }, "/v1/email/verify", { email: "a@example.com" }],
    ["tanod_ip_lookup", { ip: "1.1.1.1" }, "/v1/ip", { ip: "1.1.1.1" }],
    ["tanod_get_token_price", { chain: "base", pair: "ETH/USD" }, "/v1/chain/price", { chain: "base", pair: "ETH/USD" }],
    ["tanod_get_transaction", { chain: "base", hash: "0x" + "c".repeat(64) }, "/v1/chain/tx", { chain: "base", hash: "0x" + "c".repeat(64) }],
    ["tanod_get_nft", { chain: "ethereum", contract: A, token_id: "1" }, "/v1/chain/nft", { chain: "ethereum", contract: A, token_id: "1" }],
    ["tanod_get_allowance", { chain: "base", token: A, owner: A, spender: A }, "/v1/chain/allowance", { chain: "base", token: A, owner: A, spender: A }],
    ["tanod_get_portfolio", { chain: "base", address: A, tokens: [A] }, "/v1/chain/portfolio", { chain: "base", address: A, tokens: [A] }],
    ["tanod_get_swap_quote", { chain: "base", token_in: A, token_out: A, amount_in: "1" }, "/v1/chain/quote", { chain: "base", token_in: A, token_out: A, amount_in: "1" }],
    ["tanod_web_search", { query: "x402", count: 2 }, "/v1/search", { query: "x402", count: 2 }],
    ["tanod_get_weather", { place: "Oslo, NO" }, "/v1/weather", { place: "Oslo, NO" }],
    ["tanod_agents_summary", {}, "/v1/agents/summary", undefined],
    ["tanod_agents_history", { url: "https://x.example" }, "/v1/agents/history", { url: "https://x.example" }],
    ["tanod_agents_export", { limit: 10 }, "/v1/agents/export", { limit: 10, format: "json" }],
  ] as const)("%s hits its route", async (name, input, path, body) => {
    const f = fake(() => json(500, { error: "stop" }));
    const tools = allTanodTools({ useEnv: false, fetch: f.fetch });
    const out = (await (tools[name].execute as (i: unknown, o: unknown) => Promise<TanodToolResult>)(input, opts)) as TanodToolResult;
    expect(out).toMatchObject({ ok: false, error: "error" });
    expect(f.calls[0]!.url).toBe("https://tanod.dev" + path);
    expect(f.calls[0]!.body).toEqual(body);
  });
});

describe("generateText integration (mock model, mock API)", () => {
  it("model calls a Tanod tool and gets the result", async () => {
    const f = fake(() => json(200, GAS, { "X-Free-Remaining-Today": "9" }));
    const model = new MockLanguageModelV4({
      doGenerate: {
        content: [{ type: "tool-call", toolCallId: "call-1", toolName: "tanod_gas_price", input: JSON.stringify({ chain: "base" }) }],
        finishReason: { unified: "tool-calls", raw: "tool_calls" },
        usage: {
          inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
          outputTokens: { total: 1, text: 1, reasoning: 0 },
        },
        warnings: [],
      },
    });
    const result = await generateText({ model, tools: tanodTools({ useEnv: false, fetch: f.fetch }), prompt: "gas on base?" });
    const tr = result.steps[0]!.toolResults[0]!;
    expect(tr.toolName).toBe("tanod_gas_price");
    expect(tr.output).toMatchObject({ ok: true, result: GAS, freeRemainingToday: 9 });
    expect(f.calls[0]!.url).toBe("https://tanod.dev/v1/chain/gas");
  });
});
