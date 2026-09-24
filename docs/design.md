# Design

Why the tool behaves the way it does. If you only want it running against your
API, you do not need this page: start at [QUICKSTART.md](QUICKSTART.md).

Two decisions have their own records: [ADR-001](adr-001-differential-over-golden-files.md)
on why the oracle is a running service rather than a golden file, and
[ADR-002](adr-002-record-shape-not-values.md) on why a recorded contract holds
shape and status but never values.

## How the noise is handled

This is the part that decides whether a gate is still switched on in six
months.

**Volatile paths are masked, and the masks are discovered for you.** Request
ids and timestamps differ by definition. The tool samples, sees what moved, and
prints the `mask_paths` block ready to paste.

**Collections are matched by identity, not position.** Tell it
`"$.products" = "id"` and item `id=7` is compared with item `id=7`. Without it,
a reordered page of four products reports a difference on every field of every
item and the one real regression is invisible.

**Reordering is one finding.** Not one per index.

**Stability is measured before anything is compared.** A diff taken from a
flaky endpoint is not evidence of anything, so the tool refuses to produce one
and says why.

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
matches nothing, the item fields were not deleted; nothing was learned about
them. Reporting that as a breaking change is how a gate gets switched off in
its first week, so those paths are listed separately instead.

**A configuration that could not be honoured says so.** Ask for identity
matching on a collection whose key is not unique on both sides and you get a
`KEY_MATCH_UNAVAILABLE` finding, not a silent fall back to positional
comparison and noise you would have blamed on the API.

**Drift is classified by consumer impact, not by "changed".**

| Severity | Example | Why |
| --- | --- | --- |
| `breaking` | `number` to `string`, a required field removed, a field that can now be `null` | Existing consumers fail |
| `risky` | `integer` to `number`, a type narrowed | Usually survivable, worth a look |
| `additive` | A new field | Ignored by well-behaved clients |

## Flakiness across runs, not only across calls

Three calls in a row cannot see a cache that expires hourly or a replica that
rotates. `repeat_interval_ms` spaces the samples out, and the runs CI already
makes are the slower repetition: every bundle records each case's verdict and
the revision under test, so the history of an evidence directory answers what
sampling cannot.

```
$ parity-gate history evidence/ --fail-on-intermittent

  INTERMITTENT catalog/OPS-002      14 runs, 5 flip(s); revision 3f2a1c9d0b1e gave FAIL, PASS
  REGRESSED    catalog/CAT-001      14 runs, 1 flip(s)

cases     1 intermittent, 1 regressed, 0 recovered, 12 steady
```

| History | Meaning |
| --- | --- |
| `INTERMITTENT` | Changed outcome and changed back, or two runs of the **same revision** disagreed |
| `REGRESSED` | Passed, then failed and kept failing: a change, not noise |
| `RECOVERED` | Failed, then passed and kept passing |
| `STEADY` | The same outcome every run |

Only bundles that still pass `verify` are read, and the revision comes from
`--revision` or the usual CI variables (`GITHUB_SHA`, `CI_COMMIT_SHA`,
`BUILD_SOURCEVERSION`, `GIT_COMMIT`). Keep enough bundles for it to matter:
`--keep 30` rather than `--keep 1`.

## GraphQL, without lying about what is a read

A GraphQL endpoint is JSON over HTTP, so the contract engine applies unchanged.
What does not apply is everything HTTP gives for free.

**Every operation is a POST.** If the write gate went by method, every query
would have to be declared a mutation, and a safety control everyone switches
off protects nothing. The operation type is read from the document instead.

**Every response is 200**, including the failures, so the check that catches a
`404` softened into a `200` has nothing to work with. The equivalent signal is
the `errors` array, and it is checked explicitly:

```
FAIL  GQL-001   Product query keeps its shape   - 2 breaking drift; failed: graphql_errors

- `graphql_errors`: the response carries GraphQL errors:
  Cannot resolve field 'stock' on type 'Product'
```

A status-based check would have called that run green.

## Starting from an OpenAPI spec

A spec is a reason to reach for Schemathesis or Dredd *for a different
question*: they answer "does the code match the spec", this answers "did the
code change". But the spec also lists the endpoints, and typing that list into
TOML is the single largest piece of adoption friction.

```bash
parity-gate import-openapi --spec https://api.example.com/openapi.json \
                           --out suites/api.toml
```

It reads JSON (what FastAPI, Spring and Swagger UI serve live), emits a case
per safe operation, uses the `example` from each path parameter, and **comments
out the cases it cannot fill in rather than inventing an id**: a case that
fails for the wrong reason is worse than one that does not run. Writes are left
out unless you pass `--include-writes`.

## Measuring the gate itself

A gate is trusted for what it has been shown to catch, so it is measured the
way any detector is: inject a fixed fault model into real responses, count what
it catches, and count how often it cries wolf.

```
$ parity-gate selftest --demo

  fault (must fail)     mode           injected  caught  missed
  TYPE_SWAP             differential         40      40       0
  NULL_INJECT           contract             40      40       0
  STATUS_CHANGE         contract              7       7       0
  ...
  control (must not)    mode           injected    fine  alarms
  VALUE_CHANGE          contract             40      40       0
  COLLECTION_EMPTY      contract              7       7       0
  ...

differential  detection 100.0% (187/187)   false alarms   0.0% (0/20)
contract      detection 100.0% (137/137)   false alarms   0.0% (0/70)
```

| Faults: the case must fail | Controls: the case must not fail |
| --- | --- |
| A value changes JSON type | A value changes in contract mode (shape only) |
| A value becomes null | A collection loses an item, or comes back empty, in contract mode |
| An always-present field is missing | A collection comes back in another order |
| The status code changes | A new field appears |
| A value changes, an item is dropped or a page is empty, in differential mode | A value under a mask path changes |

Each fault is applied at one site (the first occurrence of each path, because
"one row of the page is wrong" is how these defects ship), and every mutant
goes through the same judging functions a real run uses rather than a copy of
their rules. The controls are what keep the number honest: a gate that failed
every change would score 100% detection and be switched off in a week.

Its first run found a real false negative, recorded in the
[review log](review-log.md).

Against your own API, `parity-gate selftest --suite suites/my-api.toml` samples
each case once, read-only and under the same safety policy, and exits `2` when
detection drops below `--min-detection` (default 100%) or false alarms rise
above `--max-false-alarms` (default 0%).

## How it is built

Standard library only. No `requests`, no `pyyaml`, no diff library. A tool that
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
| `sarif.py` | SARIF 2.1.0, located on the contract line or the suite case |
| `rules.py` | The stable rule catalogue every finding is reported under |
| `selftest.py` | Fault injection against the real judging code: detection and false-alarm rates |
| `waivers.py` | Accepted findings with an owner, a reason and an expiry that is enforced |
| `history.py` | Cross-run classification from verified bundles: intermittent, regressed, recovered |
| `httpclient.py` | Pooled `http.client`: response deadline and size cap, retries, re-validated redirects |
| `runner.py` | Orchestration, and pure judging functions with the verdict rules in one place |
| `mock/server.py` | The two-headed demo API, deterministic down to the flaky endpoint |

**279 tests**, unit and integration, offline. [CI](../.github/workflows/ci.yml)
runs them on Python 3.11, 3.12 and 3.13, on Linux and Windows, next to lint,
format, type checking, a credential scan of the repository, a check that the
committed evidence bundle still verifies and the selftest above.

The integration suite boots the mock and asserts that each planted defect is
the finding that comes out, including two control experiments that must produce
nothing: a service compared against itself, and a service checked against its
own recording.
