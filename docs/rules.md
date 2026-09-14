# Rule catalogue

Every finding parity-gate reports carries one of these ids: in `run.json`, in
`report.md` and in `report.sarif`. An id is never renumbered and never reused, and
`tests/test_rules.py` pins the whole table so a change to it is a decision
made in review.

The level is the SARIF default for the rule. A drift finding carries its own
severity (`breaking`, `risky`, `additive`), which decides its level instead:
error, warning and note respectively.

## Contract drift (PG1xxx)

### PG1001

`FIELD_REMOVED` · default level `warning`

A field present in the baseline is absent from the candidate.

**Consumer impact:** Consumers that read the field get undefined or crash; if it was always present, typed clients fail to deserialise.

### PG1002

`FIELD_NOW_OPTIONAL` · default level `warning`

A field that was always present is sometimes missing.

**Consumer impact:** Clients written without a null check for it fail on the responses that omit it.

### PG1003

`NULLABLE_ADDED` · default level `warning`

A field can now be null where it never was.

**Consumer impact:** Null-pointer failures in typed clients and 'null' rendered in UIs.

### PG1004

`TYPE_CHANGED` · default level `warning`

A field's type was replaced by an incompatible one.

**Consumer impact:** Parsers reject the payload or silently mis-read it (money as a string is the classic).

### PG1005

`TYPE_WIDENED` · default level `warning`

A field can now carry a type it did not before.

**Consumer impact:** Safe within the numeric family; any other new type is one existing parsers never saw.

### PG1006

`TYPE_NARROWED` · default level `warning`

A field stopped carrying one of the types it used to.

**Consumer impact:** Usually harmless; code paths that handled the dropped type go dead.

### PG1007

`FIELD_ADDED` · default level `note`

A field is new in the candidate.

**Consumer impact:** Ignored by tolerant readers; strict schema validation on the client rejects it.

### PG1008

`FIELD_NOW_ALWAYS_PRESENT` · default level `note`

An optional field is now always present.

**Consumer impact:** Harmless for consumers.

## Value differences (PG2xxx)

### PG2001

`VALUE` · default level `error`

Baseline and candidate return different values for the same request.

**Consumer impact:** Consumers see different data after the migration: prices, totals, flags.

### PG2002

`TYPE` · default level `error`

A value has a different JSON type on each side.

**Consumer impact:** The same parse failure a type drift causes, observed on one concrete value.

### PG2003

`MISSING_IN_CANDIDATE` · default level `error`

A key or collection item returned by the baseline is missing from the candidate.

**Consumer impact:** Data disappears for consumers: a row, an attribute, a page entry.

### PG2004

`EXTRA_IN_CANDIDATE` · default level `error`

The candidate returns a key or collection item the baseline did not.

**Consumer impact:** Consumers see data they did not before, which may be an exposure.

### PG2005

`LENGTH` · default level `error`

A collection has a different number of items on each side.

**Consumer impact:** Pagination, counts and 'no results' states diverge.

### PG2006

`ORDER_ONLY` · default level `note`

The same items come back in a different order.

**Consumer impact:** Harmless unless order is part of the contract; then assert on the sort key.

### PG2007

`ORDER` · default level `error`

The same items come back in a different order, and order is compared.

**Consumer impact:** Lists render in a different sequence; 'first item' logic changes.

### PG2008

`KEY_MATCH_UNAVAILABLE` · default level `error`

Identity matching was requested but the key is not unique on both sides.

**Consumer impact:** A configuration problem: differences reported under this collection may be ordering artefacts rather than regressions.

## Assertions (PG3xxx)

### PG3001

`status` · default level `error`

The status code is not the one the case expects.

**Consumer impact:** Clients branch on status: caching, retries and error screens all change.

### PG3002

`status_parity` · default level `error`

Baseline and candidate answer the same request with different statuses.

**Consumer impact:** The classic softened error: a 404 that became an empty 200 is cached as data.

### PG3003

`recorded_status` · default level `error`

The endpoint answers with a status its recorded contract never saw.

**Consumer impact:** The same softened-error risk, caught without a live baseline.

### PG3004

`json` · default level `error`

The response body is not usable JSON.

**Consumer impact:** Every consumer's parser fails on this response.

### PG3005

`required_field` · default level `error`

A field the case declares required is missing.

**Consumer impact:** A field a consumer was named as depending on is gone.

### PG3006

`forbidden_field` · default level `error`

A field the case declares forbidden is present.

**Consumer impact:** Data exposure: something that must not leave the service does.

### PG3007

`graphql_errors` · default level `error`

A GraphQL response carries errors, or lacks the errors the case expects.

**Consumer impact:** Partial data behind a 200: the status code alone reports success.

### PG3008

`latency` · default level `error`

Median latency exceeds the case's budget.

**Consumer impact:** Slower screens and timeouts in clients with tight deadlines.

## Stability (PG4xxx)

### PG4001

`FLAKY_STATUS` · default level `warning`

The status code varied across identical calls.

**Consumer impact:** Nothing else measured from this endpoint means anything until it is fixed.

### PG4002

`FLAKY_SHAPE` · default level `warning`

The response shape varied across identical calls.

**Consumer impact:** Consumers see a different structure depending on timing or cache state.

### PG4003

`VOLATILE_BODY` · default level `note`

Values moved across identical calls while the shape held.

**Consumer impact:** Noise, not breakage: mask the suggested paths so it stops being compared.

## Cases that could not be fully judged (PG5xxx)

### PG5001

`run_error` · default level `error`

The case could not be judged: transport failure, safety refusal or a tool defect.

**Consumer impact:** No verdict about the API exists for this case; treat it as unverified.

### PG5002

`not_gated` · default level `warning`

The case ran but its comparison was skipped.

**Consumer impact:** Its shape is not gated: re-record the contract or stabilise the endpoint.
