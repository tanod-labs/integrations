# Changelog

## 0.3.2

- Docs: shorter README and module notes.

## 0.3.1

- Payment safety: answers that tie up a payment are never retried and never paid again.
  `settlement_unknown` throws `SettlementUnknownError`, `purchase_unresolved` throws
  `PurchaseUnresolvedError`, `credit_delivery_pending` throws `CreditDeliveryPendingError`
  (all `PaymentUnresolvedError`, not `ServiceUnavailableError`). After a payment was sent, an
  error answer with a settlement receipt, a 5xx or 429 whose code the server does not document as
  uncharged, a 2xx that cannot be read, or no answer at all after paying (timeout, dropped
  connection, body read error) throws `PaymentUnresolvedError`. Each carries `payer`, `nonce`,
  `serverMessage`, `code` and `charged`. Before, a 503 after paying started again with a fresh payment.
- After paying, only a 429 or 503 with an uncharged code (for example `busy`, `queue_full`,
  `rate_limited`) is retried with a fresh payment.
- `charged` is `true` for a paid 2xx that cannot be read, or a paid answer whose body read fails,
  only when a successful settlement receipt came back; without one it is `undefined` (the
  server's free-miss 200 has no receipt).
- Error bodies are read up to 64 KiB, then the rest is cancelled.
- `SettlementUnknownError`, `PurchaseUnresolvedError` and `CreditDeliveryPendingError` set their
  own `code` (and `charged`) when constructed directly.

## 0.3.0

- Payee pinning (`payees`) and a per-client spend budget (`maxSpendUsd`).
