// Answers that tie up a payment are never retried and never paid again (settlement_unknown,
// purchase_unresolved, credit_delivery_pending, settled-but-failed, no readable answer after paying).
// Mocked fetch only, throwaway unfunded key, no real payment.
import { describe, expect, it } from "vitest";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import {
  CreditDeliveryPendingError,
  PaymentUnresolvedError,
  PurchaseUnresolvedError,
  ServiceUnavailableError,
  SettlementUnknownError,
  Tanod,
  TanodError,
} from "../src/index.js";

const PAY_TO = "0x593857A4a4F619543ea12394137C3004ce841720";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const GAS = { chain: "base", gas_price_wei: "6000000", gas_price_gwei: "0.006", base_fee_gwei: "0.005" };
const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");
const unb64 = (s: string) => JSON.parse(Buffer.from(s, "base64").toString());

const pr = {
  x402Version: 2,
  error: "Payment required",
  resource: { url: "https://tanod.dev/v1/chain/gas", description: "test", mimeType: "application/json" },
  accepts: [
    { scheme: "exact", network: "eip155:8453", asset: USDC, amount: "1000", payTo: PAY_TO, maxTimeoutSeconds: 300, extra: { name: "USD Coin", version: "2" } },
  ],
  extensions: {},
};
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
const r402 = () => json(402, pr, { "PAYMENT-REQUIRED": b64(pr) });
const settle = () =>
  b64({ success: true, transaction: "0x" + "ab".repeat(32), network: "eip155:8453", payer: "0x" + "11".repeat(20), amount: "1000" });
const failedSettle = () =>
  b64({ success: false, errorReason: "insufficient_funds", transaction: "", network: "eip155:8453", payer: "0x" + "11".repeat(20) });

// The server's own messages (product/api/scanapi/paymentid.py _REFUSALS), shortened.
const MESSAGES: Record<string, string> = {
  settlement_unknown: "Settlement was submitted but its outcome is not confirmed. Keep this payment ID for reconciliation.",
  purchase_unresolved: "An identical earlier purchase from this wallet is unresolved. This payment was verified but not submitted or charged.",
  credit_delivery_pending: "The payment is confirmed, but prepaid credit delivery is pending.",
};
const err = (status: number, code: string, headers: Record<string, string> = { "Retry-After": "0" }, extra: object = {}) =>
  json(status, { error: { code, message: MESSAGES[code] ?? code }, ...extra }, headers);

type Handler = (headers: Headers, n: number) => Response;
function make(handler: Handler, opts: ConstructorParameters<typeof Tanod>[0] = {}) {
  const calls: Headers[] = [];
  const fn = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const h = new Headers(init?.headers);
    calls.push(h);
    return handler(h, calls.length);
  }) as typeof fetch;
  return { client: new Tanod({ useEnv: false, fetch: fn, maxRetries: 3, ...opts }), calls };
}
const paidThen = (answer: () => Response): Handler => (h) => (h.has("payment-signature") ? answer() : r402());
const throwaway = () => privateKeyToAccount(generatePrivateKey()); // random, unfunded
const signedAuth = (calls: Headers[], i: number) => unb64(calls[i]!.get("payment-signature")!).payload.authorization;

const CASES = [
  [503, "settlement_unknown", SettlementUnknownError, undefined],
  [409, "purchase_unresolved", PurchaseUnresolvedError, false],
  [503, "credit_delivery_pending", CreditDeliveryPendingError, true],
] as const;

describe("payments that must be reconciled", () => {
  it("are not ServiceUnavailableError", () => {
    for (const [, , Cls] of CASES) {
      const e = new Cls("x");
      expect(e).toBeInstanceOf(PaymentUnresolvedError);
      expect(e).toBeInstanceOf(TanodError);
      expect(e).not.toBeInstanceOf(ServiceUnavailableError);
      expect(e.name).toBe(Cls.name);
    }
  });

  for (const [status, code, Cls, charged] of CASES) {
    it(`paid ${status} ${code} throws ${Cls.name} with no retry and no new payment`, async () => {
      const account = throwaway();
      const { client, calls } = make(paidThen(() => err(status, code)), { signer: account });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e).toBeInstanceOf(Cls);
      expect(calls).toHaveLength(2); // unpaid 402, paid: no retry, no second payment
      expect(calls.filter((h) => h.has("payment-signature"))).toHaveLength(1);
      const auth = signedAuth(calls, 1);
      expect(e.code).toBe(code);
      expect(e.status).toBe(status);
      expect(e.charged).toBe(charged);
      expect(e.serverMessage).toBe(MESSAGES[code]);
      expect(e.payer.toLowerCase()).toBe(account.address.toLowerCase());
      expect(e.payer).toBe(auth.from);
      expect(e.nonce).toBe(auth.nonce);
      expect(e.paymentSignature).toBe(calls[1]!.get("payment-signature"));
      expect(e.priceUsd).toBe(0.001);
      expect(e.amountAtomic).toBe("1000");
      expect(e.message).toContain(code);
      expect(e.message).toContain(e.nonce);
      expect(e.message).toContain("Do not pay again");
      expect(e.message).not.toContain(e.paymentSignature);
      expect(client.spentUsd).toBe(0.001); // the signed payment stays counted
    });

    it(`${code} without a payment is not retried`, async () => {
      const { client, calls } = make(() => err(status, code));
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e).toBeInstanceOf(Cls);
      expect(calls).toHaveLength(1);
      expect(e.nonce).toBeUndefined();
      expect(e.paymentSignature).toBeUndefined();
    });
  }

  it("settlement_unknown keeps the settlement object and ignores Retry-After", async () => {
    const { client, calls } = make(paidThen(() => err(503, "settlement_unknown", { "Retry-After": "0" }, { settlement: { state: "uncertain" } })), {
      signer: throwaway(),
    });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(SettlementUnknownError);
    expect(e.settlement).toEqual({ state: "uncertain" });
    expect(calls).toHaveLength(2);
  });

  for (const code of ["busy", "queue_full", "facilitator_error", "payment_identifier_unavailable"]) {
    it(`paid 503 ${code} still retries with a fresh payment`, async () => {
      const { client, calls } = make(
        (h, n) => {
          if (!h.has("payment-signature")) return r402();
          if (n === 2) return err(503, code);
          return json(200, GAS, { "PAYMENT-RESPONSE": settle() });
        },
        { signer: throwaway() },
      );
      const out = await client.gasPrice("base");
      expect(out.meta.payment).toBeDefined();
      expect(calls).toHaveLength(4); // unpaid, paid(503), unpaid, paid
      expect(signedAuth(calls, 1).nonce).not.toBe(signedAuth(calls, 3).nonce);
    });
  }

  it("a paid error answer with a settlement receipt is charged and not retried", async () => {
    const { client, calls } = make(paidThen(() => err(503, "queue_full", { "Retry-After": "0", "PAYMENT-RESPONSE": settle() })), {
      signer: throwaway(),
    });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e.constructor).toBe(PaymentUnresolvedError);
    expect(e.code).toBe("queue_full");
    expect(e.charged).toBe(true);
    expect(e.receipt.transaction).toBe("0x" + "ab".repeat(32));
    expect(calls).toHaveLength(2);
  });

  for (const status of [500, 502, 503, 504]) {
    it(`a paid ${status} without a code is unresolved`, async () => {
      const { client, calls } = make(paidThen(() => new Response("<html>Bad Gateway</html>", { status, headers: { "Retry-After": "0" } })), {
        signer: throwaway(),
      });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.code).toBeUndefined();
      expect(e.charged).toBeUndefined();
      expect(e.nonce).toMatch(/^0x[0-9a-f]{64}$/);
      expect(calls).toHaveLength(2);
    });
  }

  it("an unpaid 502 without a code is still ServiceUnavailableError", async () => {
    const { client, calls } = make(() => new Response("Bad Gateway", { status: 502 }));
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(ServiceUnavailableError);
    expect(calls).toHaveLength(1);
  });
});

/** A body that sends a first chunk, then fails (the connection dropped while the answer was read). */
const brokenBody = () =>
  new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode('{"chain": "ba'));
      c.error(new TypeError("terminated: other side closed"));
    },
  });

describe("no answer after paying (M1)", () => {
  const failures: Record<string, (h: Headers) => Response> = {
    "fetch rejects with a timeout": () => {
      throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
    },
    "fetch rejects with a reset connection": () => {
      throw new TypeError("fetch failed", { cause: new Error("ECONNRESET") });
    },
    "the 2xx body stream errors": () => new Response(brokenBody(), { status: 200, headers: { "PAYMENT-RESPONSE": settle() } }),
    "the 5xx body stream errors": () => new Response(brokenBody(), { status: 503, headers: { "Retry-After": "0" } }),
  };
  for (const [what, fail] of Object.entries(failures)) {
    it(`${what}: PaymentUnresolvedError, exactly one PAYMENT-SIGNATURE`, async () => {
      const account = throwaway();
      const { client, calls } = make((h) => (h.has("payment-signature") ? fail(h) : r402()), { signer: account });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.cause).toBeDefined();
      expect(e.code).toBeUndefined();
      const auth = signedAuth(calls, 1);
      expect(e.nonce).toBe(auth.nonce);
      expect(e.payer).toBe(auth.from);
      expect(e.paymentSignature).toBe(calls[1]!.get("payment-signature"));
      expect(e.priceUsd).toBe(0.001);
      expect(e.message).toContain("no answer after the payment was sent");
      expect(e.message).toContain("Do not pay again");
      expect(calls).toHaveLength(2);
      expect(calls.filter((h) => h.has("payment-signature"))).toHaveLength(1);
      expect(client.spentUsd).toBe(0.001);
      if (what.includes("2xx")) {
        expect(e.charged).toBe(true);
        expect(e.receipt.transaction).toBe("0x" + "ab".repeat(32));
      } else expect(e.charged).toBeUndefined();
    });
  }

  it("an unpaid fetch rejection is still the plain error", async () => {
    const { client } = make(() => {
      throw new TypeError("fetch failed");
    });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(TypeError);
  });

  for (const [what, status, receipt, charged] of [
    ["200, no receipt", 200, undefined, undefined],
    ["200, a receipt that does not decode", 200, "%%not-base64%%", undefined],
    ["200, a failed settlement receipt", 200, "FAILED", undefined],
    ["503 with a successful receipt", 503, "OK", true],
  ] as const) {
    it(`body read error after paying (${what}): receipt kept, charged only with a successful receipt`, async () => {
      const value = receipt === "OK" ? settle() : receipt === "FAILED" ? failedSettle() : receipt;
      const headers: Record<string, string> = value ? { "PAYMENT-RESPONSE": value } : {};
      const { client, calls } = make(paidThen(() => new Response(brokenBody(), { status, headers })), { signer: throwaway() });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.charged).toBe(charged);
      expect(e.status).toBe(status);
      expect(e.receipt !== undefined).toBe(receipt === "OK" || receipt === "FAILED");
      expect(e.message).toContain("body read failed");
      expect(calls).toHaveLength(2);
    });
  }

  it("an unpaid body read error is a TanodError, not unresolved", async () => {
    const { client } = make(() => new Response(brokenBody(), { status: 200 }));
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(TanodError);
    expect(e).not.toBeInstanceOf(PaymentUnresolvedError);
  });
});

describe("after paying, only documented uncharged codes are retried (S2)", () => {
  for (const [status, code] of [
    [503, "internal_error"],
    [503, "brand_new_code"],
    [500, "micro_error_x"],
    [429, "brand_new_limit"],
    [502, "upstream_hiccup"],
  ] as const) {
    it(`paid ${status} ${code} is unresolved`, async () => {
      const { client, calls } = make(paidThen(() => err(status, code)), { signer: throwaway() });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.code).toBe(code);
      expect(e.charged).toBeUndefined();
      expect(e.message).toContain("not a code the server documents as uncharged");
      expect(calls).toHaveLength(2);
    });
  }

  it("paid refund_review_unavailable is charged even without a receipt", async () => {
    const { client, calls } = make(paidThen(() => err(503, "refund_review_unavailable")), { signer: throwaway() });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e.constructor).toBe(PaymentUnresolvedError);
    expect(e.charged).toBe(true);
    expect(calls).toHaveLength(2);
  });

  for (const code of ["rate_limited", "unsettled_failures_in_flight", "image_uncharged_limited"]) {
    it(`paid 429 ${code} retries with a fresh payment`, async () => {
      const { client, calls } = make(
        (h, n) => {
          if (!h.has("payment-signature")) return r402();
          if (n === 2) return err(429, code);
          return json(200, GAS, { "PAYMENT-RESPONSE": settle() });
        },
        { signer: throwaway() },
      );
      await client.gasPrice("base");
      expect(calls).toHaveLength(4);
      expect(signedAuth(calls, 1).nonce).not.toBe(signedAuth(calls, 3).nonce);
    });
  }

  it("paid uncharged 503 after the retries is ServiceUnavailableError", async () => {
    const { client, calls } = make(paidThen(() => err(503, "queue_full")), { signer: throwaway(), maxRetries: 1 });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(ServiceUnavailableError);
    expect(calls).toHaveLength(4);
  });
});

describe("a paid 2xx that cannot be read (S4)", () => {
  for (const [what, body] of [
    ["not JSON", "<html>not json</html>"],
    ["JSON but not an object", "7"],
  ] as const) {
    it(`${what}: unresolved, charged, receipt kept`, async () => {
      const { client, calls } = make(paidThen(() => new Response(body, { status: 200, headers: { "PAYMENT-RESPONSE": settle() } })), {
        signer: throwaway(),
      });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.charged).toBe(true);
      expect(e.status).toBe(200);
      expect(e.receipt.transaction).toBe("0x" + "ab".repeat(32));
      expect(e.message).toContain("could not be read");
      expect(calls).toHaveLength(2);
    });
  }

  for (const [what, body, headers] of [
    ["not JSON, no receipt (the free-miss 200)", "<html>free miss</html>", {}],
    ["JSON but not an object, no receipt", "7", {}],
    ["a receipt that does not decode", "nope", { "PAYMENT-RESPONSE": "%%not-base64%%" }],
  ] as const) {
    it(`${what}: unresolved, charged unknown`, async () => {
      const { client, calls } = make(paidThen(() => new Response(body, { status: 200, headers })), { signer: throwaway() });
      const e = await client.gasPrice("base").catch((x) => x);
      expect(e.constructor).toBe(PaymentUnresolvedError);
      expect(e.charged).toBeUndefined();
      expect(e.receipt).toBeUndefined();
      expect(e.status).toBe(200);
      expect(e.nonce).toBeTruthy();
      expect(e.message).toContain("could not be read");
      expect(calls).toHaveLength(2);
    });
  }

  it("an unpaid unreadable 2xx is a plain TanodError", async () => {
    const { client } = make(() => new Response("nope", { status: 200 }));
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(TanodError);
    expect(e).not.toBeInstanceOf(PaymentUnresolvedError);
  });
});

describe("error bodies are read capped (S3)", () => {
  it("reads at most 64 KiB of an error answer and cancels the rest", async () => {
    let pulled = 0;
    let cancelled = false;
    const chunk = new Uint8Array(16 * 1024).fill(0x61);
    const endless = () =>
      new ReadableStream<Uint8Array>({
        pull(c) {
          pulled++;
          c.enqueue(chunk);
        },
        cancel() {
          cancelled = true;
        },
      });
    const { client } = make(() => new Response(endless(), { status: 502 }), { maxRetries: 0 });
    const e = await client.gasPrice("base").catch((x) => x);
    expect(e).toBeInstanceOf(ServiceUnavailableError);
    expect(cancelled).toBe(true);
    expect(pulled).toBeLessThan(10);
    expect(String(e.body).length).toBeLessThanOrEqual(64 * 1024);
  });
});

describe("subclass defaults (N1)", () => {
  it("code and charged default when constructed directly", () => {
    expect(new SettlementUnknownError("x").code).toBe("settlement_unknown");
    expect(new SettlementUnknownError("x").charged).toBeUndefined();
    expect(new PurchaseUnresolvedError("x").code).toBe("purchase_unresolved");
    expect(new PurchaseUnresolvedError("x").charged).toBe(false);
    expect(new CreditDeliveryPendingError("x").code).toBe("credit_delivery_pending");
    expect(new CreditDeliveryPendingError("x").charged).toBe(true);
    expect(new PurchaseUnresolvedError("x", { charged: true }).charged).toBe(true);
  });
});
