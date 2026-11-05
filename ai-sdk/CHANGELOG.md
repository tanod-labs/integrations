# Changelog

## 0.3.0 (unreleased)

- Requires `@tanod-labs/sdk` ^0.3.1.
- A payment whose outcome is unresolved (`PaymentUnresolvedError`) returns
  `"error": "payment_unresolved"` with `charged`, `code`, `payer` and `nonce`. Before, it was
  reported as `"charged": false`.
- `charged` is `null` (unknown) on an unexpected `error`.
