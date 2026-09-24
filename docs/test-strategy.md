# Test strategy

What the demo suite covers and what it deliberately does not. It is written for
the strictest of the three situations the tool serves, a rewrite replacing a
live service, because that is the only one where *values* are compared. Scope,
oracles and exit criteria apply unchanged to the other two.

## The question

A catalogue API is being replaced. Several consumers (a storefront, a mobile
app, a partner integration) were written against the old responses and cannot
be redeployed in lockstep. The release needs one narrow, testable answer:

> Can every existing consumer read what the new service returns?

Not "is the new service correct". That is the developers' test suite. This is
about the boundary.

## Scope

| In scope | Out of scope | Why |
| --- | --- | --- |
| Response shape and types | Internal business rules | Consumers break on shape, not on logic they never see |
| Status-code semantics | Latency SLOs | Budgets exist, but a wrong status is a bug in every client |
| Values that must be identical between versions | Values that are allowed to change | The suite says which is which |
| Data exposure on public endpoints | Authorisation depth | One explicit case; full authz is a separate effort |
| Stability of each endpoint | Load behaviour | An unstable endpoint invalidates every other result |

## What each case is there for

| Case | The failure it exists to catch |
| --- | --- |
| `CAT-001` | A serialiser changes a field's type (money as a string), a field consumers depend on is dropped as unused, a never-null field starts returning null, pagination metadata disagrees with the page |
| `CAT-003` | Error semantics soften: a `404` becomes an empty `200`, and clients cache nothing as something |
| `SEC-001` | A broader serialiser exposes internal data on the public catalogue |
| `OPS-002` | An endpoint is intermittently unavailable, which invalidates every comparison taken from it |
| `OPS-001` | An endpoint legitimately changes on every call, and comparing it would train people to ignore the gate |

The last one is the entry usually left out of a strategy, and it is the reason
most comparison harnesses get switched off within a quarter.

## Oracles and their limits

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
per warning; a finding accepted for longer than one release becomes a
`[[waivers]]` entry with that owner, a reason and an expiry the gate enforces.
Every `FLAKY_STATUS` and `FLAKY_SHAPE` finding is resolved before its case's
result counts for anything, because an unstable endpoint produces no usable
comparison.
