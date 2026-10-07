import { describe, expect, it } from "vitest";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import {
  InvalidRequestError,
  NotFoundError,
  PaymentError,
  PaymentRequiredError,
  PriceLimitExceededError,
  RateLimitError,
  ServiceUnavailableError,
  Tanod,
  TanodError,
} from "../src/index.js";

const PAY_TO = "0x593857A4a4F619543ea12394137C3004ce841720";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const A = "0x" + "a".repeat(40);
const B = "0x" + "b".repeat(40);
const GAS = { chain: "base", gas_price_wei: "6000000", gas_price_gwei: "0.006", base_fee_gwei: "0.005" };

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");

function paymentRequired(amount = "1000", error = "Payment required") {
  return {
    x402Version: 2,
    error,
    resource: { url: "https://tanod.dev/v1/chain/gas", description: "test", mimeType: "application/json" },
    accepts: [
      {
        scheme: "exact",
        network: "eip155:8453",
        asset: USDC,
        amount,
        payTo: PAY_TO,
        maxTimeoutSeconds: 300,
        extra: { name: "USD Coin", version: "2" },
      },
    ],
    extensions: {},
  };
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

const r402 = (amount = "1000", error?: string, header = true) => {
  const pr = paymentRequired(amount, error);
  return json(402, pr, header ? { "PAYMENT-REQUIRED": b64(pr) } : {});
};

const settle = (amount = "1000") =>
  b64({ success: true, transaction: "0x" + "ab".repeat(32), network: "eip155:8453", payer: "0x" + "11".repeat(20), amount });

interface Captured {
  url: string;
  method: string;
  headers: Headers;
  body?: unknown;
}

function fakeFetch(handler: (req: Captured, n: number) => Response) {
  const calls: Captured[] = [];
  const fn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const req: Captured = {
      url: String(input),
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(req);
    return handler(req, calls.length);
  }) as typeof fetch;
  return { fn, calls };
}

const make = (handler: (req: Captured, n: number) => Response, opts: ConstructorParameters<typeof Tanod>[0] = {}) => {
  const f = fakeFetch(handler);
  return { client: new Tanod({ useEnv: false, fetch: f.fn, ...opts }), calls: f.calls };
};

const throwaway = () => privateKeyToAccount(generatePrivateKey()); // random, unfunded

describe("free tier / no wallet", () => {
  it("returns data with non-enumerable meta", async () => {
    const { client, calls } = make(() => json(200, GAS, { "X-Free-Remaining-Today": "6", "X-Tanod-Product": "chainpeek" }));
    const out = await client.gasPrice("base");
    expect(out.gas_price_gwei).toBe("0.006");
    expect(out.meta.freeRemainingToday).toBe(6);
    expect(out.meta.product).toBe("chainpeek");
    expect(out.meta.payment).toBeUndefined();
    expect(JSON.parse(JSON.stringify(out))).toEqual(GAS);
    expect(calls[0]!.url).toBe("https://tanod.dev/v1/chain/gas");
    expect(calls[0]!.body).toEqual({ chain: "base" });
    expect(calls[0]!.headers.has("payment-signature")).toBe(false);
    expect(calls[0]!.headers.get("x-tanod-free")).toBe("1"); // the free tier is opt-in over HTTP
  });

  it("useFreeTier: false leaves the opt-in header off", async () => {
    const { client, calls } = make(() => r402("1000"), { useFreeTier: false });
    expect(client.useFreeTier).toBe(false);
    await expect(client.gasPrice("base")).rejects.toBeInstanceOf(PaymentRequiredError);
    expect(calls[0]!.headers.has("x-tanod-free")).toBe(false);
  });

  it("402 without wallet throws PaymentRequiredError with price", async () => {
    const { client, calls } = make(() => r402("1000"));
    const err = await client.gasPrice("base").catch((e) => e);
    expect(err).toBeInstanceOf(PaymentRequiredError);
    expect(err.priceUsd).toBe(0.001);
    expect(err.amountAtomic).toBe("1000");
    expect(err.payTo).toBe(PAY_TO);
    expect(err.message).toContain("USD 0.001");
    expect(calls).toHaveLength(1);
  });

  it("parses a 402 from the body when the header is missing", async () => {
    const { client } = make(() => r402("5000", undefined, false));
    const err = await client.checkAddress(A).catch((e) => e);
    expect(err).toBeInstanceOf(PaymentRequiredError);
    expect(err.priceUsd).toBe(0.005);
  });

  it("input-refused 402 is InvalidRequestError and is never signed", async () => {
    const { client, calls } = make(() => r402("1000", "Payment required. The input would be refused: invalid_request"), {
      signer: throwaway(),
    });
    await expect(client.gasPrice("base")).rejects.toBeInstanceOf(InvalidRequestError);
    expect(calls).toHaveLength(1);
  });
});

describe("paid flow", () => {
  it("signs with x402, retries and returns the receipt", async () => {
    const account = throwaway();
    const { client, calls } = make(
      (req) => (req.headers.has("payment-signature") ? json(200, GAS, { "PAYMENT-RESPONSE": settle() }) : r402("1000")),
      { signer: account },
    );
    expect(client.hasWallet).toBe(true);
    const out = await client.gasPrice("base");
    expect(calls).toHaveLength(2);
    expect(calls[0]!.headers.get("x-tanod-free")).toBe("1");
    expect(calls[1]!.headers.has("x-tanod-free")).toBe(false); // never on the paid retry
    const sig = JSON.parse(Buffer.from(calls[1]!.headers.get("payment-signature")!, "base64").toString());
    expect(sig.x402Version).toBe(2);
    expect(sig.accepted.amount).toBe("1000");
    expect(sig.accepted.payTo).toBe(PAY_TO);
    expect(sig.payload.authorization.from.toLowerCase()).toBe(account.address.toLowerCase());
    expect(out.meta.payment?.success).toBe(true);
    expect(out.meta.payment?.transaction).toBe("0x" + "ab".repeat(32));
    expect(out.meta.payment?.priceUsd).toBe(0.001);
  });

  it("useFreeTier: false pays without ever sending the opt-in header", async () => {
    const { client, calls } = make(
      (req) => (req.headers.has("payment-signature") ? json(200, GAS, { "PAYMENT-RESPONSE": settle() }) : r402("1000")),
      { signer: throwaway(), useFreeTier: false },
    );
    const out = await client.gasPrice("base");
    expect(out.meta.payment?.success).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls.every((c) => !c.headers.has("x-tanod-free"))).toBe(true);
  });

  it("accepts a private key (option or env) and never prints it", async () => {
    const key = generatePrivateKey();
    const c = new Tanod({ privateKey: key, useEnv: false });
    expect(c.hasWallet).toBe(true);
    expect(String(c)).not.toContain(key.slice(2));
    expect(JSON.stringify(c)).not.toContain(key.slice(2));
    process.env.TANOD_PRIVATE_KEY = key;
    try {
      expect(new Tanod().hasWallet).toBe(true);
      expect(new Tanod({ useEnv: false }).hasWallet).toBe(false);
    } finally {
      delete process.env.TANOD_PRIVATE_KEY;
    }
  });

  it("invalid key error does not echo the key", () => {
    let err: unknown;
    try {
      new Tanod({ privateKey: "0xnot-a-key-SECRET", useEnv: false });
    } catch (e) {
      err = e;
    }
    expect(err).toBeInstanceOf(PaymentError);
    expect(String((err as Error).message)).not.toContain("SECRET");
  });

  it("price cap blocks signing", async () => {
    const { client, calls } = make(() => r402("2000000"), { signer: throwaway() });
    const err = await client.agentsBulk().catch((e) => e);
    expect(err).toBeInstanceOf(PriceLimitExceededError);
    expect(err.priceUsd).toBe(2);
    expect(calls).toHaveLength(1);
  });

  it("raised cap allows bulk", async () => {
    const { client } = make(
      (req) =>
        req.headers.has("payment-signature")
          ? json(200, { generated_at: "x", count: 0, records: [] }, { "PAYMENT-RESPONSE": settle("2000000") })
          : r402("2000000"),
      { signer: throwaway(), maxPriceUsd: 2.5 },
    );
    const out = await client.agentsBulk();
    expect(out.meta.payment?.priceUsd).toBe(2);
  });

  it("refused paid retry surfaces the reason", async () => {
    const { client, calls } = make((req) => (req.headers.has("payment-signature") ? r402("3000", "price_mismatch") : r402("1000")), {
      signer: throwaway(),
    });
    const err = await client.gasPrice("base").catch((e) => e);
    expect(err).toBeInstanceOf(PaymentRequiredError);
    expect(err.reason).toBe("price_mismatch");
    expect(calls).toHaveLength(2);
  });

  it("paid 503 restarts with a fresh payment", async () => {
    const { client, calls } = make(
      (req, n) => {
        if (!req.headers.has("payment-signature")) return r402("1000");
        if (n === 2) return json(503, { error: { code: "facilitator_unavailable" } }, { "Retry-After": "0" });
        return json(200, GAS, { "PAYMENT-RESPONSE": settle() });
      },
      { signer: throwaway() },
    );
    const out = await client.gasPrice("base");
    expect(out.meta.payment).toBeDefined();
    expect(calls).toHaveLength(4);
    expect(calls[1]!.headers.get("payment-signature")).not.toBe(calls[3]!.headers.get("payment-signature"));
  });
});

describe("retries and errors", () => {
  it("retries 503 then succeeds", async () => {
    const { client, calls } = make((_, n) => (n === 1 ? json(503, { error: { code: "busy" } }, { "Retry-After": "0" }) : json(200, GAS)));
    expect((await client.gasPrice("base")).chain).toBe("base");
    expect(calls).toHaveLength(2);
  });

  it("gives up on 503 after maxRetries", async () => {
    const { client, calls } = make(() => json(503, { error: { code: "busy" } }, { "Retry-After": "0" }), { maxRetries: 2 });
    await expect(client.gasPrice("base")).rejects.toBeInstanceOf(ServiceUnavailableError);
    expect(calls).toHaveLength(3);
  });

  it("429 with long Retry-After throws at once", async () => {
    const { client, calls } = make(() => json(429, { error: { code: "rate_limited" } }, { "Retry-After": "120" }));
    const err = await client.gasPrice("base").catch((e) => e);
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err.retryAfterSeconds).toBe(120);
    expect(calls).toHaveLength(1);
  });

  it.each([
    [404, NotFoundError],
    [422, InvalidRequestError],
    [413, InvalidRequestError],
    [502, ServiceUnavailableError],
  ])("maps %i", async (status, cls) => {
    const { client } = make(() => json(status, { error: { code: "x", message: "nope" } }));
    const err = await client.scanContractAddress(A).catch((e) => e);
    expect(err).toBeInstanceOf(cls);
    expect(err.message).toContain("nope");
  });

  it("client-side validation rejects without a request", async () => {
    const { client, calls } = make(() => json(200, GAS));
    await expect(client.checkAddress("0x123")).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(client.gasPrice("solana" as never)).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(client.resolveEns({ name: "a.eth", address: A })).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(client.scanPackage({})).rejects.toBeInstanceOf(InvalidRequestError);
    await expect(client.agentsHistory({ source: "x" })).rejects.toBeInstanceOf(InvalidRequestError);
    expect(calls).toHaveLength(0);
  });
});

describe("request shapes", () => {
  const cases: [string, (c: Tanod) => Promise<unknown>, string, string, unknown][] = [
    ["scanContractSource", (c) => c.scanContractSource({ source: "contract C {}", options: { includeInformational: true } }), "POST", "/v1/scan/source", { source: "contract C {}", options: { include_informational: true } }],
    ["scanContractSource std", (c) => c.scanContractSource({ standardJson: { language: "Solidity" }, compilerVersion: "0.8.26" }), "POST", "/v1/scan/source", { standard_json: { language: "Solidity" }, compiler_version: "0.8.26" }],
    ["scanContractAddress", (c) => c.scanContractAddress(A, "base"), "POST", "/v1/scan/address", { address: A, chain: "base" }],
    ["getContractSource", (c) => c.getContractSource("ethereum", A), "GET", `/v1/source/ethereum/${A}`, undefined],
    ["checkAddress", (c) => c.checkAddress(A), "POST", "/v1/check/address", { address: A, chain: "base" }],
    ["scanPackage", (c) => c.scanPackage({ source: "npm:left-pad@1.3.0" }), "POST", "/v1/scan/package", { source: "npm:left-pad@1.3.0" }],
    ["scanPackage upload", (c) => c.scanPackage({ content: new TextEncoder().encode("# skill"), filename: "SKILL.md" }), "POST", "/v1/scan/package", { content_base64: Buffer.from("# skill").toString("base64"), filename: "SKILL.md" }],
    ["render", (c) => c.render("https://example.com"), "POST", "/v1/render", { url: "https://example.com", format: "markdown" }],
    ["inspectDomain", (c) => c.inspectDomain("example.com", ["dns"]), "POST", "/v1/domain/inspect", { domain: "example.com", checks: ["dns"] }],
    ["resolveEns", (c) => c.resolveEns({ name: "vitalik.eth" }), "POST", "/v1/chain/ens", { name: "vitalik.eth" }],
    ["decodeCalldata", (c) => c.decodeCalldata("0xa9059cbb" + "0".repeat(128)), "POST", "/v1/chain/calldata", { calldata: "0xa9059cbb" + "0".repeat(128) }],
    ["tokenInfo", (c) => c.tokenInfo("base", A), "POST", "/v1/chain/token", { chain: "base", address: A }],
    ["balance", (c) => c.balance("base", A, B), "POST", "/v1/chain/balance", { chain: "base", address: A, token: B }],
    ["gasPrice", (c) => c.gasPrice("ethereum"), "POST", "/v1/chain/gas", { chain: "ethereum" }],
    ["latestBlock", (c) => c.latestBlock("base"), "POST", "/v1/chain/block", { chain: "base" }],
    ["agentsSummary", (c) => c.agentsSummary(), "GET", "/v1/agents/summary", undefined],
    ["agentsQuery", (c) => c.agentsQuery({ network: "base", q: "weather", page_size: 10 }), "POST", "/v1/agents/query", { network: "base", q: "weather", page_size: 10 }],
    ["agentsHistory", (c) => c.agentsHistory({ url: "https://x.example/api" }), "POST", "/v1/agents/history", { url: "https://x.example/api" }],
    ["agentsExport", (c) => c.agentsExport({ source: "payai", limit: 10 }), "POST", "/v1/agents/export", { source: "payai", limit: 10, format: "json" }],
    ["agentsExportCsv", (c) => c.agentsExportCsv({ limit: 10 }), "POST", "/v1/agents/export", { limit: 10, format: "csv" }],
    ["agentsBulk", (c) => c.agentsBulk("smithery"), "POST", "/v1/agents/bulk", { source: "smithery" }],
    ["getReport", (c) => c.getReport("abc123"), "GET", "/v1/report/abc123.json", undefined],
    ["getReportMarkdown", (c) => c.getReportMarkdown("abc123"), "GET", "/v1/report/abc123.md", undefined],
    ["health", (c) => c.health(), "GET", "/healthz", undefined],
  ];
  it.each(cases)("%s", async (_name, call, method, path, body) => {
    const { client, calls } = make(() => json(599, {}));
    await expect(call(client)).rejects.toBeInstanceOf(TanodError);
    expect(calls[0]!.method).toBe(method);
    expect(calls[0]!.url).toBe("https://tanod.dev" + path);
    expect(calls[0]!.body).toEqual(body);
  });
});

describe("text routes", () => {
  it("returns CSV and Markdown as text", async () => {
    const { client } = make((req) =>
      req.url.endsWith(".md")
        ? new Response("# Report", { status: 200, headers: { "content-type": "text/plain" } })
        : new Response("source,id\n", { status: 200, headers: { "content-type": "text/csv" } }),
    );
    expect((await client.agentsExportCsv()).text).toBe("source,id\n");
    expect((await client.getReportMarkdown("s1")).text).toBe("# Report");
  });
});

describe("fail-closed quote validation", () => {
  it.each([
    [{ asset: "0x" + "ab".repeat(20) }],
    [{ network: "eip155:1" }],
    [{ amount: "not-a-number" }],
  ])("unexpected quote %o is refused and never signed", async (override) => {
    const pr = paymentRequired("1000");
    Object.assign(pr.accepts[0]!, override);
    const { client, calls } = make(() => json(402, pr, { "PAYMENT-REQUIRED": b64(pr) }), { signer: throwaway() });
    const err = await client.gasPrice("base").catch((e) => e);
    expect(err).toBeInstanceOf(TanodError);
    expect(err).not.toBeInstanceOf(PaymentError);
    expect(calls).toHaveLength(1);
  });
});

