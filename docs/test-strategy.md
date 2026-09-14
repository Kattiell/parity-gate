# Test strategy

The strategy behind the demo suite, written the way it would be written for a
real change. It is here because the code shows *how* the tool works and this
shows *why* those cases and not others.

## Which situation this covers

The tool serves three, and they share most of this document:

| Situation | Oracle | How common |
| --- | --- | --- |
| A deploy into existing consumers | A contract recorded earlier | Every team with an API |
| An API you have not tested yet | Its own repeated answers | Every team, once |
| A rewrite replacing a live service | The old service | During a migration |

What follows is written for the third, because it is the strictest: it is the
only one where *values* are compared, so it has the most to say about oracles
and noise. Everything about risk, scope and exit criteria applies unchanged to
the other two. The recorded-contract mode simply drops the value-comparison
rows and gains one guarantee, that the shape and status codes cannot move
without the build saying so.

## Context

A catalogue API is being replaced. The old service still runs and still serves
traffic; the new one is meant to be a drop-in replacement. Several consumers
(a storefront, a mobile app, a partner integration) were written against the old
responses and cannot be redeployed in lockstep.

The question the release needs answered is narrow and testable:

> Can every existing consumer read what the new service returns?

Not "is the new service correct": that is the developers' test suite. This is
about the boundary.

## What is in scope

| In scope | Out of scope | Why |
| --- | --- | --- |
| Response shape and types | Internal business rules | Consumers break on shape, not on logic they never see |
| Status-code semantics | Latency SLOs | Budgets exist, but a wrong status is a bug in every client |
| Values that must be identical between versions | Values that are allowed to change | The suite says which is which |
| Data exposure on public endpoints | Authorisation depth | One explicit case; full authz is a separate effort |
| Stability of each endpoint | Load behaviour | An unstable endpoint invalidates every other result |

## Risk analysis

Risk drives which cases exist and which are marked `critical`.

| # | Risk | Likelihood | Impact | Cases |
| --- | --- | --- | --- | --- |
| R1 | Serialiser changes a field's type (money as a string is the classic) | High | Critical: clients crash or silently mis-parse | CAT-001 |
| R2 | A field consumers depend on is dropped as "unused" | High | Critical: feature disappears in one client, unnoticed in the others | CAT-001 |
| R3 | A never-null field starts returning null | Medium | Critical: null-pointer crashes in typed clients | CAT-001 |
| R4 | Pagination metadata disagrees with the page | Medium | High: infinite scroll loops, wrong counts | CAT-001 |
| R5 | Error semantics soften: 404 becomes an empty 200 | Medium | High: clients cache nothing as something | CAT-003 |
| R6 | Internal data is exposed by a broader serialiser | Low | Critical: margin data leaks to the public catalogue | SEC-001 |
| R7 | An endpoint is intermittently unavailable | Medium | Medium, plus it invalidates every comparison taken from it | OPS-002 |
| R8 | An endpoint is compared while it legitimately changes on every call | High | Low directly, high indirectly: false alarms train people to ignore the gate | OPS-001 |

R8 is the one usually left out of strategies, and it is the reason most
comparison harnesses get switched off within a quarter.

## Test design

Each case comes from one technique, chosen for the risk:

- **Contract inference over repeated samples** (R1 to R3). Optionality cannot be
  observed from a single response: "absent this time" and "optional" look the
  same. Every endpoint is sampled three times before its contract is inferred.
- **Differential testing** (R4). The oracle is the old service. No expected
  value is written by hand, so the suite cannot encode the same misunderstanding
  the developer had.
- **Negative-path case** (R5). Exactly one unhappy path, because the unhappy
  path is where "tolerant" rewrites do their damage.
- **Explicit denial assertion** (R6). `forbidden_fields` states what must *not*
  be there. Positive assertions can never catch an over-sharing serialiser.
- **Repetition as a measurement, not a retry** (R7, R8). Calling an endpoint N
  times and classifying what varied is the difference between "this test is
  flaky" and "this endpoint is flaky".

## Oracles

| Oracle | Used for | Limit |
| --- | --- | --- |
| The baseline service | Shape, types, values | Inherits the baseline's own bugs; a bug faithfully reproduced passes |
| The suite's `expect_status` | Error semantics | Only as good as the requirement it encodes |
| `forbidden_fields` | Data exposure | Only catches fields somebody thought to name |
| Self-consistency across repeats | Stability | Three samples find frequent flakiness, not rare flakiness |

Stating the limits matters: this gate proves the rewrite matches the old
behaviour. It does not prove the old behaviour was right.

## Entry and exit

**Entry.** Both services reachable from the runner; the suite's `allowed_hosts`
covers exactly those two; no case marked `mutating` unless the target data is
disposable.

**Exit.** Zero `breaking` drift findings and zero value differences on cases
marked `critical` or `high`. `WARN` is acceptable at release with a named owner
per warning. Every `FLAKY_STATUS` and `FLAKY_SHAPE` finding is resolved before
its case's result counts for anything, because an unstable endpoint produces no
usable comparison.

## What a failure costs

Running the suite takes seconds and touches nothing. Missing R1 costs a
production incident, an emergency release and a support queue. That asymmetry
is why this runs on every pull request rather than as a pre-release checklist
item.
