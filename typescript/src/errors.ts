/**
 * Errors thrown by the Tanod client. None of them means you were charged, except
 * `PaymentUnresolvedError` and its subclasses: a payment was sent and must be reconciled.
 */

export class TanodError extends Error {
  readonly status?: number;
  readonly body?: unknown;
  constructor(message: string, opts: { status?: number; body?: unknown } = {}) {
    super(message);
    this.name = new.target.name;
    this.status = opts.status;
    this.body = opts.body;
  }
}

export interface QuoteDetails {
  /** Quoted price in USD (USDC, 6 decimals). */
  priceUsd?: number;
  /** Quoted amount in USDC atomic units. */
  amountAtomic?: string;
  asset?: string;
  network?: string;
  payTo?: string;
  /** Server-provided reason, e.g. `price_mismatch`. */
  reason?: string;
  /** Decoded x402 v2 PaymentRequired object. */
  paymentRequired?: unknown;
}

/** The call needs payment and the client could not (no wallet) or the server refused the payment. */
export class PaymentRequiredError extends TanodError implements QuoteDetails {
  readonly priceUsd?: number;
  readonly amountAtomic?: string;
  readonly asset?: string;
  readonly network?: string;
  readonly payTo?: string;
  readonly reason?: string;
  readonly paymentRequired?: unknown;
  constructor(message: string, q: QuoteDetails & { body?: unknown } = {}) {
    super(message, { status: 402, body: q.body });
    this.priceUsd = q.priceUsd;
    this.amountAtomic = q.amountAtomic;
    this.asset = q.asset;
    this.network = q.network;
    this.payTo = q.payTo;
    this.reason = q.reason;
    this.paymentRequired = q.paymentRequired;
  }
}

/** The quote is above `maxPriceUsd`; nothing was signed. */
export class PriceLimitExceededError extends PaymentRequiredError {}

/**
 * The quote names a payee (`payTo`) that is not on the client's allow-list (`payees`, by default
 * Tanod's own published payees); nothing was signed. `payTo` and `network` hold the quoted values.
 * This usually means `baseUrl` points somewhere other than Tanod.
 */
export class PayeeMismatchError extends PaymentRequiredError {
  readonly code = "payee_mismatch" as const;
}

/** Paying this quote would take the client's total past `maxSpendUsd`; nothing was signed. */
export class BudgetExceededError extends PaymentRequiredError {
  readonly code = "budget_exceeded" as const;
  /** USD already committed by this client (signed payments) before this quote. */
  readonly spentUsd: number;
  /** The client's budget. */
  readonly maxSpendUsd: number;
  constructor(message: string, q: QuoteDetails & { body?: unknown; spentUsd: number; maxSpendUsd: number }) {
    super(message, q);
    this.spentUsd = q.spentUsd;
    this.maxSpendUsd = q.maxSpendUsd;
  }
}

/** Building or signing the x402 payment failed (or the signed payload did not match the vetted quote); nothing was sent. */
export class PaymentError extends TanodError {}

/** Input refused (422/413/415, a 402 saying the input would be refused, or client-side validation). */
export class InvalidRequestError extends TanodError {}

/** 404: unverified contract, unknown package/record or report. */
export class NotFoundError extends TanodError {}

/** 429 after retries. */
export class RateLimitError extends TanodError {
  readonly retryAfterSeconds?: number;
  constructor(message: string, opts: { status?: number; body?: unknown; retryAfterSeconds?: number } = {}) {
    super(message, opts);
    this.retryAfterSeconds = opts.retryAfterSeconds;
  }
}

/** 502/503/504 after retries (busy queue, chain or facilitator unavailable). Never charged: answers that may be charged throw `PaymentUnresolvedError`. */
export class ServiceUnavailableError extends TanodError {
  readonly retryAfterSeconds?: number;
  constructor(message: string, opts: { status?: number; body?: unknown; retryAfterSeconds?: number } = {}) {
    super(message, opts);
    this.retryAfterSeconds = opts.retryAfterSeconds;
  }
}

export interface UnresolvedDetails {
  status?: number;
  body?: unknown;
  /** The server's error code (`settlement_unknown`, ...); undefined when the answer had none. */
  code?: string;
  /** The server's error message. */
  serverMessage?: string;
  /** true: settled (see `receipt`); false: this payment was not charged; undefined: unknown. */
  charged?: boolean;
  /** The paying wallet (the signed authorization's `from`). */
  payer?: string;
  /** The signed EIP-3009 authorization nonce (this client sends no x402 payment-identifier). */
  nonce?: string;
  /** The PAYMENT-SIGNATURE header that was sent. */
  paymentSignature?: string;
  priceUsd?: number;
  amountAtomic?: string;
  /** The decoded settlement receipt (PAYMENT-RESPONSE), when the answer carried one. */
  receipt?: unknown;
  /** The body's `settlement` object, when present. */
  settlement?: unknown;
}

/**
 * A payment for this request was sent and its outcome must be reconciled. Do NOT pay again.
 * The client never retries this and never signs a new payment for it. Keep `payer`, `nonce` and
 * `serverMessage` for reconciliation with ops@tanod.dev. Thrown as is after a payment was sent for:
 * an error answer that carries a settlement receipt; a 5xx or 429 whose code the server does not
 * document as uncharged (or no code); no answer at all (timeout, dropped connection, body read
 * error); a 2xx that cannot be read. The subclasses cover the server's documented codes. Not a
 * `ServiceUnavailableError`.
 */
export class PaymentUnresolvedError extends TanodError {
  readonly code?: string;
  readonly serverMessage?: string;
  readonly charged?: boolean;
  readonly payer?: string;
  readonly nonce?: string;
  readonly paymentSignature?: string;
  readonly priceUsd?: number;
  readonly amountAtomic?: string;
  readonly receipt?: unknown;
  readonly settlement?: unknown;
  constructor(message: string, d: UnresolvedDetails = {}) {
    super(message, { status: d.status, body: d.body });
    this.code = d.code;
    this.serverMessage = d.serverMessage;
    this.charged = d.charged;
    this.payer = d.payer;
    this.nonce = d.nonce;
    this.paymentSignature = d.paymentSignature;
    this.priceUsd = d.priceUsd;
    this.amountAtomic = d.amountAtomic;
    this.receipt = d.receipt;
    this.settlement = d.settlement;
  }
}

/** 503 `settlement_unknown`: the settlement was submitted and its outcome is unconfirmed. You may have been charged. */
export class SettlementUnknownError extends PaymentUnresolvedError {
  constructor(message: string, d: UnresolvedDetails = {}) {
    super(message, { ...d, code: d.code ?? "settlement_unknown" });
  }
}

/**
 * 409 `purchase_unresolved`: an identical earlier purchase from this wallet is unresolved. This
 * payment was verified but not submitted or charged (`charged: false`). Do not pay again for this
 * request until it is reconciled; a different input is a new purchase.
 */
export class PurchaseUnresolvedError extends PaymentUnresolvedError {
  constructor(message: string, d: UnresolvedDetails = {}) {
    super(message, { ...d, code: d.code ?? "purchase_unresolved", charged: d.charged ?? false });
  }
}

/** 503 `credit_delivery_pending`: the payment is confirmed but prepaid credit delivery is pending (`charged: true`). */
export class CreditDeliveryPendingError extends PaymentUnresolvedError {
  constructor(message: string, d: UnresolvedDetails = {}) {
    super(message, { ...d, code: d.code ?? "credit_delivery_pending", charged: d.charged ?? true });
  }
}
