# Changelog

## 0.4.0 (unreleased)

- Requires `tanod>=0.4.1,<0.5`.
- `PaymentUnresolvedError` (a payment sent, outcome unresolved) is reported as
  `Payment sent, outcome unresolved ...: do NOT retry or pay again`, with the code, whether it was
  charged, payer and nonce. Before, it read as a generic Tanod error.
- Other unexpected errors read `Tanod error (charge unknown)`.
