# parity-gate

**Your API changed. Did it break anyone?**

A CI gate that answers that in seconds, for any JSON API: no second service, no
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

## Start here

```bash
git clone https://github.com/Kattiell/parity-gate
cd parity-gate
pip install -e .

parity-gate demo --mode contract     # the run above, offline, against a bundled mock
```

Then **[docs/QUICKSTART.md](docs/QUICKSTART.md)** puts it on your own API in
about ten minutes: four commands, the output you should see at each one, and a
troubleshooting table. There is no code to write, only one TOML file.

## Which mode is yours

| You have | Compared against | Command |
| --- | --- | --- |
| One API in production, no rewrite planned | its own shape, recorded earlier | `record`, then `run` |
| An API nobody has measured yet | its own repeated answers | `stability` |
| A rewrite replacing a live service | the old service | `run`, with two targets |

The first row is the common case, and the one most tools do not serve: it needs
nothing but the API you already have.

## The problem

APIs rarely break because a test failed. They break because the shape moved and
nothing was watching. A field becomes a string, a never-null value comes back
null, a `404` quietly becomes a `200`, and the consumers find out in
production.

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
signal-to-noise ratio**, not in the diff. How that is done, finding by finding,
is in [docs/design.md](docs/design.md).

## What it does

**Gates a deploy against the shape you already ship.** The recorded contract
holds types, requiredness and the status codes each endpoint answered with,
never values. That is the whole trick against golden-file rot: your catalogue
changes daily, its *shape* does not. It is written one field per line so the
diff is the review:

```diff
-  {"path": "$.products[].price", "types": ["number"], "required": true},
+  {"path": "$.products[].price", "types": ["string"], "required": true},
-  {"path": "$.products[].stock", "types": ["integer"], "required": true},
```

**Tells you which endpoints you can trust**, before you assert anything against
them. `stability` calls each endpoint N times and classifies what varied, so
"this test is flaky" and "this endpoint is flaky" stop being the same sentence:

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `STABLE` | Same status, shape and bytes | Trust anything built on it |
| `VOLATILE_BODY` | Values move, shape holds | Noisy, not broken: **the exact masks are printed for you** |
| `FLAKY_SHAPE` | The structure itself varies | Fix before writing tests against it |
| `FLAKY_STATUS` | The status code varies | Fix first; nothing else from this endpoint means anything |

Across runs rather than across calls, `parity-gate history` reads the bundles CI
already produced and separates an intermittent failure from a real regression.

**Proves a rewrite matches the service it replaces.** Two live services, same
requests, and here values are diffed too, because a live baseline is an oracle
no hand-written expectation can match. It catches the pagination counter that
says 3 while returning 4 items.

GraphQL is supported, with the operation type read from the document and the
`errors` array checked explicitly, since every GraphQL response is a `200`. If
you publish an OpenAPI document, `import-openapi` writes the starter suite for
you.

## Evidence, not opinions

Every run writes a bundle:

| File | What it is |
| --- | --- |
| `report.md` | For the ticket |
| [`report.html`](docs/evidence/report.html) | Self-contained, light/dark, for the CI artifact |
| `run.json` | Every finding, every redacted exchange, both inferred schemas |
| `report.sarif` | SARIF 2.1.0: each finding as a code-scanning alert on the contract line or suite case it is about |
| `manifest.json` | SHA-256 per file plus the chain head |

Each record is hashed together with the hash of the record before it, so a
report edited after the fact is detectable. A real bundle is committed under
[`docs/evidence/`](docs/evidence/) and verifies with `parity-gate verify
docs/evidence`.

Cases carry a `requirement` id, which produces a traceability matrix in every
report, and every finding carries a stable rule id (`PG1004` is a type change)
from the [rule catalogue](docs/rules.md). Ids are never renumbered.

## Safety

A QA tool gets handed credentials and pointed at internal services. Full threat
model in [SECURITY.md](SECURITY.md); the controls:

- **Host allow-list is mandatory**, and every redirect hop is re-validated: a
  staging host that 302s to production is not followed.
- **Production is refused by name.** A host containing `prod`, `prd` or
  `producao` needs `--allow-production`, out loud.
- **Writes are doubly gated**, by the case and by the run. **Private networks
  are opt-in.**
- **Credentials do not cross hosts**: the `Authorization` and `Cookie` headers
  are dropped before a redirect to a different host is followed.
- **Fail before sending.** A misconfigured run makes zero requests.
- **Credentials never touch the suite file**, and `parity-gate scan` runs that
  same check over any path, including in this project's own CI.
- **Everything written to disk is redacted first**: sensitive headers and JSON
  keys, known credential shapes, and personal data. An integration test asserts
  that a value supplied through the environment never reaches the evidence.

## In CI

```yaml
- name: API contract gate
  run: parity-gate run --suite suites/api.toml --strict --sarif parity-gate.sarif
  env:
    PARITY_TOKEN: ${{ secrets.STAGING_TOKEN }}

- name: Findings as pull-request annotations
  if: always()
  uses: github/codeql-action/upload-sarif@v4
  with:
    sarif_file: parity-gate.sarif
    category: parity-gate
```

The upload step needs `permissions: security-events: write`. The same file
opens in any SARIF viewer, so the annotations do not depend on it.

| Exit code | Meaning |
| --- | --- |
| `0` | Passed (warnings allowed unless `--strict`) |
| `1` | Could not run: bad arguments, malformed suite, missing credential |
| `2` | **Failed**: breaking drift, value difference, or failed assertion |
| `3` | Refused by the safety policy, before any request was sent |

## Commands

```
parity-gate record    --suite FILE [--out PATH] [--from URL]   # capture today's shape
parity-gate run       --suite FILE [--strict] [--evidence DIR] # gate against it
parity-gate stability --suite FILE                             # measure the noise
parity-gate import-openapi --spec SPEC --out FILE              # a suite from a spec
parity-gate selftest  (--demo | --suite FILE)                  # measure what the gate catches
parity-gate history   DIR [--fail-on-intermittent]             # flaky, regressed or recovered, across runs
parity-gate verify    DIR                                      # re-check an evidence bundle
parity-gate scan      PATH...                                  # fail on a credential in a file
parity-gate demo      [--mode MODE]                            # offline, bundled mock
parity-gate mock      [--port 8799]                            # serve the mock by hand
```

Shared flags (`--filter`, `--keep`, `--sarif`, `--revision`) are in
[docs/suite-reference.md](docs/suite-reference.md). If the `parity-gate` script
is not on your `PATH`, `python -m parity_gate` is the same program.

## Built to be read

Standard library only: no `requests`, no `pyyaml`, no diff library. **279
tests**, offline, on Python 3.11 to 3.13, Linux and Windows, next to lint,
format, type checking, a credential scan of the repository, a check that the
committed evidence still verifies, and a selftest that injects a fixed fault
model into real responses and fails if detection drops below 100% or false
alarms rise above 0%. The badge at the top is the honest answer to whether that
is currently green.

It has been through two adversarial reviews whose first instruction was *make
it report PASS on a change that would break a real consumer*. They found two
ways, plus a false negative the selftest caught on its own first run. All of it,
including what is still open, is in the [review log](docs/review-log.md).

## Documentation

- [Quickstart](docs/QUICKSTART.md): from clone to a gate in your CI, step by step
- [Suite reference](docs/suite-reference.md): every field of the suite file, plus waivers
- [Design](docs/design.md): noise handling, flakiness across runs, how the gate measures itself, module map
- [Rule catalogue](docs/rules.md): every finding id and its consumer impact
- [Test strategy](docs/test-strategy.md): scope, oracles and their limits, exit criteria
- [ADR-001](docs/adr-001-differential-over-golden-files.md): why not golden files
- [ADR-002](docs/adr-002-record-shape-not-values.md): why a contract holds shape and status, never values
- [Review log](docs/review-log.md): what the adversarial reviews broke, what was fixed, what is open
- [SECURITY.md](SECURITY.md): threat model and controls
- [Sample evidence bundle](docs/evidence/): report, machine record, manifest
- Bug reports written from real findings:
  [BUG-001](docs/bugs/BUG-001-price-serialised-as-string.md) ·
  [BUG-002](docs/bugs/BUG-002-missing-product-returns-200.md) ·
  [BUG-003](docs/bugs/BUG-003-internal-cost-exposed.md)

## When to use something else

The honest map, because a tool that claims to cover everything is easy to catch
out and hard to trust afterwards.

| If you have | Use | Why |
| --- | --- | --- |
| A maintained OpenAPI spec and you want the implementation checked against it | **Schemathesis**, **Dredd** | They answer "does the code match the spec". This answers "did the code change"; the spec is not the oracle here, yesterday's behaviour is |
| Consumers who can publish their expectations | **Pact** | Consumer-driven contracts are a stronger guarantee. They also need every consumer to cooperate and a broker to run |
| Budget and a team that wants API diffing as a product | **Optic** | Adjacent ground, more mature, commercial |
| gRPC or Protobuf | **buf breaking**, **Protolock** | Schema-first by construction; the check belongs in that toolchain |
| Long-lived streams (websockets, SSE, GraphQL subscriptions) | Something else | Not supported, and refused at load time with that reason rather than half-working |

Where this earns its place: **one API, real consumers, and no spec anyone
trusts.** No broker, no code generation, no consumer cooperation, nothing to
install. One TOML file and `record`.

## Limits

Worth stating plainly, because a tool that oversells itself gets trusted in the
wrong places:

- **It checks shape and status, not correctness.** A recorded contract says what
  the API looked like, not that it was right.
- **A recording is only as good as the day it was taken.** Record from a version
  you believe in, and read the file before committing it.
- **Stability is sampled, not proven.** `repeat_interval_ms` and `history`
  widen the window, but a period longer than the runs you keep is invisible.
- **The selftest measures a fixed fault model.** 100% means every fault in that
  model is caught, not that every possible defect is.
- **Redaction is pattern-based.** Read evidence before attaching it to a public
  issue.
- **gRPC and long-lived streams are out of scope.** It speaks JSON over HTTP;
  GraphQL is supported, subscriptions are not.

## Licence

MIT, see [LICENSE](LICENSE).

Built by [Gabriel Caetano](https://github.com/Kattiell), QA engineer.
