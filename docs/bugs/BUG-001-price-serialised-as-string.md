# BUG-001 — `price` is returned as a string by the rewritten catalogue

| | |
| --- | --- |
| **Severity** | Critical |
| **Priority** | P1 — blocks the migration |
| **Requirement** | REQ-CAT-01 (product listing keeps its item contract) |
| **Found by** | `parity-gate` case `CAT-001`, contract-drift check |
| **Environment** | candidate `/next` vs baseline `/legacy`, demo catalogue build |
| **Evidence** | [`docs/evidence/report.md`](../evidence/report.md), record `CAT-001`, run chain `4a238a62…` |

## Summary

Every `price` in `GET /products` comes back as a JSON string in the candidate,
where the baseline returns a number. Consumers that read the field arithmetically
fail or, worse, succeed with the wrong answer.

## Steps to reproduce

1. `parity-gate demo` (starts both variants and runs the suite), or by hand:
2. `GET {baseline}/products?limit=4`
3. `GET {candidate}/products?limit=4`
4. Compare the type of `$.products[].price` between the two.

## Expected

`$.products[].price` is a JSON number, as in the baseline:

```json
{ "id": 1, "price": 19.9 }
```

## Actual

```json
{ "id": 1, "price": "19.90" }
```

Reported as `TYPE_CHANGED` / `breaking` at `$.products[].price`, `['number'] -> ['string']`.

## Impact

- Typed clients (mobile, partner SDKs) throw on deserialisation. The failure is
  at parse time, so the whole page fails, not one field.
- Untyped clients are worse: JavaScript evaluates `"19.90" + 5` as `"19.905"`.
  A cart total that is wrong silently is more expensive than one that crashes.
- The field is money, so anything downstream of it is financial.

## Analysis

The new serialiser formats decimals for presentation (`f"{value:.2f}"`) inside
the API layer instead of at the edge that renders them. Formatting is a
presentation concern; the API contract is a data concern.

## Suggested fix

Return the numeric value and let each consumer format it. If a fixed-precision
representation is genuinely required — and for money it often is — it belongs in
a new, additive field (`priceFormatted`) alongside the numeric one, with the
change versioned and announced, not substituted in place.

## Notes

Caught by type comparison across three samples of both services. No expected
value was written by hand, so this could not have been missed by a test that
merely asserted "price is present".
