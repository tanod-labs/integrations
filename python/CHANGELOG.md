# Changelog

## 0.4.2

- Docs: shorter README and module notes.

## 0.4.1

- Payment safety: answers that tie up a payment are never retried and never paid again.
  `settlement_unknown` raises `SettlementUnknownError`, `purchase_unresolved` raises
  `PurchaseUnresolvedError`, `credit_delivery_pending` raises `CreditDeliveryPendingError`
  (all `PaymentUnresolvedError`, not `ServiceUnavailableError`). After a payment was sent, an
  error answer with a settlement receipt, a 5xx or 429 whose code the server does not document as
  uncharged, a 2xx that cannot be read, or no answer at all after paying (timeout, dropped
  connection) raises `PaymentUnresolvedError`. Each carries `payer`, `nonce`, `server_message`,
  `code` and `charged`. Before, a 503 after paying started again with a fresh payment.
- After paying, only a 429 or 503 with an uncharged code (for example `busy`, `queue_full`,
  `rate_limited`) is retried with a fresh payment.
- `charged` is `True` for a paid 2xx that cannot be read only when a successful settlement receipt
  came back; without one it is `None` (the server's free-miss 200 has no receipt). A body read
  error after paying keeps the status and receipt, as in the TypeScript SDK.
- `asyncio.CancelledError` (a caller's `wait_for` or framework timeout) or another interrupt after
  the payment was sent is re-raised unchanged, after a WARNING log and an exception note
  (Python 3.11+) with `payer` and `nonce` (never the payment signature).

## 0.4.0

- Payee pinning (`payees`) and a per-client spend budget (`max_spend_usd`).
