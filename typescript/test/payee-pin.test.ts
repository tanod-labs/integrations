/**
 * Payee pinning and the spend budget, against a real local HTTP server that mimics
 * POST /v1/check/address (no network beyond 127.0.0.1, no real payments: throwaway keys).
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it } from "vitest";
import { x402Client } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import {
  BudgetExceededError,
  DEFAULT_MAX_SPEND_USD,
  InvalidRequestError,
  PayeeMismatchError,
  PaymentError,
  PaymentRequiredError,
  TANOD_PAYEES,
  Tanod,
  TanodError,
  type TanodOptions,
} from "../src/index.js";

const BASE = "eip155:8453";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const TANOD = TANOD_PAYEES[BASE]!;
const ATTACKER = "0x9999999999999999999999999999999999999999";
const A = "0x" + "a".repeat(40);
const CHECK = { schema: "precheck/1", ok: true, chain: "base", address: A, verdict: "low", risk_score: 0, reasons: [] };

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");
const unb64 = (s: string) => JSON.parse(Buffer.from(s, "base64").toString());

function requirement(payTo: string, amount: string, extra: Record<string, unknown> = { name: "USD Coin", version: "2" }) {
  return { scheme: "exact", network: BASE, asset: USDC, amount, payTo, maxTimeoutSeconds: 300, extra };
}

function quote(accepts: unknown[]) {
  return {
    x402Version: 2,
    error: "Payment required",
    resource: { url: "http://127.0.0.1/v1/check/address", description: "test", mimeType: "application/json" },
    accepts,
    extensions: {},
  };
}

interface Seen {
  path: string;
  signature?: string;
}

/** A local server like the one in the report: every unpaid call gets this 402, every paid call a 200. */
class FakeTanod {
  readonly seen: Seen[] = [];
  #server?: Server;
  constructor(
    private readonly accepts: unknown[],
    private readonly paidStatus = 200,
  ) {}

  get signatures(): string[] {
    return this.seen.flatMap((s) => (s.signature ? [s.signature] : []));
  }

  async start(): Promise<string> {
    this.#server = createServer((req: IncomingMessage, res: ServerResponse) => {
      req.resume();
      req.on("end", () => {
        const signature = req.headers["payment-signature"] as string | undefined;
        this.seen.push({ path: req.url ?? "", signature });
        if (!signature) {
          const pr = quote(this.accepts);
          res.writeHead(402, { "content-type": "application/json", "PAYMENT-REQUIRED": b64(pr) });
          res.end(JSON.stringify(pr));
          return;
        }
        const accepted = unb64(signature).accepted;
        const settle = { success: true, transaction: "0x" + "ab".repeat(32), network: BASE, payer: "0x" + "11".repeat(20), amount: accepted.amount };
        res.writeHead(this.paidStatus, { "content-type": "application/json", "PAYMENT-RESPONSE": b64(settle) });
        res.end(JSON.stringify(this.paidStatus === 200 ? CHECK : { error: "boom" }));
      });
    });
    await new Promise<void>((resolve) => this.#server!.listen(0, "127.0.0.1", resolve));
    return `http://127.0.0.1:${(this.#server.address() as AddressInfo).port}`;
  }

  async stop(): Promise<void> {
    if (!this.#server) return;
    this.#server.closeAllConnections();
    await new Promise<void>((resolve) => this.#server!.close(() => resolve()));
  }
}

const servers: FakeTanod[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => s.stop()));
});

const throwaway = () => privateKeyToAccount(generatePrivateKey()); // random, unfunded

async function setup(accepts: unknown[], opts: TanodOptions = {}, paidStatus = 200) {
  const server = new FakeTanod(accepts, paidStatus);
  servers.push(server);
  const baseUrl = await server.start();
  const client = new Tanod({ useEnv: false, signer: throwaway(), maxRetries: 0, timeoutMs: 10_000, baseUrl, ...opts });
  return { server, client };
}

describe("payee pinning", () => {
  it("refuses a swapped payee and signs nothing (three calls, as in the report)", async () => {
    const { server, client } = await setup([requirement(ATTACKER, "500000")]); // USD 0.50, under maxPriceUsd
    for (let i = 0; i < 3; i++) {
      const err = await client.checkAddress(A).catch((e) => e);
      expect(err).toBeInstanceOf(PayeeMismatchError);
      expect(err).toBeInstanceOf(PaymentRequiredError); // existing catch blocks still match
      expect(err.code).toBe("payee_mismatch");
      expect(err.payTo).toBe(ATTACKER);
      expect(err.network).toBe(BASE);
      expect(err.priceUsd).toBe(0.5);
    }
    expect(server.seen).toHaveLength(3);
    expect(server.signatures).toHaveLength(0);
    expect(client.spentUsd).toBe(0);
  });

  it("refuses a swapped payee even without a wallet (instead of quoting it)", async () => {
    const { server, client } = await setup([requirement(ATTACKER, "5000")], { signer: undefined });
    expect(client.hasWallet).toBe(false);
    await expect(client.checkAddress(A)).rejects.toBeInstanceOf(PayeeMismatchError);
    expect(server.signatures).toHaveLength(0);
  });

  it("pays Tanod's published payee normally (EVM compared case-insensitively)", async () => {
    const { server, client } = await setup([requirement(TANOD.toLowerCase(), "5000")]);
    const out = await client.checkAddress(A);
    expect(out.verdict).toBe("low");
    expect(out.meta.payment?.success).toBe(true);
    expect(out.meta.payment?.priceUsd).toBe(0.005);
    expect(server.signatures).toHaveLength(1);
    const sig = unb64(server.signatures[0]!);
    expect(sig.accepted.payTo).toBe(TANOD.toLowerCase());
    expect(sig.accepted.amount).toBe("5000");
    expect(client.spentUsd).toBe(0.005);
  });

  it("hands the x402 client only the vetted requirement", async () => {
    // Honest Base entry first, a second Base entry to another payee after it. A custom
    // paymentClient whose policy prefers the last entry must still end up paying the vetted one.
    const account = throwaway();
    const custom = new x402Client()
      .register(BASE, new ExactEvmScheme(account))
      .registerPolicy((_v, reqs) => reqs.slice(-1));
    const { server, client } = await setup([requirement(TANOD, "5000"), requirement(ATTACKER, "5000")], {
      signer: undefined,
      paymentClient: custom,
    });
    await client.checkAddress(A);
    expect(server.signatures).toHaveLength(1);
    expect(unb64(server.signatures[0]!).accepted.payTo).toBe(TANOD);
  });

  it("refuses to send when a custom payment client signs a different payee", async () => {
    const custom = new x402Client()
      .register(BASE, new ExactEvmScheme(throwaway()))
      .registerPolicy((_v, reqs) => reqs.map((r) => ({ ...r, payTo: ATTACKER })));
    const { server, client } = await setup([requirement(TANOD, "5000")], { signer: undefined, paymentClient: custom });
    const err = await client.checkAddress(A).catch((e) => e);
    expect(err).toBeInstanceOf(PayeeMismatchError);
    expect(err.payTo).toBe(ATTACKER);
    expect(server.signatures).toHaveLength(0);
    expect(client.spentUsd).toBe(0); // nothing left the process, so the reservation was released
  });

  it("payees: null disables pinning (pays as before)", async () => {
    const { server, client } = await setup([requirement(ATTACKER, "500000")], { payees: null });
    const out = await client.checkAddress(A);
    expect(out.meta.payment?.success).toBe(true);
    expect(server.signatures).toHaveLength(1);
    expect(unb64(server.signatures[0]!).accepted.payTo).toBe(ATTACKER);
    expect(client.spentUsd).toBe(0.5);
  });

  it("accepts a flat list or a per-network map of payees", async () => {
    const own = "0x" + "12".repeat(20);
    const flat = await setup([requirement(own, "5000")], { payees: [own.toUpperCase().replace("0X", "0x")] });
    await flat.client.checkAddress(A);
    expect(flat.server.signatures).toHaveLength(1);

    const perNetwork = await setup([requirement(own, "5000")], { payees: { [BASE]: [TANOD, own] } });
    await perNetwork.client.checkAddress(A);
    expect(perNetwork.server.signatures).toHaveLength(1);

    // Allowed on another network only: refused on Base.
    const otherNet = await setup([requirement(own, "5000")], { payees: { "eip155:137": own } });
    await expect(otherNet.client.checkAddress(A)).rejects.toBeInstanceOf(PayeeMismatchError);
    expect(otherNet.server.signatures).toHaveLength(0);
  });

  it("default allow-list is Tanod's published payees", () => {
    expect(TANOD_PAYEES[BASE]).toBe("0x593857A4a4F619543ea12394137C3004ce841720");
    expect(TANOD_PAYEES["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"]).toBe("RMk9k3VC8dr4hhjgFh88rGhjAbzfBzHxJJ5n2fTcqus");
    expect(Object.isFrozen(TANOD_PAYEES)).toBe(true);
  });

  it("rejects an empty or malformed allow-list at construction", () => {
    expect(() => new Tanod({ useEnv: false, payees: [] })).toThrow(InvalidRequestError);
    expect(() => new Tanod({ useEnv: false, payees: {} })).toThrow(InvalidRequestError);
    expect(() => new Tanod({ useEnv: false, payees: { [BASE]: [] } })).toThrow(InvalidRequestError);
    expect(() => new Tanod({ useEnv: false, payees: [""] })).toThrow(InvalidRequestError);
  });
});

describe("spend budget", () => {
  it("defaults to USD 5 per client", async () => {
    expect(DEFAULT_MAX_SPEND_USD).toBe(5);
    const { server, client } = await setup([requirement(TANOD, "500000")]); // USD 0.50 per call
    expect(client.maxSpendUsd).toBe(5);
    for (let i = 0; i < 10; i++) await client.checkAddress(A);
    expect(client.spentUsd).toBe(5);
    const err = await client.checkAddress(A).catch((e) => e);
    expect(err).toBeInstanceOf(BudgetExceededError);
    expect(err).toBeInstanceOf(PaymentRequiredError);
    expect(err.code).toBe("budget_exceeded");
    expect(err.spentUsd).toBe(5);
    expect(err.maxSpendUsd).toBe(5);
    expect(err.priceUsd).toBe(0.5);
    expect(server.signatures).toHaveLength(10);
    expect(server.seen).toHaveLength(21); // 10 x (unpaid + paid) + the refused unpaid call
  });

  it("stops payments once maxSpendUsd is reached", async () => {
    const { server, client } = await setup([requirement(TANOD, "5000")], { maxSpendUsd: 0.012 });
    await client.checkAddress(A);
    await client.checkAddress(A);
    expect(client.spentUsd).toBe(0.01);
    await expect(client.checkAddress(A)).rejects.toBeInstanceOf(BudgetExceededError);
    await expect(client.checkAddress(A)).rejects.toBeInstanceOf(BudgetExceededError);
    expect(server.signatures).toHaveLength(2);
    expect(client.spentUsd).toBe(0.01);
  });

  it("reserves before signing, so concurrent calls cannot overshoot", async () => {
    const { server, client } = await setup([requirement(TANOD, "5000")], { maxSpendUsd: 0.015 });
    const results = await Promise.allSettled(Array.from({ length: 6 }, () => client.checkAddress(A)));
    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(3);
    const refused = results.filter((r) => r.status === "rejected") as PromiseRejectedResult[];
    expect(refused).toHaveLength(3);
    expect(refused.every((r) => r.reason instanceof BudgetExceededError)).toBe(true);
    expect(server.signatures).toHaveLength(3);
    expect(client.spentUsd).toBe(0.015);
  });

  it("maxSpendUsd: null removes the total cap", async () => {
    const { server, client } = await setup([requirement(TANOD, "1000000")], { maxSpendUsd: null }); // USD 1.00
    for (let i = 0; i < 6; i++) await client.checkAddress(A);
    expect(server.signatures).toHaveLength(6);
    expect(client.spentUsd).toBe(6);
  });

  it("releases the reservation when signing fails", async () => {
    const empty = new x402Client(); // no scheme registered: building the payment fails
    const { server, client } = await setup([requirement(TANOD, "5000")], { signer: undefined, paymentClient: empty });
    await expect(client.checkAddress(A)).rejects.toBeInstanceOf(PaymentError);
    expect(server.signatures).toHaveLength(0);
    expect(client.spentUsd).toBe(0);
  });

  it("keeps the reservation when the paid request fails (the payment may still settle)", async () => {
    const { server, client } = await setup([requirement(TANOD, "5000")], {}, 500);
    await expect(client.checkAddress(A)).rejects.toBeInstanceOf(TanodError);
    expect(server.signatures).toHaveLength(1);
    expect(client.spentUsd).toBe(0.005);
  });

  it("rejects a negative or non-numeric budget", () => {
    expect(() => new Tanod({ useEnv: false, maxSpendUsd: -1 })).toThrow(InvalidRequestError);
    expect(() => new Tanod({ useEnv: false, maxSpendUsd: Number.NaN })).toThrow(InvalidRequestError);
  });
});
