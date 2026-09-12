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
$ parity-gate run --suite suites/demo-contract-guard.toml

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
parity-gate record --suite suites/api.toml     # once — writes contracts/api.json
git add contracts/api.json                     # review it, commit it
parity-gate run --suite suites/api.toml        # every deploy, in CI
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

parity-gate demo                                            # mode 3, differential
parity-gate run --suite suites/demo-contract-guard.toml --with-mock   # mode 1
parity-gate stability --suite suites/demo-catalog.toml --with-mock    # mode 2
pytest -q                                                   # 133 tests, no network
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

**Redundant findings are folded.** Four products whose `price` changed type is
one contract-drift finding, not four value differences. The count of what was
folded is reported, so nothing vanishes silently.

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
repeats = 3
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
parity-gate verify    DIR                                      # re-check an evidence bundle
parity-gate scan      PATH...                                  # fail on a credential in a file
parity-gate demo                                               # offline, bundled mock
parity-gate mock      [--port 8799]                            # serve the mock by hand
```

## How it is built

Standard library only. No `requests`, no `pyyaml`, no diff library — a tool that
exists to be trusted about someone else's code should be readable end to end,
and installing it should pull nothing.

| Module | Responsibility |
| --- | --- |
| `schema.py` | Infer a contract from samples; classify drift by consumer impact |
| `contracts.py` | Record, store and reload a contract as a reviewable file |
| `differ.py` | Value diff with masking, identity matching and ordering as one finding |
| `flaky.py` | Stability triage and automatic mask suggestion |
| `safety.py` | Host allow-list, production guard, write gating, secret scan |
| `redaction.py` | Everything leaving the process, scrubbed |
| `evidence.py` | Hash-chained records, manifest, traceability matrix, verification |
| `report.py` | Markdown for tickets, self-contained HTML for CI |
| `httpclient.py` | `urllib` with explicit timeouts, retries and re-validated redirects |
| `runner.py` | Orchestration and the verdict rules for all three modes |
| `mock/server.py` | The two-headed demo API, deterministic down to the flaky endpoint |

**133 tests**, unit and integration, on Python 3.11–3.13 across Linux and
Windows. The integration suite boots the mock and asserts that each planted
defect is the finding that comes out — including two control experiments: a
service compared to itself, and a service checked against its own recording,
both of which must produce nothing. Three real bugs (a severity misclassified as
breaking, wrong CLI exit codes, and evidence hashes that depended on the
operating system's line endings) were caught by these tests during development.

## Documentation

- [Test strategy](docs/test-strategy.md) — risk analysis, scope, oracles and
  their limits, entry and exit criteria
- [ADR-001](docs/adr-001-differential-over-golden-files.md) — why not golden
  files, and why the oracle is a running service
- [ADR-002](docs/adr-002-record-shape-not-values.md) — why a recorded contract
  holds shape and status but never values, and how that removes the rot
- [SECURITY.md](SECURITY.md) — threat model and controls
- [Sample evidence bundle](docs/evidence/) — report, machine record, manifest
- Bug reports written from real findings:
  [BUG-001](docs/bugs/BUG-001-price-serialised-as-string.md) ·
  [BUG-002](docs/bugs/BUG-002-missing-product-returns-200.md) ·
  [BUG-003](docs/bugs/BUG-003-internal-cost-exposed.md)

## Limits

Worth stating plainly, because a tool that oversells itself gets trusted in the
wrong places:

- **It checks shape and status, not correctness.** A recorded contract says what
  the API looked like, not that it was right. In differential mode, a bug
  faithfully reproduced by both sides passes.
- **A recording is only as good as the day it was taken.** Record from a version
  you actually believe in, and review the file before committing it.
- **Stability is sampled, not proven.** Three repeats find frequent flakiness,
  not rare flakiness.
- **Redaction is pattern-based.** A secret in a format nobody has seen survives
  it. Read evidence before attaching it to a public issue.
- **GraphQL, gRPC and streaming are out of scope.** It speaks JSON over HTTP.

## Licence

MIT — see [LICENSE](LICENSE).

Built by [Gabriel Caetano](https://github.com/Kattiell), QA engineer.
