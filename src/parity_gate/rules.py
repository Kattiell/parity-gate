"""Stable identifiers for every kind of finding the gate can produce.

A finding kind such as ``FIELD_NOW_OPTIONAL`` is readable, but it is not a
contract: names get refined, and a suite that suppresses a finding by name, a
dashboard that counts it, or a code-scanning alert that tracks it across runs
all break when one does. Linters and API breaking-change checkers solved this
the same way long ago, with a numbered catalogue.

The rules for the catalogue itself:

* **An id is never renumbered and never reused.** A rule that stops existing
  keeps its id retired. ``tests/test_rules.py`` pins the full table, so a
  change to it has to be deliberate and shows up in review.
* **The families are fixed by the thousands digit**: 1 contract drift,
  2 value difference, 3 assertion, 4 stability, 5 a case that could not be
  fully judged.
* **``help`` names the consumer consequence**, not the mechanism, because that
  is what the person reading a pull-request annotation needs to decide on.

``docs/rules.md`` is the human copy of this table.
"""

from __future__ import annotations

from dataclasses import dataclass

DRIFT = "drift"
DIFFERENCE = "difference"
CHECK = "check"
STABILITY = "stability"
RUN = "run"


@dataclass(frozen=True)
class Rule:
    id: str
    #: The kind, check name or verdict this rule is reported under in evidence.
    name: str
    family: str
    summary: str
    help: str
    #: SARIF level when the finding carries no severity of its own.
    level: str = "warning"


RULES: tuple[Rule, ...] = (
    # -- contract drift -------------------------------------------------------
    Rule(
        "PG1001",
        "FIELD_REMOVED",
        DRIFT,
        "A field present in the baseline is absent from the candidate.",
        "Consumers that read the field get undefined or crash; if it was always "
        "present, typed clients fail to deserialise.",
    ),
    Rule(
        "PG1002",
        "FIELD_NOW_OPTIONAL",
        DRIFT,
        "A field that was always present is sometimes missing.",
        "Clients written without a null check for it fail on the responses that omit it.",
    ),
    Rule(
        "PG1003",
        "NULLABLE_ADDED",
        DRIFT,
        "A field can now be null where it never was.",
        "Null-pointer failures in typed clients and 'null' rendered in UIs.",
    ),
    Rule(
        "PG1004",
        "TYPE_CHANGED",
        DRIFT,
        "A field's type was replaced by an incompatible one.",
        "Parsers reject the payload or silently mis-read it (money as a string is the classic).",
    ),
    Rule(
        "PG1005",
        "TYPE_WIDENED",
        DRIFT,
        "A field can now carry a type it did not before.",
        "Safe within the numeric family; any other new type is one existing parsers never saw.",
    ),
    Rule(
        "PG1006",
        "TYPE_NARROWED",
        DRIFT,
        "A field stopped carrying one of the types it used to.",
        "Usually harmless; code paths that handled the dropped type go dead.",
    ),
    Rule(
        "PG1007",
        "FIELD_ADDED",
        DRIFT,
        "A field is new in the candidate.",
        "Ignored by tolerant readers; strict schema validation on the client rejects it.",
        level="note",
    ),
    Rule(
        "PG1008",
        "FIELD_NOW_ALWAYS_PRESENT",
        DRIFT,
        "An optional field is now always present.",
        "Harmless for consumers.",
        level="note",
    ),
    # -- value differences ----------------------------------------------------
    Rule(
        "PG2001",
        "VALUE",
        DIFFERENCE,
        "Baseline and candidate return different values for the same request.",
        "Consumers see different data after the migration: prices, totals, flags.",
        level="error",
    ),
    Rule(
        "PG2002",
        "TYPE",
        DIFFERENCE,
        "A value has a different JSON type on each side.",
        "The same parse failure a type drift causes, observed on one concrete value.",
        level="error",
    ),
    Rule(
        "PG2003",
        "MISSING_IN_CANDIDATE",
        DIFFERENCE,
        "A key or collection item returned by the baseline is missing from the candidate.",
        "Data disappears for consumers: a row, an attribute, a page entry.",
        level="error",
    ),
    Rule(
        "PG2004",
        "EXTRA_IN_CANDIDATE",
        DIFFERENCE,
        "The candidate returns a key or collection item the baseline did not.",
        "Consumers see data they did not before, which may be an exposure.",
        level="error",
    ),
    Rule(
        "PG2005",
        "LENGTH",
        DIFFERENCE,
        "A collection has a different number of items on each side.",
        "Pagination, counts and 'no results' states diverge.",
        level="error",
    ),
    Rule(
        "PG2006",
        "ORDER_ONLY",
        DIFFERENCE,
        "The same items come back in a different order.",
        "Harmless unless order is part of the contract; then assert on the sort key.",
        level="note",
    ),
    Rule(
        "PG2007",
        "ORDER",
        DIFFERENCE,
        "The same items come back in a different order, and order is compared.",
        "Lists render in a different sequence; 'first item' logic changes.",
        level="error",
    ),
    Rule(
        "PG2008",
        "KEY_MATCH_UNAVAILABLE",
        DIFFERENCE,
        "Identity matching was requested but the key is not unique on both sides.",
        "A configuration problem: differences reported under this collection may be "
        "ordering artefacts rather than regressions.",
        level="error",
    ),
    # -- assertions -----------------------------------------------------------
    Rule(
        "PG3001",
        "status",
        CHECK,
        "The status code is not the one the case expects.",
        "Clients branch on status: caching, retries and error screens all change.",
        level="error",
    ),
    Rule(
        "PG3002",
        "status_parity",
        CHECK,
        "Baseline and candidate answer the same request with different statuses.",
        "The classic softened error: a 404 that became an empty 200 is cached as data.",
        level="error",
    ),
    Rule(
        "PG3003",
        "recorded_status",
        CHECK,
        "The endpoint answers with a status its recorded contract never saw.",
        "The same softened-error risk, caught without a live baseline.",
        level="error",
    ),
    Rule(
        "PG3004",
        "json",
        CHECK,
        "The response body is not usable JSON.",
        "Every consumer's parser fails on this response.",
        level="error",
    ),
    Rule(
        "PG3005",
        "required_field",
        CHECK,
        "A field the case declares required is missing.",
        "A field a consumer was named as depending on is gone.",
        level="error",
    ),
    Rule(
        "PG3006",
        "forbidden_field",
        CHECK,
        "A field the case declares forbidden is present.",
        "Data exposure: something that must not leave the service does.",
        level="error",
    ),
    Rule(
        "PG3007",
        "graphql_errors",
        CHECK,
        "A GraphQL response carries errors, or lacks the errors the case expects.",
        "Partial data behind a 200: the status code alone reports success.",
        level="error",
    ),
    Rule(
        "PG3008",
        "latency",
        CHECK,
        "Median latency exceeds the case's budget.",
        "Slower screens and timeouts in clients with tight deadlines.",
        level="error",
    ),
    # -- stability ------------------------------------------------------------
    Rule(
        "PG4001",
        "FLAKY_STATUS",
        STABILITY,
        "The status code varied across identical calls.",
        "Nothing else measured from this endpoint means anything until it is fixed.",
    ),
    Rule(
        "PG4002",
        "FLAKY_SHAPE",
        STABILITY,
        "The response shape varied across identical calls.",
        "Consumers see a different structure depending on timing or cache state.",
    ),
    Rule(
        "PG4003",
        "VOLATILE_BODY",
        STABILITY,
        "Values moved across identical calls while the shape held.",
        "Noise, not breakage: mask the suggested paths so it stops being compared.",
        level="note",
    ),
    # -- cases that could not be fully judged ---------------------------------
    Rule(
        "PG5001",
        "run_error",
        RUN,
        "The case could not be judged: transport failure, safety refusal or a tool defect.",
        "No verdict about the API exists for this case; treat it as unverified.",
        level="error",
    ),
    Rule(
        "PG5002",
        "not_gated",
        RUN,
        "The case ran but its comparison was skipped.",
        "Its shape is not gated: re-record the contract or stabilise the endpoint.",
    ),
)

BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}
_BY_FAMILY_NAME: dict[tuple[str, str], Rule] = {(rule.family, rule.name): rule for rule in RULES}


def lookup(family: str, name: str) -> Rule | None:
    """The rule reported under ``name`` in ``family``, if there is one."""
    return _BY_FAMILY_NAME.get((family, name))


def rule_id(family: str, name: str) -> str | None:
    rule = lookup(family, name)
    return rule.id if rule else None


def resolve(token: str) -> Rule:
    """Find a rule by id (``PG1002``) or by the name findings carry.

    Names are accepted so a suite can say ``rule = "FIELD_NOW_OPTIONAL"`` and
    still be pinned to an id; a name shared by two families is refused, because
    guessing which one was meant is how a suppression lands on the wrong thing.
    """
    text = token.strip()
    if text.upper() in BY_ID:
        return BY_ID[text.upper()]
    matches = [rule for rule in RULES if rule.name == text]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ", ".join(rule.id for rule in matches)
        raise KeyError(f"rule name {token!r} is ambiguous; use one of {options}")
    raise KeyError(f"unknown rule {token!r}; see docs/rules.md for the catalogue")
