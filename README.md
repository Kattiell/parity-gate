# parity-gate

**Prove an API rewrite did not break the contract.**

A CI gate that runs the same requests against the old service and the new one,
then reports the differences that actually matter: contract drift, value
divergence, and whether the endpoint was stable enough for the comparison to
mean anything. Every run leaves a hash-chained evidence bundle you can attach to
a ticket.

[![CI](https://github.com/Kattiell/parity-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/Kattiell/parity-gate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

```
$ parity-gate demo

suite  demo-catalog  (6 cases, 3 repeats)
  baseline  http://127.0.0.1:8799/legacy
  candidate http://127.0.0.1:8799/next

  FAIL  CAT-001   Product listing keeps its pagination envelope a...  - 3 breaking drift; 2 diff
  PASS  CAT-002   Product detail is byte-for-byte unchanged (not ...
  FAIL  CAT-003   An unknown product id still answers 404, not an...  - 2 breaking drift; failed: status
  FAIL  SEC-001   Internal cost data is never exposed on the publ...  - 2 breaking drift; 2 diff; failed: forbidden_field
  WARN  OPS-001   Health endpoint answers and reports uptime          - volatile_body
  WARN  OPS-002   Inventory sync status is stable enough to be co...  - flaky_status

verdict   FAIL
cases     6 - 1 PASS, 2 WARN, 3 FAIL
findings  7 breaking drift, 4 value diff, 1 unstable
evidence  evidence/20260912T040717Z-a35ce1
chain     07a62a4133006678
```

That run is offline and takes about a second. It boots a bundled mock that
serves a catalogue API twice — once as it was, once as rewritten with defects
planted on purpose — and finds every one of them.

---

## The problem

APIs rarely break because a test failed. They break because the contract moved
and nothing was watching. A field becomes a string, a never-null value comes
back null, `404` becomes an empty `200`, and the consumers find out in
production. Contract drift is consistently named the top non-bug API failure in
enterprise systems, and automated detection is what takes time-to-detection from
weeks down to minutes.

The reason teams do not already run this check is not that the idea is new. It
is that naive comparison harnesses are unusable:

- Two live services return different timestamps and request ids on every call,
  so every report is full of differences nobody cares about.
- A page returned in a different order produces a difference at every index.
- An endpoint that is intermittently failing produces a diff that looks like a
  regression and is actually noise — and timing and test data are behind the
  large majority of flaky failures.

After a few weeks of that, the gate gets switched off. `parity-gate` is built
around that failure mode: **the engineering is in the signal-to-noise ratio**,
not in the diff.

## What it does

**Infers the contract from real traffic.** No hand-written JSON Schema. Both
services are sampled several times, and the shape is derived from what they
actually return — including which fields are *always* present, which cannot be
known from a single response.

**Classifies drift by consumer impact, not by "changed".**

| Severity | Example | Why |
| --- | --- | --- |
| `breaking` | `number` → `string`, a required field removed, a field that can now be `null` | Existing consumers fail |
| `risky` | `integer` → `number`, a type narrowed | Usually survivable, worth a look |
| `additive` | A new field | Ignored by well-behaved clients |

**Diffs values by identity, not by position.** Give it `"$.products" = "id"` and
item `id=7` is compared against item `id=7`. A reordered page becomes one
ordering note instead of a difference on every field of every item.

**Measures stability before it compares anything.** Each endpoint is called N
times first:

| Verdict | Meaning | What the report does |
| --- | --- | --- |
| `STABLE` | Same status, shape and bytes | Compare normally |
| `VOLATILE_BODY` | Values move, shape holds | Compare, and **propose the exact `mask_paths`** to silence the noise |
| `FLAKY_SHAPE` | The structure itself varies | Refuse to compare; report it |
| `FLAKY_STATUS` | The status code varies | Refuse to compare; fix this first |

A diff taken from a flaky endpoint is not evidence of anything, and saying so is
more useful than reporting the noise as a regression.

**Folds redundant findings.** Four products whose `price` changed type are one
contract-drift finding, not four value differences. The count of what was folded
is reported, so nothing disappears silently.

**Leaves evidence.** Every case produces a record hashed together with the hash
of the record before it. `parity-gate verify` recomputes the chain and the file
hashes, so an edited report is detectable.

## Try it

```bash
git clone https://github.com/Kattiell/parity-gate
cd parity-gate
pip install -e ".[dev]"

parity-gate demo          # offline: bundled mock, ~1s, exits 2 by design
pytest -q                 # 97 tests, no network
```

Outputs land in `evidence/<run-id>/`:

| File | What it is |
| --- | --- |
| `report.md` | For the ticket |
| [`report.html`](docs/evidence/report.html) | Self-contained, light/dark, for the CI artifact |
| `run.json` | Machine-readable: every finding, every redacted exchange, both inferred schemas |
| `manifest.json` | SHA-256 per file plus the chain head |

A complete sample bundle from a real run is committed under
[`docs/evidence/`](docs/evidence/) — including
[the full report](docs/evidence/report.md). It verifies:

```bash
parity-gate verify docs/evidence
```

### Against a real API over the network

```bash
parity-gate run --suite suites/live-self-parity.toml
```

This compares a live public API **against itself**. It should come back all
green, and that is the point: it is the control experiment, and the measurement
worth taking before trusting any real comparison. Whatever it reports as
volatile is noise you would otherwise have chased.

## What the demo finds

The bundled rewrite contains the defects that survive code review, not obvious
ones:

| Case | Finding | Kind |
| --- | --- | --- |
| `CAT-001` | `price` serialised as a string | `TYPE_CHANGED` · breaking |
| `CAT-001` | `stock` dropped as "unused" | `FIELD_REMOVED` · breaking |
| `CAT-001` | `discount` can now be `null` | `NULLABLE_ADDED` · breaking |
| `CAT-001` | `total` says 3 while 4 items are returned | value difference |
| `CAT-001` | Items come back in a different order | reported once |
| `CAT-002` | Untouched endpoint stays green | `PASS` |
| `CAT-003` | Missing product answers `200 {"data": null}` | failed assertion + breaking drift |
| `SEC-001` | `internalCost` exposed on the public catalogue | failed `forbidden_fields` |
| `OPS-001` | `uptimeSeconds` moves every call | `VOLATILE_BODY` + suggested mask |
| `OPS-002` | Endpoint fails intermittently | `FLAKY_STATUS`, comparison refused |

Three of them are written up as bug reports:
[BUG-001](docs/bugs/BUG-001-price-serialised-as-string.md) ·
[BUG-002](docs/bugs/BUG-002-missing-product-returns-200.md) ·
[BUG-003](docs/bugs/BUG-003-internal-cost-exposed.md)

## Writing a suite

A suite is one TOML file. It names two targets, a safety policy, and the cases.

```toml
name = "catalog-migration"

[targets.baseline]
base_url = "https://api-v1.staging.example.com"

[targets.candidate]
base_url = "https://api-v2.staging.example.com"
auth = "env:PARITY_CANDIDATE_TOKEN"   # the name, never the value

[policy]
allowed_hosts = [".staging.example.com"]   # mandatory; no implicit allow-all
allow_mutations = false
repeats = 3
mask_paths = ["$.meta.requestId", "$.*.updatedAt"]

[policy.array_keys]
"$.products" = "id"                        # compare by identity, not position

[[cases]]
id = "CAT-001"
requirement = "REQ-CAT-01"                 # feeds the traceability matrix
title = "Product listing keeps its pagination envelope"
risk = "critical"
method = "GET"
path = "/products?limit=4"
expect_status = 200
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
- **Private networks are opt-in**, so a suite cannot be aimed at internal
  infrastructure by accident.
- **Fail before sending.** Every URL and method is validated up front; a
  misconfigured run makes zero requests.
- **Credentials never touch the suite file.** A suite containing something
  shaped like a live token is refused, and `parity-gate scan` runs the same
  check over any path — including in this project's own CI.
- **Everything written to disk is redacted first**: sensitive headers and JSON
  keys, known token shapes, and personal data (e-mail, CPF, CNPJ, card numbers).
  An integration test asserts that a token supplied through the environment
  never reaches the evidence.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Gate passed (warnings allowed unless `--strict`) |
| `1` | Could not run: bad arguments, malformed suite, missing credential |
| `2` | **Gate failed** — breaking drift, value difference, or failed assertion |
| `3` | Refused by the safety policy, before any request was sent |

```yaml
- name: API parity gate
  run: parity-gate run --suite suites/catalog-migration.toml --strict
  env:
    PARITY_CANDIDATE_TOKEN: ${{ secrets.STAGING_TOKEN }}
```

## Commands

```
parity-gate run --suite FILE [--evidence DIR] [--strict]
                             [--allow-mutations] [--allow-production] [--with-mock]
parity-gate demo                    # offline, bundled mock + suite
parity-gate verify DIR              # recompute the evidence hash chain
parity-gate scan PATH...            # fail if a file holds a credential
parity-gate mock [--port 8799]      # serve the two-variant mock by hand
```

## How it is built

Standard library only. No `requests`, no `pyyaml`, no diff library — a tool that
exists to be trusted about someone else's code should be readable end to end,
and `pip install` of it should pull nothing.

| Module | Responsibility |
| --- | --- |
| `schema.py` | Infer a contract from samples; classify drift by consumer impact |
| `differ.py` | Value diff with masking, identity matching and ordering as one finding |
| `flaky.py` | Stability triage and automatic mask suggestion |
| `safety.py` | Host allow-list, production guard, write gating, secret scan |
| `redaction.py` | Everything leaving the process, scrubbed |
| `evidence.py` | Hash-chained records, manifest, traceability matrix, verification |
| `report.py` | Markdown for tickets, self-contained HTML for CI |
| `httpclient.py` | `urllib` with explicit timeouts, retries and re-validated redirects |
| `runner.py` | Orchestration and the verdict rules |
| `mock/server.py` | The two-headed demo API, deterministic down to the flaky endpoint |

**97 tests**, unit and integration, on Python 3.11–3.13 across Linux and Windows.
The integration suite boots the mock, runs the whole pipeline, and asserts that
each planted defect is the finding that comes out — including a control test
that comparing a service to *itself* produces nothing. Two real bugs in the
severity classifier and in the CLI exit codes were caught by these tests during
development.

## Documentation

- [Test strategy](docs/test-strategy.md) — risk analysis, scope, oracles and
  their limits, entry and exit criteria
- [ADR-001](docs/adr-001-differential-over-golden-files.md) — why compare
  against a running baseline instead of committing golden files
- [SECURITY.md](SECURITY.md) — threat model and controls
- [Sample evidence bundle](docs/evidence/) — report, machine record, manifest

## Limits

Worth stating plainly, because a tool that oversells itself gets trusted in the
wrong places:

- **It proves parity, not correctness.** A bug faithfully reproduced by both
  sides passes. The oracle is the old service, and the old service has bugs.
- **Both services have to be running at once.** That is the migration window.
  Once the baseline is gone, so is the oracle.
- **Stability is sampled, not proven.** Three repeats find frequent flakiness,
  not rare flakiness.
- **Redaction is pattern-based.** A secret in a format nobody has seen survives
  it. Read evidence before attaching it to a public issue.
- **GraphQL, gRPC and streaming are out of scope.** It speaks JSON over HTTP.

## Licence

MIT — see [LICENSE](LICENSE).

Built by [Gabriel Caetano](https://github.com/Kattiell), QA engineer. The problem
it solves is one I met at work: verifying a legacy service and its refactored
replacement agree, across hundreds of endpoints, without drowning in false
alarms.
