# parity-gate: demo-catalog

**Verdict: FAIL**

- Run `20260914T021657Z-7b4a1f` finished 2026-09-14T02:16:57Z
- Baseline `http://127.0.0.1:8799/legacy`
- Candidate `http://127.0.0.1:8799/next`
- 7 case(s): 1 PASS, 2 WARN, 4 FAIL
- 9 breaking contract change(s), 5 value difference(s), 1 unstable endpoint(s)
- Evidence chain head `eee530873a1e5793`

## Cases

| Verdict | Case | Requirement | Risk | Drift | Diffs | Stability |
| --- | --- | --- | --- | --- | --- | --- |
| FAIL | `CAT-001` Product listing keeps its pagination envelope and item contract | REQ-CAT-01 | critical | 5 (3 breaking) | 2 | STABLE |
| PASS | `CAT-002` Product detail is byte-for-byte unchanged (not part of the migration) | REQ-CAT-02 | high | 0 | 0 | STABLE |
| FAIL | `CAT-003` An unknown product id still answers 404, not an empty success | REQ-CAT-03 | high | 3 (2 breaking) | 0 | STABLE |
| FAIL | `SEC-001` Internal cost data is never exposed on the public catalogue | REQ-SEC-01 | critical | 4 (2 breaking) | 2 | STABLE |
| WARN | `OPS-001` Health endpoint answers and reports uptime | REQ-OPS-01 | low | 0 | 0 | VOLATILE_BODY |
| WARN | `OPS-002` Inventory sync status is stable enough to be compared at all | REQ-OPS-02 | medium | 0 | 0 | FLAKY_STATUS |
| FAIL | `GQL-001` GraphQL product query keeps its shape and returns no errors | REQ-GQL-01 | critical | 4 (2 breaking) | 1 | STABLE |

## Findings

### FAIL `CAT-001`: Product listing keeps its pagination envelope and item contract

`GET /products?limit=4` · requirement REQ-CAT-01 · risk critical

**Contract drift**

| Severity | Kind | Path | Detail |
| --- | --- | --- | --- |
| breaking | `PG1003 NULLABLE_ADDED` | `$.products[].discount` | candidate can return null where baseline never did (['number'] -> ['null', 'number']) |
| breaking | `PG1004 TYPE_CHANGED` | `$.products[].price` | incompatible type change (['number'] -> ['string']) |
| breaking | `PG1001 FIELD_REMOVED` | `$.products[].stock` | present in baseline but absent from candidate (was always present) |
| additive | `PG1007 FIELD_ADDED` | `$.products[].internalCost` | new in candidate; harmless for consumers that ignore unknown fields |
| additive | `PG1007 FIELD_ADDED` | `$.products[].warehouseId` | new in candidate; harmless for consumers that ignore unknown fields |

**Value differences**

| Kind | Path | Baseline | Candidate |
| --- | --- | --- | --- |
| `PG2006 ORDER_ONLY` | `$.products` | `[1, 2, 3, 4]` | `[4, 3, 2, 1]` |
| `PG2001 VALUE` | `$.total` | `4` | `3` |

_17 further value difference(s) are folded away: they are restatements of the contract drift listed above._

### FAIL `CAT-003`: An unknown product id still answers 404, not an empty success

`GET /products/999` · requirement REQ-CAT-03 · risk high

**Failed assertions**

- `PG3001 status`: expected [404], got 200
- `PG3002 status_parity`: baseline answered [404], candidate answered [200]

**Contract drift**

| Severity | Kind | Path | Detail |
| --- | --- | --- | --- |
| breaking | `PG1001 FIELD_REMOVED` | `$.code` | present in baseline but absent from candidate (was always present) |
| breaking | `PG1001 FIELD_REMOVED` | `$.error` | present in baseline but absent from candidate (was always present) |
| additive | `PG1007 FIELD_ADDED` | `$.data` | new in candidate; harmless for consumers that ignore unknown fields |

_3 further value difference(s) are folded away: they are restatements of the contract drift listed above._

### FAIL `SEC-001`: Internal cost data is never exposed on the public catalogue

`GET /products?limit=2` · requirement REQ-SEC-01 · risk critical

**Failed assertions**

- `PG3006 forbidden_field`: $.products[].internalCost must not be exposed but is present

**Contract drift**

| Severity | Kind | Path | Detail |
| --- | --- | --- | --- |
| breaking | `PG1004 TYPE_CHANGED` | `$.products[].price` | incompatible type change (['number'] -> ['string']) |
| breaking | `PG1001 FIELD_REMOVED` | `$.products[].stock` | present in baseline but absent from candidate (was always present) |
| additive | `PG1007 FIELD_ADDED` | `$.products[].internalCost` | new in candidate; harmless for consumers that ignore unknown fields |
| additive | `PG1007 FIELD_ADDED` | `$.products[].warehouseId` | new in candidate; harmless for consumers that ignore unknown fields |

**Value differences**

| Kind | Path | Baseline | Candidate |
| --- | --- | --- | --- |
| `PG2006 ORDER_ONLY` | `$.products` | `[1, 2]` | `[2, 1]` |
| `PG2001 VALUE` | `$.total` | `2` | `1` |

_8 further value difference(s) are folded away: they are restatements of the contract drift listed above._

### WARN `OPS-001`: Health endpoint answers and reports uptime

`GET /health` · requirement REQ-OPS-01 · risk low

**Stability (both sides)**: `VOLATILE_BODY` over 3 calls: values changed between identical calls; add the suggested paths to policy.mask_paths to stop diffing noise

Add to `policy.mask_paths` if this movement is expected:

```toml
mask_paths = [
  "$.uptimeSeconds",
]
```

### WARN `OPS-002`: Inventory sync status is stable enough to be compared at all

`GET /inventory/sync-status` · requirement REQ-OPS-02 · risk medium

> parity comparison skipped: candidate is FLAKY_STATUS across 3 identical calls. Stabilise the endpoint before trusting any diff taken from it.

**Stability (candidate)**: `FLAKY_STATUS` over 3 calls: status varied across identical calls: [200, 503]

### FAIL `GQL-001`: GraphQL product query keeps its shape and returns no errors

`POST /graphql` · requirement REQ-GQL-01 · risk critical

**Failed assertions**

- `PG3007 graphql_errors`: the response carries GraphQL errors: Cannot resolve field 'stock' on type 'Product'

**Contract drift**

| Severity | Kind | Path | Detail |
| --- | --- | --- | --- |
| breaking | `PG1004 TYPE_CHANGED` | `$.data.products[].price` | incompatible type change (['number'] -> ['string']) |
| breaking | `PG1001 FIELD_REMOVED` | `$.data.products[].stock` | present in baseline but absent from candidate (was always present) |
| additive | `PG1007 FIELD_ADDED` | `$.data.products[].internalCost` | new in candidate; harmless for consumers that ignore unknown fields |
| additive | `PG1007 FIELD_ADDED` | `$.data.products[].warehouseId` | new in candidate; harmless for consumers that ignore unknown fields |

**Value differences**

| Kind | Path | Baseline | Candidate |
| --- | --- | --- | --- |
| `PG2004 EXTRA_IN_CANDIDATE` | `$.errors` | `None` | `[{"message": "Cannot resolve field 'stock' on type 'Product'"}]` |

_8 further value difference(s) are folded away: they are restatements of the contract drift listed above._

## Traceability

| Requirement | Cases | Worst verdict |
| --- | --- | --- |
| REQ-CAT-01 | `CAT-001` | FAIL |
| REQ-CAT-02 | `CAT-002` | PASS |
| REQ-CAT-03 | `CAT-003` | FAIL |
| REQ-GQL-01 | `GQL-001` | FAIL |
| REQ-OPS-01 | `OPS-001` | WARN |
| REQ-OPS-02 | `OPS-002` | WARN |
| REQ-SEC-01 | `SEC-001` | FAIL |

## Evidence

Every record is hashed together with the hash of the record before it. Run `parity-gate verify <evidence-dir>` to confirm the files on disk still match the chain.
