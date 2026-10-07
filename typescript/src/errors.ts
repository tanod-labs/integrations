/** Errors thrown by the Tanod client. None of them means you were charged. */

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

/** Building or signing the x402 payment failed; nothing was sent. */
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

/** 502/503/504 after retries (busy queue, chain or facilitator unavailable). */
export class ServiceUnavailableError extends TanodError {
  readonly retryAfterSeconds?: number;
  constructor(message: string, opts: { status?: number; body?: unknown; retryAfterSeconds?: number } = {}) {
    super(message, opts);
    this.retryAfterSeconds = opts.retryAfterSeconds;
  }
}
