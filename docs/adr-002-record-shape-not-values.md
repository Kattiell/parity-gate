# ADR-002: A recorded contract stores shape, never values

**Status:** accepted · **Date:** 2026-09-12 · **Extends:** [ADR-001](adr-001-differential-over-golden-files.md)

## Context

[ADR-001](adr-001-differential-over-golden-files.md) chose to compare against a
running baseline and rejected golden files because they rot. That decision holds
for what it covered, and it left the tool useful to exactly one situation: a
migration, while both services happen to be running.

That is not where the problem lives. Most teams have **one** API. They still
ship a deploy every week into consumers that cannot be redeployed in lockstep,
and they still have no answer to "did that change break anyone". Requiring a
second live service to answer it made the tool a migration accessory.

So the question came back: how does a single-service team get a baseline without
inheriting the decay ADR-001 correctly refused?

## Decision

Record a contract from the live API, and store only **types, requiredness and
the status codes each endpoint answered with**. Never values.

```json
{"path": "$.products[].price", "types": ["number"], "required": true}
```

The file is committed. Every later run compares the live API against it.

## Why this does not rot

ADR-001's objection to golden files was specific: a fixture recorded in March
fails in June because a category was renamed or a `total` legitimately grew. The
failure is real and uninteresting, it happens constantly, and the cure people
reach for (re-record without reading the diff) turns the suite into a rubber
stamp.

Every one of those failures comes from recording **data**. Data changes hourly
by design. Shape does not: `price` stays a number across every product ever
added, until somebody changes the serialiser, which is precisely the event worth
a build failure.

So the decay mechanism is removed rather than tolerated. A recorded contract
stays valid until the API actually changes, and when it does, the diff is one
line and its meaning is obvious:

```diff
-  {"path": "$.products[].price", "types": ["number"], "required": true},
+  {"path": "$.products[].price", "types": ["string"], "required": true},
```

That is also why the file is written one field per line. A contract nobody can
read in a pull request gets approved without being read, and then it is a golden
file again by another name.

### One correction

The first version of this decision claimed a recorded contract "stays valid
until someone actually changes the API". That was too strong, and a review
falsified it: a collection that is *empty* on the day the gate runs makes every
item field look removed, so a healthy service with no matching rows failed the
build with five breaking findings.

Shape does not rot with data, but shape cannot be *observed* through an empty
collection either. The fix is to treat that subtree as unobserved rather than
absent, and to report those paths as "not checked" so the gap is visible rather
than either alarming or silent. See
[the review](review-2026-09-12.md#blocker--an-empty-collection-read-as-five-breaking-changes).

## Recording status codes as part of the contract

The recording keeps which statuses each endpoint answered with. This is what
catches the "tolerant handler" class of regression (a `404` softened into a
`200` with an empty body) on a case where nobody wrote `expect_status`,
because nobody predicted that particular change.

It costs one integer per case and it is the single highest-value thing in the
file.

## What it costs

**A recording inherits the moment it was taken.** If the API was already wrong,
the contract enshrines it. Mitigated by making the file reviewable and by
recording stability alongside each case, so a contract taken from a flaky
endpoint says so.

**It cannot catch value regressions.** A pagination counter that reports 3 while
returning 4 items is invisible to a shape check. That is the one thing the live
differential mode from ADR-001 does better, and it is why that mode is kept
rather than replaced.

**Requiredness is decided at record time.** "Always present" is observed across
the samples taken during recording; three samples of an endpoint that omits a
field one time in fifty will record it as required. Raising `repeats` before
recording is the mitigation, and the trade is stated rather than hidden.

## Result

Three modes, from one engine:

| Mode | Needs | Catches |
| --- | --- | --- |
| Recorded contract | One API | Shape and status regressions on every deploy |
| Stability | One API | Which endpoints cannot be tested reliably yet |
| Differential | Two live APIs | All of the above, plus value divergence |

ADR-001 is not reversed. Golden **values** are still refused, for the reasons it
gives. What changed is the realisation that a baseline does not have to contain
values to be a baseline.
