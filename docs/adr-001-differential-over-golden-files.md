# ADR-001: Compare against the running baseline, not against golden files

**Status:** accepted · **Date:** 2026-09-12 · **Extended by:** [ADR-002](adr-002-record-shape-not-values.md)

## Context

To verify that a rewritten API still satisfies its consumers, the expected
response has to come from somewhere. Three options were on the table.

**Golden files.** Record the old responses once, commit them, assert the new
service matches. Simple, offline, reviewable in a diff.

**Hand-written expectations.** Write out the expected shape and values per
endpoint. This is what most API test suites do.

**Differential.** Call both services with the same request and compare.

## Decision

Compare against the running baseline, with recorded responses as a fallback
only for endpoints that cannot be called twice.

## Why

Hand-written expectations were ruled out first. The person writing the
assertion and the person writing the endpoint share the same misunderstanding
of the requirement, so the test encodes the bug and passes. That is how a suite
ends up green over a broken payload.

Golden files are better — the expectation comes from real behaviour — but they
rot in a specific and expensive way. A fixture recorded in March fails in June
because a category was renamed, a product was archived, or a `total` legitimately
grew. The failure is real but uninteresting, and each one is a small tax on
whoever is on shift. After enough of them, the fixtures get re-recorded from the
current output without anyone reading the diff, and at that moment the suite
stops testing anything at all. A golden-file suite decays into a rubber stamp
and gives no signal that it has.

Differential comparison does not rot, because both sides move together. When
the catalogue changes, both services return the new catalogue and the diff stays
empty. The only thing that shows up is the thing being looked for: a divergence
between the two implementations, right now, on the same data.

## What it costs

**Both services have to be reachable at the same time.** Fine during a
migration, which is exactly the window this tool is for, and a real constraint
afterwards.

**Twice the calls.** With `repeats = 3`, six calls per case. Acceptable against
staging; the reason mutating cases are off unless deliberately enabled.

**It only proves parity.** A bug faithfully reproduced by both sides passes. The
gate answers "did the rewrite change behaviour", never "is the behaviour right".
That is stated in the test strategy so nobody mistakes a green run for a
correctness proof.

**Noise had to be engineered away, not tolerated.** Two live services return
different timestamps, ids and orderings on every call. Left alone, the report is
unreadable and gets ignored. Three mechanisms exist for this reason alone:
masked volatile paths, identity-based collection matching, and measuring
stability before comparing anything.

## Alternatives if the constraint changes

If the baseline is decommissioned before the gate is retired, the fallback is to
record its responses under the same masks the live comparison used, and accept
the decay — with the expiry written down rather than discovered.

## Revisited

That fallback turned out to be the main event, and the decay turned out to be
avoidable. [ADR-002](adr-002-record-shape-not-values.md) records a contract that
holds shape and status but no values, which removes the rot mechanism this
decision was written to avoid — and lifts the requirement for two live services,
which was the real limitation here.
