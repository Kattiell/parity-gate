# BUG-003: Internal cost data is exposed on the public catalogue endpoint

| | |
| --- | --- |
| **Severity** | Critical |
| **Priority** | P0: data exposure, fix before any deploy |
| **Requirement** | REQ-SEC-01 (internal cost is never exposed publicly) |
| **Found by** | `parity-gate` case `SEC-001`, `forbidden_fields` assertion |
| **Environment** | candidate `/next`, demo catalogue build |
| **Evidence** | [`docs/evidence/report.md`](../evidence/report.md), record `SEC-001` |

## Summary

`GET /products` on the candidate returns `internalCost` for every item. The
field is the purchase cost and was never part of the public response. Anyone who
can open the storefront can compute the margin on every product.

## Steps to reproduce

1. `GET {candidate}/products?limit=2` with no authentication.
2. Read `$.products[].internalCost`.

## Expected

`$.products[].internalCost` is absent. The public catalogue carries selling
price, availability and descriptive fields only.

## Actual

```json
{ "id": 1, "price": "19.90", "warehouseId": 7, "internalCost": 8.4 }
```

Reported as a failed `forbidden_field` assertion, plus `FIELD_ADDED` at
`$.products[].internalCost`.

## Impact

- Margin on the full catalogue becomes public: `1 - internalCost / price`.
- Competitors and B2B customers can price against actual cost. The commercial
  damage does not need a breach report to be real.
- The data is already in client caches and CDN edges the moment it ships; a
  later fix does not recall it.

## Analysis

The rewrite serialises the ORM entity rather than an explicit view model, so
every column added to the table becomes a public API field by default. The bug
is not this one field; it is that the endpoint has no allow-list, so the next
column added will be exposed too, silently.

## Suggested fix

1. Remove `internalCost` from the public serialiser now.
2. Replace entity serialisation with an explicit response model listing the
   fields that are public. Fail closed: a new column should be invisible until
   someone adds it deliberately.
3. Keep the `forbidden_fields` assertion in the suite permanently. It costs
   nothing and it is the only check that fails when a *new* internal field
   appears.

## Notes

This class of bug is invisible to positive assertions: every test that checks
"the response contains id, title and price" passes on a response that also
contains the cost. Only a denial assertion catches it.
