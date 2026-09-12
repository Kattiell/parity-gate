# BUG-002 — A missing product answers `200 {"data": null}` instead of `404`

| | |
| --- | --- |
| **Severity** | High |
| **Priority** | P1 — blocks the migration |
| **Requirement** | REQ-CAT-03 (unknown ids still answer 404) |
| **Found by** | `parity-gate` case `CAT-003`, status assertion + contract drift |
| **Environment** | candidate `/next` vs baseline `/legacy`, demo catalogue build |
| **Evidence** | [`docs/evidence/report.md`](../evidence/report.md), record `CAT-003` |

## Summary

`GET /products/{id}` for an id that does not exist returns `200` with a null
payload in the candidate. The baseline returns `404` with an error body. Every
consumer's "not found" branch stops being reachable.

## Steps to reproduce

1. `GET {baseline}/products/999` → `404`
2. `GET {candidate}/products/999` → `200`

## Expected

```
HTTP/1.1 404 Not Found
{ "error": "product not found", "code": "PRODUCT_NOT_FOUND" }
```

## Actual

```
HTTP/1.1 200 OK
{ "data": null }
```

Reported as a failed `status` assertion (`expected [404], got 200`) plus
`FIELD_REMOVED` / `breaking` on `$.error` and `$.code`.

## Impact

- Client code branches on the status. A `200` means "found", so the not-found
  path never runs: the UI renders an empty product page instead of the
  not-found screen.
- Caches and CDNs store `200` responses. A deleted product stays "available"
  for the whole TTL.
- Retry and alerting logic keyed on 4xx rates goes blind: the error disappears
  from every dashboard while still being an error.
- `code` was the machine-readable discriminator. Its removal breaks any consumer
  that distinguished `PRODUCT_NOT_FOUND` from other 404s.

## Analysis

The new handler catches the lookup miss and returns an envelope rather than
letting it become a status. It reads as defensive — "don't throw on a missing
record" — but HTTP already has the vocabulary, and replacing a status with a
body moves the decision to a place no client is looking.

## Suggested fix

Restore `404` with the original body, `code` included. If an envelope is wanted
for successful responses, introduce it for those and keep error semantics in the
status line.

## Notes

The value diff for this case is empty, because the whole *shape* changed. That
is exactly the case a body-only comparison misses and a status assertion catches,
which is why both run.
