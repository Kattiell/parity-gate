# Suite reference

One TOML file per suite: two targets, a safety policy, the cases. This page is
the field list. The guided path that gets you a working suite is in
[QUICKSTART.md](QUICKSTART.md).

## The file

```toml
name = "catalog"

[targets.baseline]
snapshot = "contracts/catalog.json"     # a recording...
# base_url = "https://api-v1.example.com"   # ...or the old service, for a migration

[targets.candidate]
base_url = "https://api.staging.example.com"
auth = "env:PARITY_TOKEN"               # the variable name, never the value

[policy]
allowed_hosts = [".staging.example.com"]   # mandatory; no implicit allow-all
allow_mutations = false
repeats = 3                             # samples per endpoint, for stability
max_retries = 0                         # a retried 503 hides the flakiness
workers = 1                             # raise it deliberately; it is someone's staging
timeout_seconds = 10                    # a deadline for the whole response, not per read
max_response_bytes = 10485760           # a runaway body is refused, not loaded
repeat_interval_ms = 0                  # space the samples out to see slower flakiness
max_waiver_days = 90                    # no waiver may be dated further out than this
mask_paths = ["$.meta.requestId", "$.*.updatedAt"]

[policy.array_keys]
"$.products" = "id"                     # compare by identity, not position

[[cases]]
id = "CAT-001"
requirement = "REQ-CAT-01"              # feeds the traceability matrix
title = "Product listing keeps its pagination envelope"
risk = "critical"
method = "GET"
path = "/products?limit=4"
required_fields = ["$.products", "$.total"]
forbidden_fields = ["$.products[].internalCost"]
max_latency_ms = 2000
```

Paths are written as `$.products[].id`. A mask written with `[]` covers every
item, whether the tool addressed it as `[0]` or `[id=7]`.

## GraphQL cases

A GraphQL endpoint is JSON over HTTP, so a case looks the same with one extra
field:

```toml
[[cases]]
id = "GQL-001"
path = "/graphql"
graphql = """
query Products { products { id title price stock } }
"""
```

The operation type is read from the document rather than from the method, so a
`query` runs as a read and a `mutation` still needs `mutating = true` and
`--allow-mutations`. Because every GraphQL response is `200`, the `errors`
array is checked explicitly. Subscriptions are refused at load time.

## Command-line flags

`record`, `run` and `stability` all take:

| Flag | What it does |
| --- | --- |
| `--filter PATTERN` | run a subset: a glob against a case id or requirement, or a substring of the title. Repeatable. `--filter 'CAT-*'`, `--filter REQ-SEC-01`, `--filter orders` |
| `--keep N` | keep only the N most recent evidence bundles and delete the rest. Off by default; only directories carrying a manifest are ever touched |
| `--sarif PATH` | `run` and `stability`: also write the SARIF log to a fixed path, for an upload step |
| `--revision SHA` | `run` and `stability`: the revision under test, for `history`; read from the CI environment when omitted |

## Accepting a finding on the record

Sometimes a finding is known and accepted for a while: a field retired on
purpose, a consumer already migrated. Deleting the assertion removes the check
along with the noise, and running without `--strict` forever hides the next
real problem. A waiver says it instead, in the suite, where it is reviewed like
code:

```toml
[[waivers]]
rule = "PG1001"                          # or its name, FIELD_REMOVED
case = "CAT-*"                           # glob over case ids (default: all)
path = "$.products[].stock"              # glob over the finding path (default: all)
owner = "storefront-team"
expires = 2026-10-31
ticket = "CAT-4412"
reason = "stock moved to the availability endpoint; storefront v5 no longer reads it"
```

- A waived finding still appears in every report, and in SARIF as a suppressed
  alert carrying the justification. A case whose only failures are waived ends
  as `WARN`, never `PASS`.
- **After `expires` it waives nothing**, and the run names the lapsed waiver
  and its owner. A waiver that matched no finding is listed as unused.
- `owner`, `expires` and `reason` are required, and a date beyond
  `policy.max_waiver_days` (90 by default) is refused.
- Only findings about the API can be waived. An unstable endpoint, or a case
  that could not be judged, is not something to accept; it is something to fix.
