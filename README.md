# parity-gate

**Your API changed. Did it break anyone?**

A CI gate that answers that in seconds, for any JSON API — no second service, no
hand-written schema, no golden files to re-record. It records the shape your API
has today, then fails the build when a deploy changes it in a way existing
consumers cannot survive.

[![CI](https://github.com/Kattiell/parity-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/Kattiell/parity-gate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

```
$ parity-gate demo --mode contract

suite  demo-contract-guard  (4 cases, 3 repeats)
  baseline  recorded contract contracts/demo-catalog.json
  candidate https://api.example.com

  FAIL  CAT-001   Product listing keeps the shape its consumers w...  - 3 breaking drift
  PASS  CAT-002   Product detail is unchanged
  FAIL  CAT-003   An unknown product id keeps answering the way i...  - 2 breaking drift; failed: recorded_status
  FAIL  SEC-001   No internal cost on the public catalogue            - 2 breaking drift; failed: forbidden_field

verdict   FAIL
cases     4 - 1 PASS, 3 FAIL
findings  7 breaking drift, 0 value diff, 0 unstable
```

`CAT-003` has no `expect_status` written anywhere. The contract remembers that
endpoint used to answer `404`; the deploy made it answer `200` with an empty
body, and the gate caught it without anyone having predicted that specific
failure.

---

## The problem

APIs rarely break because a test failed. They break because the shape moved and
nothing was watching. A field becomes a string, a never-null value comes back
null, a `404` quietly becomes a `200` — and the consumers find out in
production. Contract drift is consistently named the top non-bug API failure in
enterprise systems, and automated detection is what takes time-to-detection from
weeks down to minutes.

Almost every team knows this. Almost none check it, for three reasons:

1. **Writing the contract down is work nobody schedules.** A JSON Schema per
   endpoint, maintained by hand, forever.
2. **Golden files rot.** Record the responses, and three weeks later the suite
   fails because a product was renamed. After enough of those, people re-record
   without reading the diff, and the suite is a rubber stamp.
3. **Live comparison drowns in noise.** Timestamps differ on every call, pages
   come back reordered, one flaky endpoint poisons the whole report. Those
   harnesses get switched off within a quarter.

`parity-gate` exists because of 2 and 3. **The engineering is in the
signal-to-noise ratio**, not in the diff.

## Three things it does

### 1. Gate every deploy against the shape you already ship

The common case: one API, real consumers, no rewrite in sight.

```bash
cp suites/example-api.toml suites/my-api.toml  # a commented starter suite
parity-gate record --suite suites/my-api.toml  # once — captures today's shape
git add suites/my-api.toml suites/contracts/   # review the contract, commit it
parity-gate run --suite suites/my-api.toml     # every deploy, in CI
```

The recorded contract holds **types, requiredness and the status codes each
endpoint answered with** — never values. That is the whole trick against
golden-file rot: your catalogue changes daily, its *shape* does not. A recorded
contract stays valid until someone actually changes the API, which is exactly
when you want to hear about it.

It is written one field per line so the diff is the review:

```diff
-  {"path": "$.products[].price", "types": ["number"], "required": true},
+  {"path": "$.products[].price", "types": ["string"], "required": true},
-  {"path": "$.products[].stock", "types": ["integer"], "required": true},
```

### 2. Find out which endpoints you can't trust yet

Before writing a single assertion against an API:

```
$ parity-gate stability --suite suites/api.toml

sampling demo-catalog (6 cases x 3 calls)

  PASS  CAT-001   Product listing keeps its pagination envelope a...
  PASS  CAT-002   Product detail is byte-for-byte unchanged (not ...
  WARN  OPS-001   Health endpoint answers and reports uptime          - volatile_body
  FAIL  OPS-002   Inventory sync status is stable enough to be co...  - flaky_status

verdict   FAIL
stability 1 unstable, 1 merely noisy

Noise found. Add to [policy] in the suite so it stops being reported:
mask_paths = [
  "$.uptimeSeconds",
]
```

Timing and test data are behind the large majority of flaky failures, and the
usual response is to retry until it goes green. This calls each endpoint N times
and classifies what varied, so the two get separated:

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `STABLE` | Same status, shape and bytes | Trust anything built on it |
| `VOLATILE_BODY` | Values move, shape holds | Noisy, not broken — **the exact masks are printed for you** |
| `FLAKY_SHAPE` | The structure itself varies | Fix before writing tests against it |
| `FLAKY_STATUS` | The status code varies | Fix first; nothing else from this endpoint means anything |

No assertions are evaluated here. It measures the endpoints, not your
expectations.

### GraphQL, without lying about what is a read

A GraphQL endpoint is JSON over HTTP, so everything above applies — but two
things HTTP gives for free are missing, and both need handling rather than
ignoring.

```toml
[[cases]]
id = "GQL-001"
path = "/graphql"
graphql = """
query Products { products { id title price stock } }
"""
```

**Every operation is a POST.** If the write gate went by method, every query
would have to be declared a mutation, and a safety control everyone switches
off protects nothing. The operation type is read from the document instead: a
`query` runs as a read, a `mutation` still needs `mutating = true` *and*
`--allow-mutations`.

**Every response is 200**, including the failures — so the check that catches a
`404` softened into a `200` has nothing to work with here. The equivalent
signal is the `errors` array, and it is checked explicitly:

```
FAIL  GQL-001   Product query keeps its shape   - 2 breaking drift; failed: graphql_errors

- `graphql_errors`: the response carries GraphQL errors:
  Cannot resolve field 'stock' on type 'Product'

| breaking | TYPE_CHANGED  | $.data.products[].price | ['number'] -> ['string'] |
| breaking | FIELD_REMOVED | $.data.products[].stock | was always present       |
```

A status-based check would have called that run green.

### Already have an OpenAPI spec?

Then the endpoint list is written down already, and typing it into TOML is the
largest piece of friction in adopting any gate:

```bash
parity-gate import-openapi --spec https://api.example.com/openapi.json \
                           --out suites/api.toml
```

Reads JSON (what FastAPI, Spring and Swagger UI serve live), emits a case per
safe operation, uses the `example` from each path parameter, and **comments out
the cases it cannot fill in rather than inventing an id** — a case that fails
for the wrong reason is worse than one that does not run. Writes are left out
unless you pass `--include-writes`.

### 3. Prove a rewrite matches the service it replaces

The migration case: two live services, same requests, honest comparison.

```bash
parity-gate run --suite suites/migration.toml   # baseline = old, candidate = new
```

Here it also diffs *values*, because a live baseline is an oracle no hand-written
expectation can match — it catches the pagination counter that says 3 while
returning 4 items.

---

## Try it — offline, one second

```bash
git clone https://github.com/Kattiell/parity-gate
cd parity-gate
pip install -e ".[dev]"

parity-gate demo --mode contract         # gate against a recorded contract
parity-gate demo --mode stability        # measure the noise
parity-gate demo                         # differential, two live services
pytest -q                                # 213 tests, no network
```

All of it runs against a bundled mock that serves a catalogue API twice — once
as it was, once as rewritten with defects planted on purpose. Nothing leaves the
machine.

### Against a real API over the network

```bash
parity-gate run --suite suites/live-self-parity.toml
```

That one compares a live public API **against itself**. It should come back all
green, and that is the point: it is the control experiment. Whatever it reports
as volatile is noise you would otherwise have chased.

## Point it at your own API — about five minutes

The demo proves the tool works. This is the part that makes it yours. There is
no code to write: the whole configuration is one TOML file.

```bash
cp suites/example-api.toml suites/my-api.toml
```

[`suites/example-api.toml`](suites/example-api.toml) is a commented starter that
works unchanged against a public demo API, so you can run the full cycle once
before trusting it with anything of yours. Three lines to change, all marked in
the file: where the contract file goes, your `base_url`, and `allowed_hosts`.

**1. Find out what you can trust before you assert anything.**

```bash
parity-gate stability --suite suites/my-api.toml
```

It calls each endpoint three times and tells you which ones answer differently
to identical calls. If it prints a `mask_paths` block, paste it into `[policy]`:
those are the fields that move on their own, and diffing them is how a gate
becomes noise. Fix anything reported `FLAKY_STATUS` before going further —
nothing measured from an endpoint like that means anything.

**2. Record the shape it has today.**

```bash
parity-gate record --suite suites/my-api.toml
```

Read the file it writes. It is one field per line precisely so you can: this is
the moment to notice that something you thought was required is optional, or
that a field you meant to remove last quarter is still there. Then commit it.

**3. Gate on it.**

```bash
parity-gate run --suite suites/my-api.toml --strict
```

Exit code `2` stops a pipeline. In CI:

```yaml
- name: API contract gate
  run: parity-gate run --suite suites/my-api.toml --strict --keep 20
  env:
    MY_API_TOKEN: ${{ secrets.STAGING_TOKEN }}
```

**4. When it fails, read `report.md` in the evidence directory** — it names the
path, the severity, and what a consumer would experience. If the change was
intended, re-record and commit the new contract; the diff is the review.

Working on one endpoint? `--filter 'PROD-*'` or `--filter REQ-CATALOG-01`
narrows the run without editing the suite.

## How the noise is handled

This is the part that decides whether a gate is still switched on in six months.

**Volatile paths are masked, and the masks are discovered for you.** Request ids
and timestamps differ by definition. The tool samples, sees what moved, and
prints the `mask_paths` block ready to paste.

**Collections are matched by identity, not position.** Tell it
`"$.products" = "id"` and item `id=7` is compared with item `id=7`. Without it, a
reordered page of four products reports a difference on every field of every
item and the one real regression is invisible.

**Reordering is one finding.** Not one per index.

**Stability is measured before anything is compared.** A diff taken from a flaky
endpoint is not evidence of anything, so the tool refuses to produce one and
says why.

**Connections are reused.** A suite sampling each endpoint three times against
two targets used to pay for a TLS handshake every single call. Measured against
a live HTTPS API, same network, same minute:

```
12 calls, connection reused : 0.20s   (16 ms/call)
12 calls, fresh socket each : 1.25s  (104 ms/call)
```

The demo run makes 36 requests over **one** TCP connection, which a test
asserts by counting sockets at the server rather than taking the client's word
for it.

**Redundant findings are folded.** Four products whose `price` changed type is
one contract-drift finding, not four value differences. The count of what was
folded is reported, so nothing vanishes silently.

**An empty collection is "not checked", not "removed".** When today's filter
matches nothing, the item fields were not deleted — nothing was learned about
them. Reporting that as a breaking change is how a gate gets switched off in
its first week, so those paths are listed separately instead.

**A configuration that could not be honoured says so.** Ask for identity
matching on a collection whose key is not unique on both sides and you get a
`KEY_MATCH_UNAVAILABLE` finding, not a silent fall back to positional
comparison and noise you would have blamed on the API.

**Drift is classified by consumer impact, not by "changed".**

| Severity | Example | Why |
| --- | --- | --- |
| `breaking` | `number` → `string`, a required field removed, a field that can now be `null` | Existing consumers fail |
| `risky` | `integer` → `number`, a type narrowed | Usually survivable, worth a look |
| `additive` | A new field | Ignored by well-behaved clients |

## Evidence, not opinions

Every run writes a bundle:

| File | What it is |
| --- | --- |
| `report.md` | For the ticket |
| [`report.html`](docs/evidence/report.html) | Self-contained, light/dark, for the CI artifact |
| `run.json` | Every finding, every redacted exchange, both inferred schemas |
| `manifest.json` | SHA-256 per file plus the chain head |

Each record is hashed together with the hash of the record before it, so a
report edited after the fact is detectable. A real bundle is committed under
[`docs/evidence/`](docs/evidence/) and verifies:

```bash
parity-gate verify docs/evidence
```

Cases carry a `requirement` id, which produces a traceability matrix in every
report — requirement → cases → worst verdict.

## Writing a suite

One TOML file. Two targets, a safety policy, the cases.

```toml
name = "catalog"

[targets.baseline]
snapshot = "contracts/catalog.json"     # a recording…
# base_url = "https://api-v1.example.com"   # …or the old service, for a migration

[targets.candidate]
base_url = "https://api.staging.example.com"
auth = "env:PARITY_TOKEN"               # the variable name, never the value

[policy]
allowed_hosts = [".staging.example.com"]   # mandatory; no implicit allow-all
allow_mutations = false
repeats = 3                             # samples per endpoint, for stability
max_retries = 0                         # a retried 503 hides the flakiness
workers = 1                             # raise it deliberately; it is someone's staging
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

## Safety

A QA tool gets handed credentials and pointed at internal services. Full threat
model in [SECURITY.md](SECURITY.md); the controls:

- **Host allow-list is mandatory.** No implicit allow-all, and every redirect
  hop is re-validated — a staging host that 302s to production is not followed.
- **Production is refused by name.** A host containing `prod`/`prd`/`producao`
  needs `--allow-production`, out loud.
- **Writes are doubly gated.** `POST`/`PUT`/`PATCH`/`DELETE` run only when the
  case declares `mutating = true` *and* the run passes `--allow-mutations`.
- **Private networks are opt-in.**
- **Credentials do not cross hosts.** A redirect to a different host is
  followed only after re-validation, and the `Authorization` and `Cookie`
  headers are dropped before it is: a token issued for one host has no business
  reaching another, even one that is also on the allow-list.
- **Fail before sending.** Every URL and method is validated up front; a
  misconfigured run makes zero requests.
- **Credentials never touch the suite file.** A suite containing something
  shaped like a live token is refused, and `parity-gate scan` runs the same
  check over any path — including in this project's own CI.
- **Everything written to disk is redacted first**: sensitive headers and JSON
  keys, known token shapes, and personal data (e-mail, CPF, CNPJ, card numbers).
  An integration test asserts a token supplied through the environment never
  reaches the evidence.

## In CI

```yaml
- name: API contract gate
  run: parity-gate run --suite suites/api.toml --strict
  env:
    PARITY_TOKEN: ${{ secrets.STAGING_TOKEN }}
```

| Exit code | Meaning |
| --- | --- |
| `0` | Passed (warnings allowed unless `--strict`) |
| `1` | Could not run: bad arguments, malformed suite, missing credential |
| `2` | **Failed** — breaking drift, value difference, or failed assertion |
| `3` | Refused by the safety policy, before any request was sent |

## Commands

```
parity-gate record    --suite FILE [--out PATH] [--from URL]   # capture today's shape
parity-gate run       --suite FILE [--strict] [--evidence DIR] # gate against it
parity-gate stability --suite FILE                             # measure the noise
parity-gate import-openapi --spec SPEC --out FILE               # a suite from a spec
parity-gate verify    DIR                                      # re-check an evidence bundle
parity-gate scan      PATH...                                  # fail on a credential in a file
parity-gate demo      [--mode MODE]                            # offline, bundled mock
parity-gate mock      [--port 8799]                            # serve the mock by hand
```

`record`, `run` and `stability` also take:

| Flag | What it does |
| --- | --- |
| `--filter PATTERN` | run a subset: a glob against a case id or requirement, or a substring of the title. Repeatable. `--filter 'CAT-*'`, `--filter REQ-SEC-01`, `--filter orders` |
| `--keep N` | keep only the N most recent evidence bundles and delete the rest. Off by default; only directories carrying a manifest are ever touched |

If the `parity-gate` script is not on your `PATH` — common on Windows, where
`pip` installs it under `Scripts\` — `python -m parity_gate` is the same
program and takes the same arguments.

## How it is built

Standard library only. No `requests`, no `pyyaml`, no diff library — a tool that
exists to be trusted about someone else's code should be readable end to end,
and installing it should pull nothing.

| Module | Responsibility |
| --- | --- |
| `schema.py` | Infer a contract from samples; classify drift by consumer impact |
| `contracts.py` | Record, store and reload a contract as a reviewable file |
| `graphql.py` | Operation type from the document; errors as the missing status code |
| `openapi.py` | A starter suite from a spec, with what it cannot fill in commented out |
| `differ.py` | Value diff with masking, identity matching and ordering as one finding |
| `flaky.py` | Stability triage and automatic mask suggestion |
| `safety.py` | Host allow-list, production guard, write gating, secret scan |
| `redaction.py` | Everything leaving the process, scrubbed |
| `evidence.py` | Hash-chained records, manifest, traceability matrix, verification |
| `report.py` | Markdown for tickets, self-contained HTML for CI |
| `httpclient.py` | Pooled `http.client`: explicit timeouts, retries, re-validated redirects |
| `runner.py` | Orchestration and the verdict rules for all three modes |
| `mock/server.py` | The two-headed demo API, deterministic down to the flaky endpoint |

**213 tests**, unit and integration, offline. The CI matrix is configured for
Python 3.11–3.13 on Linux and Windows; the badge at the top is the honest answer
to whether it is currently green.

The integration suite boots the mock and asserts that each planted defect is the
finding that comes out — including two control experiments that must produce
nothing: a service compared against itself, and a service checked against its
own recording.

This has been through an adversarial review whose first instruction was *make it
report PASS on a change that would break a real consumer*. It found two ways.
Both are fixed, both now have a regression test, and the write-up —
including the findings that were **not** fixed — is in
[docs/review-2026-09-12.md](docs/review-2026-09-12.md).

## Documentation

- [Test strategy](docs/test-strategy.md) — risk analysis, scope, oracles and
  their limits, entry and exit criteria
- [ADR-001](docs/adr-001-differential-over-golden-files.md) — why not golden
  files, and why the oracle is a running service
- [ADR-002](docs/adr-002-record-shape-not-values.md) — why a recorded contract
  holds shape and status but never values, and how that removes the rot
- [SECURITY.md](SECURITY.md) — threat model and controls
- [Review findings](docs/review-2026-09-12.md) — an adversarial review, what it
  broke, what was fixed, and what is still open
- [Sample evidence bundle](docs/evidence/) — report, machine record, manifest
- Bug reports written from real findings:
  [BUG-001](docs/bugs/BUG-001-price-serialised-as-string.md) ·
  [BUG-002](docs/bugs/BUG-002-missing-product-returns-200.md) ·
  [BUG-003](docs/bugs/BUG-003-internal-cost-exposed.md)

## When to use something else

The honest map, because a tool that claims to cover everything is easy to catch
out and hard to trust afterwards.

| If you have | Use | Why |
| --- | --- | --- |
| A maintained OpenAPI spec and you want the implementation checked against it | **Schemathesis**, **Dredd** | They answer "does the code match the spec". This answers "did the code change" — the spec is not the oracle here, yesterday's behaviour is. `import-openapi` gets you a suite from the spec, and then the two descriptions can be compared |
| Consumers who can publish their expectations | **Pact** | Consumer-driven contracts are a stronger guarantee: they encode what each consumer actually uses. They also need every consumer to cooperate and a broker to run. This needs neither, and gives you less |
| Budget and a team that wants API diffing as a product | **Optic** | Adjacent ground, more mature, commercial. This is a CLI with no dependencies and an evidence trail built for a QA workflow |
| gRPC or Protobuf | **buf breaking**, **Protolock** | Schema-first by construction; the breaking-change check belongs in the schema toolchain, not here |
| Long-lived streams — websockets, SSE, GraphQL subscriptions | Something else | Not supported, and a subscription is refused at load time with that reason rather than half-working |

Where this earns its place: **one API, real consumers, and no spec anyone
trusts.** No broker, no code generation, no consumer cooperation, nothing to
install. One TOML file and `record`.

And the second reason, which is the QA half rather than the dev half: the
output is built to survive a ticket. A hash-chained bundle, a requirement
traceability matrix, and a report that names the consumer consequence rather
than printing a diff.

## Limits

Worth stating plainly, because a tool that oversells itself gets trusted in the
wrong places:

- **It checks shape and status, not correctness.** A recorded contract says what
  the API looked like, not that it was right. In differential mode, a bug
  faithfully reproduced by both sides passes.
- **A recording is only as good as the day it was taken.** Record from a version
  you actually believe in, and review the file before committing it.
- **Stability is sampled, not proven.** Three repeats taken back to back find
  frequent flakiness, not flakiness on a slower period than that.
- **Redaction is pattern-based.** A secret in a format nobody has seen survives
  it. Read evidence before attaching it to a public issue.
- **gRPC and long-lived streams are out of scope.** It speaks JSON over HTTP;
  GraphQL is supported, subscriptions are not.
- **A recorded contract is only shape.** Two services that agree on every type
  and disagree on every value both pass contract mode. Values are compared only
  in differential mode, where there is a live oracle to compare against.

## Licence

MIT — see [LICENSE](LICENSE).

Built by [Gabriel Caetano](https://github.com/Kattiell), QA engineer.
