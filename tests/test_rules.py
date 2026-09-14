"""Rule ids are a public contract: pinned here so a change to one is a decision.

Suppressions in suite files, SARIF alerts tracked across runs and anyone
counting findings all key on these ids. Renumbering one silently orphans all
of that, so the full table is written out below and has to be edited on
purpose, in review, for anything to change.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from parity_gate import rules

SRC = Path(__file__).resolve().parent.parent / "src" / "parity_gate"

PINNED = [
    ("PG1001", "FIELD_REMOVED", "drift"),
    ("PG1002", "FIELD_NOW_OPTIONAL", "drift"),
    ("PG1003", "NULLABLE_ADDED", "drift"),
    ("PG1004", "TYPE_CHANGED", "drift"),
    ("PG1005", "TYPE_WIDENED", "drift"),
    ("PG1006", "TYPE_NARROWED", "drift"),
    ("PG1007", "FIELD_ADDED", "drift"),
    ("PG1008", "FIELD_NOW_ALWAYS_PRESENT", "drift"),
    ("PG2001", "VALUE", "difference"),
    ("PG2002", "TYPE", "difference"),
    ("PG2003", "MISSING_IN_CANDIDATE", "difference"),
    ("PG2004", "EXTRA_IN_CANDIDATE", "difference"),
    ("PG2005", "LENGTH", "difference"),
    ("PG2006", "ORDER_ONLY", "difference"),
    ("PG2007", "ORDER", "difference"),
    ("PG2008", "KEY_MATCH_UNAVAILABLE", "difference"),
    ("PG3001", "status", "check"),
    ("PG3002", "status_parity", "check"),
    ("PG3003", "recorded_status", "check"),
    ("PG3004", "json", "check"),
    ("PG3005", "required_field", "check"),
    ("PG3006", "forbidden_field", "check"),
    ("PG3007", "graphql_errors", "check"),
    ("PG3008", "latency", "check"),
    ("PG4001", "FLAKY_STATUS", "stability"),
    ("PG4002", "FLAKY_SHAPE", "stability"),
    ("PG4003", "VOLATILE_BODY", "stability"),
    ("PG5001", "run_error", "run"),
    ("PG5002", "not_gated", "run"),
]


def test_the_catalogue_matches_the_pinned_table_exactly() -> None:
    assert [(r.id, r.name, r.family) for r in rules.RULES] == PINNED


def test_ids_are_unique_and_the_thousands_digit_names_the_family() -> None:
    families = {"1": "drift", "2": "difference", "3": "check", "4": "stability", "5": "run"}
    assert len({r.id for r in rules.RULES}) == len(rules.RULES)
    for rule in rules.RULES:
        assert re.fullmatch(r"PG\d{4}", rule.id)
        assert families[rule.id[2]] == rule.family
        assert rule.help and rule.summary
        assert rule.level in {"error", "warning", "note"}


@pytest.mark.parametrize(
    ("module", "pattern", "family"),
    [
        ("schema.py", r'kind="([A-Z_]+)"', rules.DRIFT),
        ("schema.py", r'"(TYPE_WIDENED)" if', rules.DRIFT),
        ("schema.py", r'else "(TYPE_NARROWED)"', rules.DRIFT),
        ("differ.py", r'kind="([A-Z_]+)"', rules.DIFFERENCE),
        ("differ.py", r'"(ORDER(?:_ONLY)?)"', rules.DIFFERENCE),
        ("runner.py", r'_check\(\s*"([a-z_]+)"', rules.CHECK),
    ],
)
def test_every_finding_the_code_can_emit_has_a_rule(module: str, pattern: str, family: str) -> None:
    """A new kind of finding without a rule would reach evidence and SARIF with
    no id, and nothing else would notice."""
    source = (SRC / module).read_text(encoding="utf-8")
    emitted = set(re.findall(pattern, source))
    assert emitted, f"pattern found nothing in {module}; the test has gone stale"
    unmapped = {name for name in emitted if rules.lookup(family, name) is None}
    assert not unmapped


def test_a_rule_resolves_by_id_or_by_name_and_refuses_what_it_cannot_place() -> None:
    assert rules.resolve("PG1002").name == "FIELD_NOW_OPTIONAL"
    assert rules.resolve("pg1002").id == "PG1002"
    assert rules.resolve("FIELD_NOW_OPTIONAL").id == "PG1002"
    with pytest.raises(KeyError, match="unknown rule"):
        rules.resolve("FIELD_VANISHED")


def test_the_human_catalogue_lists_every_rule_under_its_own_anchor() -> None:
    """SARIF helpUri links to docs/rules.md#pg1004, so every id needs a heading."""
    text = (SRC.parent.parent / "docs" / "rules.md").read_text(encoding="utf-8")
    for rule in rules.RULES:
        assert f"### {rule.id}\n\n`{rule.name}`" in text, rule.id
