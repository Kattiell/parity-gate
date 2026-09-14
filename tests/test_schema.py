"""Drift classification is the core judgement call, so each severity is pinned
by a test that names the consumer consequence it stands for."""

from __future__ import annotations

from parity_gate.schema import Severity, compare, infer, render_path, type_of, worst


def kinds(base, cand) -> dict[str, tuple[str, str]]:
    drifts, _ = compare(infer(*base), infer(*cand))
    return {d.path: (d.kind, d.severity.value) for d in drifts}


def test_type_names_distinguish_bool_from_int() -> None:
    assert type_of(True) == "boolean"
    assert type_of(1) == "integer"
    assert type_of(1.5) == "number"
    assert type_of(None) == "null"


def test_paths_render_readably_and_quote_odd_keys() -> None:
    schema = infer({"products": [{"id": 1}], "a.b": 2})
    rendered = set(schema.to_dict()["fields"])
    assert "$.products[].id" in rendered
    assert '$["a.b"]' in rendered
    assert render_path(()) == "$"


def test_identical_payloads_produce_no_drift() -> None:
    payload = {"id": 1, "tags": ["x"], "nested": {"a": 1.0}}
    assert compare(infer(payload), infer(payload)) == ([], [])


def test_removing_an_always_present_field_is_breaking() -> None:
    assert kinds([{"id": 1, "stock": 3}], [{"id": 1}])["$.stock"] == ("FIELD_REMOVED", "breaking")


def test_removing_an_optional_field_is_only_risky() -> None:
    base = [{"id": 1, "note": "x"}, {"id": 2}]
    assert kinds(base, [{"id": 1}, {"id": 2}])["$.note"] == ("FIELD_REMOVED", "risky")


def test_incompatible_type_change_is_breaking() -> None:
    assert kinds([{"price": 19.9}], [{"price": "19.90"}])["$.price"] == ("TYPE_CHANGED", "breaking")


def test_becoming_nullable_is_breaking_because_consumers_crash_on_null() -> None:
    base = [{"discount": 0.1}, {"discount": 0.0}]
    cand = [{"discount": 0.1}, {"discount": None}]
    assert kinds(base, cand)["$.discount"] == ("NULLABLE_ADDED", "breaking")


def test_integer_to_number_is_widening_not_breaking() -> None:
    assert kinds([{"qty": 3}], [{"qty": 3.5}])["$.qty"] == ("TYPE_WIDENED", "risky")


def test_a_new_field_is_additive() -> None:
    assert kinds([{"id": 1}], [{"id": 1, "warehouseId": 7}])["$.warehouseId"] == (
        "FIELD_ADDED",
        "additive",
    )


def test_a_field_that_stops_being_guaranteed_is_breaking() -> None:
    base = [{"id": 1, "sku": "a"}, {"id": 2, "sku": "b"}]
    cand = [{"id": 1, "sku": "a"}, {"id": 2}]
    assert kinds(base, cand)["$.sku"] == ("FIELD_NOW_OPTIONAL", "breaking")


def test_an_empty_array_does_not_look_like_a_removed_field() -> None:
    # The classic false alarm: page two happens to be empty.
    base = infer({"products": [{"id": 1}]}, {"products": []})
    cand = infer({"products": [{"id": 1}]}, {"products": []})
    assert compare(base, cand) == ([], [])


def test_a_collection_that_is_empty_today_is_unchecked_not_broken() -> None:
    """The regression that would have made the gate unusable in production.

    The contract was recorded on a day the catalogue had rows. Today the filter
    matches nothing. Every item field then looks "removed": five breaking
    findings for a healthy service, on any endpoint that can return an empty
    page, which is most of them.
    """
    recorded = infer({"products": [{"id": 1, "price": 9.9, "tags": ["a"]}], "total": 1})
    today = infer({"products": [], "total": 0})

    drifts, unchecked = compare(recorded, today)
    assert drifts == []
    assert "$.products[].price" in unchecked
    assert "$.products[].tags[]" in unchecked


def test_the_reverse_is_first_sight_not_an_addition() -> None:
    recorded = infer({"products": [], "total": 0})
    today = infer({"products": [{"id": 1, "price": 9.9}], "total": 1})

    drifts, unchecked = compare(recorded, today)
    assert drifts == []
    assert "$.products[].price" in unchecked


def test_an_empty_collection_does_not_hide_a_real_removal() -> None:
    """The fix must not become a blanket amnesty for anything under an array."""
    base = infer({"products": [{"id": 1, "price": 9.9}]})
    cand = infer({"products": [{"id": 1}]})

    drifts, unchecked = compare(base, cand)
    assert [(d.kind, d.path) for d in drifts] == [("FIELD_REMOVED", "$.products[].price")]
    assert unchecked == []


def test_the_collection_itself_disappearing_is_still_breaking() -> None:
    base = infer({"products": [{"id": 1}]})
    cand = infer({"total": 0})

    drifts, _ = compare(base, cand)
    assert ("FIELD_REMOVED", "$.products") in [(d.kind, d.path) for d in drifts]


def test_fingerprint_ignores_values_but_not_structure() -> None:
    assert infer({"a": 1}).fingerprint() == infer({"a": 99}).fingerprint()
    assert infer({"a": 1}).fingerprint() != infer({"a": "1"}).fingerprint()
    assert infer({"a": 1}).fingerprint() != infer({"a": 1, "b": 2}).fingerprint()


def test_findings_are_ordered_worst_first() -> None:
    drifts, _ = compare(
        infer({"keep": 1, "gone": 2}),
        infer({"keep": "1", "added": 3}),
    )
    assert [d.severity for d in drifts][:2] == [Severity.BREAKING, Severity.BREAKING]
    assert drifts[-1].severity is Severity.ADDITIVE
    assert worst(drifts) is Severity.BREAKING


def test_worst_of_nothing_is_none() -> None:
    assert worst([]) is None
