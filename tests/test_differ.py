"""The differ decides what a reviewer sees, so the tests focus on noise control
as much as on correctness."""

from __future__ import annotations

from parity_gate.differ import DiffOptions, canonical, diff, is_masked


def kinds(base, cand, options=None) -> list[tuple[str, str]]:
    return [(d.kind, d.path) for d in diff(base, cand, options)]


def test_identical_documents_produce_nothing() -> None:
    doc = {"a": 1, "b": [1, 2, {"c": None}]}
    assert diff(doc, doc) == []


def test_scalar_change_is_reported_with_both_sides() -> None:
    (difference,) = diff({"total": 4}, {"total": 3})
    assert (difference.kind, difference.path) == ("VALUE", "$.total")
    assert (difference.baseline, difference.candidate) == (4, 3)


def test_missing_and_extra_keys_are_distinguished() -> None:
    assert kinds({"a": 1}, {"b": 1}) == [
        ("MISSING_IN_CANDIDATE", "$.a"),
        ("EXTRA_IN_CANDIDATE", "$.b"),
    ]


def test_masked_paths_are_never_compared() -> None:
    base = {"meta": {"requestId": "a"}, "total": 1}
    cand = {"meta": {"requestId": "b"}, "total": 1}
    assert diff(base, cand) != []
    assert diff(base, cand, DiffOptions(mask_paths=["$.meta.requestId"])) == []


def test_a_mask_written_with_empty_brackets_covers_every_item() -> None:
    base = {"items": [{"ts": 1}, {"ts": 2}]}
    cand = {"items": [{"ts": 9}, {"ts": 8}]}
    assert diff(base, cand, DiffOptions(mask_paths=["$.items[].ts"])) == []


def test_reordering_is_one_finding_not_one_per_index() -> None:
    base = {"items": [1, 2, 3]}
    cand = {"items": [3, 2, 1]}
    assert kinds(base, cand) == [("ORDER_ONLY", "$.items")]


def test_collections_are_matched_by_identity_when_a_key_is_given() -> None:
    base = {"products": [{"id": 1, "price": 10}, {"id": 2, "price": 20}]}
    cand = {"products": [{"id": 2, "price": 20}, {"id": 1, "price": 11}]}
    options = DiffOptions(array_keys={"$.products": "id"})
    found = kinds(base, cand, options)
    # One ordering note plus the single real change, addressed by identity.
    assert ("ORDER_ONLY", "$.products") in found
    assert ("VALUE", "$.products[id=1].price") in found
    assert len(found) == 2


def test_positional_matching_would_have_reported_both_items() -> None:
    base = {"products": [{"id": 1, "price": 10}, {"id": 2, "price": 20}]}
    cand = {"products": [{"id": 2, "price": 20}, {"id": 1, "price": 11}]}
    assert len(kinds(base, cand)) > 2  # this is the noise array_keys removes


def test_items_only_on_one_side_are_named_by_their_identity() -> None:
    base = {"products": [{"id": 1}, {"id": 2}]}
    cand = {"products": [{"id": 1}, {"id": 3}]}
    options = DiffOptions(array_keys={"$.products": "id"})
    found = dict(kinds(base, cand, options))
    assert found["MISSING_IN_CANDIDATE"] == "$.products[id=2]"
    assert found["EXTRA_IN_CANDIDATE"] == "$.products[id=3]"


def test_key_matching_falls_back_when_the_key_is_not_unique() -> None:
    base = {"products": [{"id": 1}, {"id": 1}]}
    cand = {"products": [{"id": 1}, {"id": 2}]}
    found = kinds(base, cand, DiffOptions(array_keys={"$.products": "id"}))
    assert any(path.startswith("$.products[1]") for _, path in found)


def test_length_change_is_reported_once() -> None:
    found = kinds({"items": [1, 2, 3]}, {"items": [1, 2]})
    assert ("LENGTH", "$.items") in found


def test_float_tolerance_is_off_by_default_because_money() -> None:
    assert kinds({"v": 1.0}, {"v": 1.0000001}) == [("VALUE", "$.v")]
    assert diff({"v": 1.0}, {"v": 1.0000001}, DiffOptions(float_tolerance=1e-6)) == []


def test_integer_and_float_of_equal_value_are_not_a_type_change() -> None:
    assert diff({"v": 1}, {"v": 1.0}) == []


def test_output_is_capped_so_one_change_cannot_flood_a_report() -> None:
    base = {"items": [{"n": i} for i in range(500)]}
    cand = {"items": [{"n": -i} for i in range(500)]}
    assert len(diff(base, cand, DiffOptions(max_differences=10))) == 10


def test_canonical_form_ignores_key_order_and_blanks_masked_paths() -> None:
    assert canonical({"a": 1, "b": 2}) == canonical({"b": 2, "a": 1})
    assert canonical({"ts": 1}, ["$.ts"]) == canonical({"ts": 2}, ["$.ts"])


def test_is_masked_matches_generalised_and_literal_paths() -> None:
    assert is_masked("$.items[0].ts", ["$.items[].ts"])
    assert is_masked("$.items[id=7].ts", ["$.items[].ts"])
    assert not is_masked("$.items[0].value", ["$.items[].ts"])
    assert not is_masked("$.anything", [])
