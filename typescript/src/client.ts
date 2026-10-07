/**
 * Tanod client: one method per route of https://tanod.dev, paid with x402 when needed.
 *
 * Flow per call:
 *  1. Send unpaid; the free daily tier answers 200 when it covers the call.
 *  2. On 402: if the quote says the input would be refused -> InvalidRequestError (nothing signed);
 *     no wallet -> PaymentRequiredError (with price); above maxPriceUsd -> PriceLimitExceededError.
 *     Otherwise sign with the official x402 client (@x402/fetch + @x402/evm) and retry once.
 *  3. 429/503 are retried honouring Retry-After (never charged).
 *  4. The settlement receipt (PAYMENT-RESPONSE) is in `result.meta.payment`.
 */

import { x402Client, x402HTTPClient } from "@x402/fetch";
import type { PaymentRequired } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm";
import { privateKeyToAccount } from "viem/accounts";

import {
  InvalidRequestError,
  NotFoundError,
  PaymentError,
  PaymentRequiredError,
  PriceLimitExceededError,
  RateLimitError,
  ServiceUnavailableError,
  TanodError,
  type QuoteDetails,
} from "./errors.js";
import type * as T from "./types.js";

export const VERSION = "0.1.0";
export const DEFAULT_BASE_URL = "https://tanod.dev";
export const DEFAULT_MAX_PRICE_USD = 1;
export const ENV_PRIVATE_KEY = "TANOD_PRIVATE_KEY";
const BASE_MAINNET = "eip155:8453";
const USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"; // USDC on Base mainnet (6 decimals)
const USDC_DECIMALS = 6;

/** Anything with an address and EIP-712 signTypedData (a viem LocalAccount works). */
export interface EvmSigner {
  readonly address: `0x${string}`;
  signTypedData(message: {
    domain: Record<string, unknown>;
    types: Record<string, unknown>;
    primaryType: string;
    message: Record<string, unknown>;
  }): Promise<`0x${string}`>;
}

export interface TanodOptions {
  /** Hex private key of the paying wallet (USDC on Base). Default: process.env.TANOD_PRIVATE_KEY. Never logged. */
  privateKey?: string;
  /** A viem account or any EIP-712 signer, instead of a raw key. */
  signer?: EvmSigner;
  /** A fully configured x402Client (advanced: custom wallets, policies). Overrides key/signer. */
  paymentClient?: x402Client;
  /** Refuse to sign any single payment above this USD amount (default 1; agentsBulk costs 2). null = no cap. */
  maxPriceUsd?: number | null;
  /** Read TANOD_PRIVATE_KEY from the environment when no key/signer is given (default true). */
  useEnv?: boolean;
  baseUrl?: string;
  /** Retries on 429/503 (default 2). */
  maxRetries?: number;
  /** Give up instead of sleeping longer than this (default 30 s). */
  maxRetryWaitSeconds?: number;
  /** Per-request timeout (default 120 s; scans can take ~90 s). */
  timeoutMs?: number;
  /** Custom fetch (testing, proxies). Default: globalThis.fetch. */
  fetch?: typeof globalThis.fetch;
}

interface Route {
  method: "GET" | "POST";
  path: string;
  body?: Record<string, unknown>;
  kind?: "json" | "text";
}

const ADDR = /^0x[0-9a-fA-F]{40}$/;
const SCAN_ID = /^[A-Za-z0-9_-]{1,128}$/;

function addr(value: string, what = "address"): string {
  if (typeof value !== "string" || !ADDR.test(value)) {
    throw new InvalidRequestError(`${what} must be 0x followed by 40 hex characters, got ${JSON.stringify(value)}`);
  }
  return value;
}

function chain(value: string): T.Chain {
  if (value !== "ethereum" && value !== "base") {
    throw new InvalidRequestError(`chain must be 'ethereum' or 'base', got ${JSON.stringify(value)}`);
  }
  return value;
}

function compact(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== null) out[k] = v;
  return out;
}

function scanOptions(o?: T.ScanOptions): Record<string, boolean> | undefined {
  if (!o) return undefined;
  const opts = compact({
    include_informational: o.includeInformational,
    include_noisy: o.includeNoisy,
    include_dependencies: o.includeDependencies,
  }) as Record<string, boolean>;
  return Object.keys(opts).length ? opts : undefined;
}

function toBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function atomicToUsd(amount?: string): number | undefined {
  if (amount === undefined || !/^\d+$/.test(amount)) return undefined;
  return Number(amount) / 10 ** USDC_DECIMALS;
}

function retryAfterSeconds(res: Response): number | undefined {
  const v = res.headers.get("retry-after");
  if (!v) return undefined;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

function intHeader(res: Response, name: string): number | undefined {
  const v = res.headers.get(name);
  if (v === null) return undefined;
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) ? n : undefined;
}

async function readBody(res: Response): Promise<{ json?: unknown; text: string }> {
  const text = await res.text();
  try {
    return { json: JSON.parse(text), text };
  } catch {
    return { text };
  }
}

function errorText(json: unknown, text: string): string {
  if (json && typeof json === "object") {
    const e = (json as Record<string, unknown>).error ?? (json as Record<string, unknown>).detail;
    if (e && typeof e === "object") {
      const o = e as Record<string, unknown>;
      const msg = String(o.message ?? o.detail ?? JSON.stringify(o).slice(0, 300));
      return o.code ? `${String(o.code)}: ${msg}` : msg;
    }
    if (e) return String(e).slice(0, 500);
  }
  return text.slice(0, 500);
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function envKey(): string | undefined {
  const p = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return p?.env?.[ENV_PRIVATE_KEY] || undefined;
}

class Quote implements QuoteDetails {
  paymentRequired?: PaymentRequired;
  priceUsd?: number;
  amountAtomic?: string;
  asset?: string;
  network?: string;
  payTo?: string;
  reason?: string;
  constructor(
    http: x402HTTPClient,
    res: Response,
    readonly body: unknown,
  ) {
    try {
      this.paymentRequired = http.getPaymentRequiredResponse((n) => res.headers.get(n), body);
    } catch {
      const b = body as PaymentRequired | undefined;
      if (b && Array.isArray(b.accepts) && b.accepts.length) this.paymentRequired = b;
    }
    const accepts = this.paymentRequired?.accepts ?? [];
    const req = accepts.find((a) => a.network === BASE_MAINNET) ?? accepts[0];
    this.amountAtomic = req?.amount;
    this.priceUsd = atomicToUsd(req?.amount);
    this.asset = req?.asset;
    this.network = req?.network;
    this.payTo = req?.payTo;
    let err = this.paymentRequired?.error;
    if (!err && body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string") {
      err = (body as { error: string }).error;
    }
    this.reason = err;
  }
  get inputRefused(): boolean {
    return !!this.reason && this.reason.includes("would be refused");
  }
  details(): QuoteDetails & { body?: unknown } {
    return {
      priceUsd: this.priceUsd,
      amountAtomic: this.amountAtomic,
      asset: this.asset,
      network: this.network,
      payTo: this.payTo,
      reason: this.reason,
      paymentRequired: this.paymentRequired,
      body: this.body,
    };
  }
}

export class Tanod {
  readonly baseUrl: string;
  readonly maxPriceUsd: number | null;
  readonly maxRetries: number;
  readonly maxRetryWaitSeconds: number;
  readonly timeoutMs: number;
  #payer?: x402Client;
  readonly #http: x402HTTPClient;
  readonly #fetch: typeof globalThis.fetch;

  constructor(opts: TanodOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.maxPriceUsd = opts.maxPriceUsd === undefined ? DEFAULT_MAX_PRICE_USD : opts.maxPriceUsd;
    this.maxRetries = Math.max(0, opts.maxRetries ?? 2);
    this.maxRetryWaitSeconds = opts.maxRetryWaitSeconds ?? 30;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
    this.#fetch = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.#payer = Tanod.#buildPayer(opts, this.maxPriceUsd);
    this.#http = new x402HTTPClient(this.#payer ?? new x402Client());
  }

  static #buildPayer(opts: TanodOptions, maxPriceUsd: number | null): x402Client | undefined {
    if (opts.paymentClient) return opts.paymentClient;
    let signer: EvmSigner | undefined = opts.signer;
    if (!signer) {
      let key = opts.privateKey ?? (opts.useEnv === false ? undefined : envKey());
      if (!key) return undefined;
      key = key.trim();
      if (!key.startsWith("0x")) key = `0x${key}`;
      try {
        signer = privateKeyToAccount(key as `0x${string}`);
      } catch {
        // Never echo the key.
        throw new PaymentError(`${ENV_PRIVATE_KEY} / privateKey is not a valid 32-byte hex private key`);
      }
    }
    const client = new x402Client().register(BASE_MAINNET, new ExactEvmScheme(signer));
    client.setSpendControls({ maxAmountPerPayment: maxPriceUsd === null ? false : `$${maxPriceUsd}` });
    return client;
  }

  /** True when a payment signer is configured. */
  get hasWallet(): boolean {
    return this.#payer !== undefined;
  }

  toString(): string {
    return `Tanod(baseUrl=${this.baseUrl}, wallet=${this.hasWallet ? "yes" : "no"})`;
  }

  // -- transport -----------------------------------------------------------

  async #send(route: Route, extra?: Record<string, string>): Promise<Response> {
    const headers: Record<string, string> = {
      accept: "application/json, text/plain, */*",
      "user-agent": `tanod-js/${VERSION}`,
      ...extra,
    };
    if (route.body !== undefined) headers["content-type"] = "application/json";
    return this.#fetch(this.baseUrl + route.path, {
      method: route.method,
      headers,
      body: route.body !== undefined ? JSON.stringify(route.body) : undefined,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
  }

  #waitFor(res: Response, attempt: number): number | undefined {
    if ((res.status !== 429 && res.status !== 503) || attempt >= this.maxRetries) return undefined;
    const wait = retryAfterSeconds(res) ?? Math.min(2 ** attempt, 8);
    return wait > this.maxRetryWaitSeconds ? undefined : wait;
  }

  #checkQuote(q: Quote, path: string): void {
    if (q.inputRefused) throw new InvalidRequestError(q.reason ?? "input refused", { status: 402, body: q.body });
    if (!q.paymentRequired || q.amountAtomic === undefined) {
      throw new TanodError(`402 from ${path} without usable x402 requirements`, { status: 402, body: q.body });
    }
    // Fail closed: this SDK only ever pays USDC on Base mainnet, and only when the price is known,
    // so a malformed or unexpected quote can never slip past the maxPriceUsd cap.
    if (q.network !== BASE_MAINNET || (q.asset ?? "").toLowerCase() !== USDC_BASE) {
      throw new TanodError(
        `${path} quotes an unsupported payment (network=${q.network}, asset=${q.asset}); ` +
          "this SDK only pays USDC on Base mainnet. Nothing was signed",
        { status: 402, body: q.body },
      );
    }
    if (q.priceUsd === undefined || !Number.isFinite(q.priceUsd)) {
      throw new TanodError(`${path} quotes an unparseable amount ${q.amountAtomic}; nothing was signed`, {
        status: 402,
        body: q.body,
      });
    }
    if (!this.#payer) {
      throw new PaymentRequiredError(
        `${path} costs USD ${q.priceUsd} (free daily tier used up or not available). ` +
          "Configure a wallet (TANOD_PRIVATE_KEY, privateKey or signer) to pay with USDC on Base.",
        q.details(),
      );
    }
    if (this.maxPriceUsd !== null && q.priceUsd !== undefined && q.priceUsd > this.maxPriceUsd) {
      throw new PriceLimitExceededError(
        `${path} quotes USD ${q.priceUsd}, above maxPriceUsd=${this.maxPriceUsd}; nothing was signed`,
        q.details(),
      );
    }
  }

  #receipt(res: Response, q?: Quote): T.PaymentReceipt | undefined {
    if (!res.headers.get("payment-response") && !res.headers.get("x-payment-response")) return undefined;
    try {
      const s = this.#http.getPaymentSettleResponse((n) => res.headers.get(n)) as unknown as Record<string, unknown>;
      const amount = (s.amount as string | undefined) ?? q?.amountAtomic;
      return {
        success: Boolean(s.success),
        transaction: String(s.transaction ?? ""),
        network: String(s.network ?? ""),
        payer: s.payer as string | undefined,
        amountAtomic: amount,
        priceUsd: atomicToUsd(amount) ?? q?.priceUsd,
        raw: s,
      };
    } catch {
      return undefined;
    }
  }

  async #call<R>(route: Route): Promise<T.WithMeta<R>> {
    for (let attempt = 0; ; attempt++) {
      let res = await this.#send(route);
      let quote: Quote | undefined;
      if (res.status === 402) {
        quote = new Quote(this.#http, res, (await readBody(res)).json);
        this.#checkQuote(quote, route.path);
        let payHeaders: Record<string, string>;
        try {
          const payload = await this.#http.createPaymentPayload(quote.paymentRequired!);
          payHeaders = this.#http.encodePaymentSignatureHeader(payload);
        } catch (e) {
          throw new PaymentError(`could not build the x402 payment: ${(e as Error).message}`);
        }
        res = await this.#send(route, payHeaders);
        if (res.status === 402) {
          const again = new Quote(this.#http, res, (await readBody(res)).json);
          throw new PaymentRequiredError(`${route.path}: payment refused: ${again.reason ?? "no reason given"}`, again.details());
        }
      }
      const wait = this.#waitFor(res, attempt);
      if (wait !== undefined) {
        await res.body?.cancel().catch(() => undefined);
        await sleep(wait * 1000);
        continue;
      }
      const { json, text } = await readBody(res);
      if (res.status >= 400) this.#throwFor(res, json, text, route.path);
      const meta: T.ResponseMeta = {
        status: res.status,
        url: this.baseUrl + route.path,
        product: res.headers.get("x-tanod-product") ?? undefined,
        freeRemainingToday: intHeader(res, "x-free-remaining-today"),
        scanId: res.headers.get("x-scan-id") ?? undefined,
        cache: res.headers.get("x-cache") ?? undefined,
        reportMarkdownUrl: res.headers.get("x-report-markdown") ?? undefined,
        reportJsonUrl: res.headers.get("x-report-json") ?? undefined,
        payment: this.#receipt(res, quote),
      };
      let data: object;
      if (route.kind === "text") data = { text };
      else if (json && typeof json === "object") data = json as object;
      else throw new TanodError(`${route.path}: expected a JSON object`, { status: res.status, body: text.slice(0, 500) });
      Object.defineProperty(data, "meta", { value: meta, enumerable: false });
      return data as T.WithMeta<R>;
    }
  }

  #throwFor(res: Response, json: unknown, text: string, path: string): never {
    const msg = errorText(json, text);
    const s = res.status;
    if (s === 413 || s === 415 || s === 422) throw new InvalidRequestError(`${path}: ${msg}`, { status: s, body: json ?? text });
    if (s === 404) throw new NotFoundError(`${path}: ${msg}`, { status: s, body: json ?? text });
    if (s === 429)
      throw new RateLimitError(`${path}: rate limited: ${msg}`, { status: s, body: json ?? text, retryAfterSeconds: retryAfterSeconds(res) });
    if (s === 502 || s === 503 || s === 504)
      throw new ServiceUnavailableError(`${path}: temporarily unavailable (${s}, not charged): ${msg}`, {
        status: s,
        body: json ?? text,
        retryAfterSeconds: retryAfterSeconds(res),
      });
    throw new TanodError(`${path}: HTTP ${s}: ${msg}`, { status: s, body: json ?? text });
  }

  // -- pactlint --------------------------------------------------------------

  /** pactlint: scan one Solidity file (`source`) or solc standard JSON. USD 0.25 (<=3k nSLOC) / 0.75 (<=15k); 3 free scans/IP/day. */
  async scanContractSource(input: {
    source?: string;
    standardJson?: Record<string, unknown>;
    filename?: string;
    compilerVersion?: string;
    options?: T.ScanOptions;
  }): Promise<T.WithMeta<T.ContractScanReport>> {
    if ((input.source === undefined) === (input.standardJson === undefined)) {
      throw new InvalidRequestError("give exactly one of source or standardJson");
    }
    return this.#call({
      method: "POST",
      path: "/v1/scan/source",
      body: compact({
        source: input.source,
        standard_json: input.standardJson,
        filename: input.filename,
        compiler_version: input.compilerVersion,
        options: scanOptions(input.options),
      }),
    });
  }

  /** pactlint: scan a verified contract (Sourcify) by address. Unverified: 404, not charged. */
  async scanContractAddress(address: string, chainId: T.Chain = "ethereum", options?: T.ScanOptions): Promise<T.WithMeta<T.ContractScanReport>> {
    return this.#call({
      method: "POST",
      path: "/v1/scan/address",
      body: compact({ address: addr(address), chain: chain(chainId), options: scanOptions(options) }),
    });
  }

  /** Verified source, ABI and compiler settings. USD 0.005; 10 free/IP/day. */
  async getContractSource(chainId: T.Chain, address: string): Promise<T.WithMeta<T.ContractSource>> {
    return this.#call({ method: "GET", path: `/v1/source/${chain(chainId)}/${addr(address)}` });
  }

  // -- txpeek ----------------------------------------------------------------

  /** txpeek: risk verdict for an address you are about to transact with. USD 0.005; 30 free/IP/day. */
  async checkAddress(address: string, chainId: T.Chain = "base"): Promise<T.WithMeta<T.AddressCheck>> {
    return this.#call({ method: "POST", path: "/v1/check/address", body: { address: addr(address), chain: chain(chainId) } });
  }

  // -- toolsniff -------------------------------------------------------------

  /**
   * toolsniff: scan an AI-agent skill or MCP server before installing it.
   * `source`: npm:name[@ver] | pypi:name[==ver] | github:owner/repo[@ref][//subdir] | GitHub URL | clawhub:[owner/]slug[@ver];
   * or `content` (archive or single-file bytes, with `filename`). USD 0.02 (0.05 whole GitHub repo / large upload).
   */
  async scanPackage(input: { source?: string; content?: Uint8Array; filename?: string }): Promise<T.WithMeta<T.PackageScanReport>> {
    if ((input.source === undefined) === (input.content === undefined)) {
      throw new InvalidRequestError("give exactly one of source or content");
    }
    const body =
      input.content !== undefined
        ? compact({ content_base64: toBase64(input.content), filename: input.filename })
        : { source: input.source };
    return this.#call({ method: "POST", path: "/v1/scan/package", body });
  }

  // -- sitepeek / dnspeek ----------------------------------------------------

  /** sitepeek: public URL -> Markdown (or PNG screenshot, base64). USD 0.005 static / 0.01 JS or screenshot; 5 free static/IP/day. */
  async render(
    url: string,
    opts: { format?: "markdown" | "screenshot"; js?: boolean; width?: number; height?: number } = {},
  ): Promise<T.WithMeta<T.RenderResult>> {
    const format = opts.format ?? "markdown";
    if (format !== "markdown" && format !== "screenshot") throw new InvalidRequestError("format must be 'markdown' or 'screenshot'");
    return this.#call({
      method: "POST",
      path: "/v1/render",
      body: compact({ url, format, js: opts.js || undefined, width: opts.width, height: opts.height }),
    });
  }

  /** dnspeek: DNS, email auth (SPF/DMARC/DKIM/MTA-STS) and TLS. USD 0.01 (0.004 one section); 5 free/IP/day. */
  async inspectDomain(domain: string, checks?: ("dns" | "email" | "tls")[]): Promise<T.WithMeta<T.DomainInspection>> {
    if (checks !== undefined && (checks.length === 0 || checks.some((c) => !["dns", "email", "tls"].includes(c)))) {
      throw new InvalidRequestError("checks must be a non-empty subset of dns, email, tls");
    }
    return this.#call({ method: "POST", path: "/v1/domain/inspect", body: compact({ domain, checks }) });
  }

  // -- chainpeek ---------------------------------------------------------------

  /** chainpeek: ENS forward ({name}) or reverse ({address}). USD 0.002. */
  async resolveEns(input: { name?: string; address?: string }): Promise<T.WithMeta<T.EnsResult>> {
    if ((input.name === undefined) === (input.address === undefined)) throw new InvalidRequestError("give exactly one of name or address");
    return this.#call({ method: "POST", path: "/v1/chain/ens", body: compact({ ...input }) });
  }

  /** chainpeek: decode EVM calldata. USD 0.003. */
  async decodeCalldata(calldata: string, signature?: string): Promise<T.WithMeta<T.CalldataDecode>> {
    if (typeof calldata !== "string" || !calldata.startsWith("0x") || calldata.length < 10) {
      throw new InvalidRequestError("calldata must be 0x-prefixed hex with at least a 4-byte selector");
    }
    return this.#call({ method: "POST", path: "/v1/chain/calldata", body: compact({ calldata, signature }) });
  }

  /** chainpeek: ERC-20 metadata. USD 0.003. */
  async tokenInfo(chainId: T.Chain, address: string): Promise<T.WithMeta<T.TokenInfo>> {
    return this.#call({ method: "POST", path: "/v1/chain/token", body: { chain: chain(chainId), address: addr(address) } });
  }

  /** chainpeek: native (or ERC-20 with `token`) balance. USD 0.002. */
  async balance(chainId: T.Chain, address: string, token?: string): Promise<T.WithMeta<T.Balance>> {
    return this.#call({
      method: "POST",
      path: "/v1/chain/balance",
      body: compact({ chain: chain(chainId), address: addr(address), token: token === undefined ? undefined : addr(token, "token") }),
    });
  }

  /** chainpeek: gas price and base fee. USD 0.001. */
  async gasPrice(chainId: T.Chain): Promise<T.WithMeta<T.GasPrice>> {
    return this.#call({ method: "POST", path: "/v1/chain/gas", body: { chain: chain(chainId) } });
  }

  /** chainpeek: latest block header. USD 0.001. (chainpeek: 10 free reads/IP/day, shared.) */
  async latestBlock(chainId: T.Chain): Promise<T.WithMeta<T.BlockHeader>> {
    return this.#call({ method: "POST", path: "/v1/chain/block", body: { chain: chain(chainId) } });
  }

  // -- agentscan ---------------------------------------------------------------

  /** agentscan: FREE aggregate summary of the x402/MCP index. */
  async agentsSummary(): Promise<T.WithMeta<T.AgentsSummary>> {
    return this.#call({ method: "GET", path: "/v1/agents/summary" });
  }

  /** agentscan: filtered, paginated records. USD 0.02; 10 free/IP/day. */
  async agentsQuery(query: T.AgentsQuery = {}): Promise<T.WithMeta<T.AgentsQueryResult>> {
    if (query.page_size !== undefined && (query.page_size < 1 || query.page_size > 100)) {
      throw new InvalidRequestError("page_size must be between 1 and 100");
    }
    return this.#call({ method: "POST", path: "/v1/agents/query", body: compact({ ...query }) });
  }

  /** agentscan: price/presence history of one record ({source,id} or {url}). USD 0.05; 5 free/IP/day. */
  async agentsHistory(input: { source?: string; id?: string; url?: string }): Promise<T.WithMeta<T.AgentsHistoryResult>> {
    if (input.url === undefined && (input.source === undefined || input.id === undefined)) {
      throw new InvalidRequestError("give source and id together, or url");
    }
    return this.#call({ method: "POST", path: "/v1/agents/history", body: compact({ ...input }) });
  }

  /** agentscan: filtered slice (<= 5,000 rows) as JSON. USD 0.25; no free tier. */
  async agentsExport(query: T.AgentsExportQuery = {}): Promise<T.WithMeta<T.AgentsExport>> {
    return this.#call({ method: "POST", path: "/v1/agents/export", body: compact({ ...query, format: "json" }) });
  }

  /** agentscan: filtered slice as CSV text. USD 0.25; no free tier. */
  async agentsExportCsv(query: T.AgentsExportQuery = {}): Promise<T.WithMeta<T.TextBody>> {
    return this.#call({ method: "POST", path: "/v1/agents/export", body: compact({ ...query, format: "csv" }), kind: "text" });
  }

  /** agentscan: full snapshot. USD 2.00 (raise maxPriceUsd); no free tier. */
  async agentsBulk(source?: string): Promise<T.WithMeta<T.AgentsBulk>> {
    return this.#call({ method: "POST", path: "/v1/agents/bulk", body: compact({ source }) });
  }

  // -- shared ------------------------------------------------------------------

  /** A stored pactlint/toolsniff report as JSON (kept 30 days, free). */
  async getReport(scanId: string): Promise<T.WithMeta<Record<string, unknown>>> {
    if (!SCAN_ID.test(scanId)) throw new InvalidRequestError("scanId must be alphanumeric (with - or _)");
    return this.#call({ method: "GET", path: `/v1/report/${scanId}.json` });
  }

  /** A stored report as Markdown text (free). */
  async getReportMarkdown(scanId: string): Promise<T.WithMeta<T.TextBody>> {
    if (!SCAN_ID.test(scanId)) throw new InvalidRequestError("scanId must be alphanumeric (with - or _)");
    return this.#call({ method: "GET", path: `/v1/report/${scanId}.md`, kind: "text" });
  }

  /** Liveness (free). */
  async health(): Promise<T.WithMeta<Record<string, unknown>>> {
    return this.#call({ method: "GET", path: "/healthz" });
  }
}
