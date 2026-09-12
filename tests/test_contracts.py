"""Recorded contracts have to survive a round trip through a file, and the file
has to be reviewable in a pull request. Both are tested here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from parity_gate.contracts import Contract, ContractError, RecordedCase, load, now, save
from parity_gate.schema import compare, infer, parse_path, render_path


@pytest.mark.parametrize(
    "display",
    [
        "$",
        "$.id",
        "$.products[]",
        "$.products[].price",
        "$.a.b.c",
        "$[]",
        '$["a.b"]',
        '$["with space"]',
        '$["123numeric"]',
        '$[""]',
        '$.products[]["odd key"].id',
    ],
)
def test_paths_round_trip_through_their_readable_spelling(display: str) -> None:
    assert render_path(parse_path(display)) == display


def test_a_malformed_path_is_rejected_rather_than_guessed() -> None:
    for bad in ("products.id", "$.a[", "$(x)"):
        with pytest.raises(ValueError):
            parse_path(bad)


def test_a_schema_survives_the_trip_to_a_file_and_back() -> None:
    original = infer(
        {"id": 1, "price": 19.9, "tags": ["a"], "meta": {"note": None}, "sometimes": 1},
        {"id": 2, "price": 20.0, "tags": [], "meta": {"note": "x"}},
    )
    restored = type(original).from_portable(original.to_portable())

    assert set(restored.fields) == set(original.fields)
    assert restored.fingerprint() == original.fingerprint()
    for path in original.fields:
        assert restored.fields[path].type_names == original.fields[path].type_names
        assert restored.is_required(path) == original.is_required(path)


def test_a_restored_schema_compares_identically_to_the_original() -> None:
    """The point of recording: comparing against the file must equal comparing
    against the service it was recorded from."""
    baseline = infer({"id": 1, "price": 19.9, "stock": 3}, {"id": 2, "price": 20.0, "stock": 0})
    candidate = infer({"id": 1, "price": "19.90"}, {"id": 2, "price": "20.00"})
    restored = type(baseline).from_portable(baseline.to_portable())

    live = [(d.path, d.kind, d.severity) for d in compare(baseline, candidate)[0]]
    recorded = [(d.path, d.kind, d.severity) for d in compare(restored, candidate)[0]]
    assert recorded == live


def make_contract() -> Contract:
    return Contract(
        suite_name="s",
        source_url="https://api.example.com",
        recorded_at=now(),
        cases={
            "A-1": RecordedCase(
                schema=infer({"id": 1, "price": 19.9}),
                statuses=[200, 200, 200],
                stability="STABLE",
            ),
            "A-2": RecordedCase(
                schema=infer({"error": "not found"}), statuses=[404], stability="STABLE"
            ),
        },
    )


def test_a_contract_survives_save_and_load(tmp_path: Path) -> None:
    path = save(make_contract(), tmp_path / "contract.json")
    restored = load(path)

    assert set(restored.cases) == {"A-1", "A-2"}
    assert restored.cases["A-1"].statuses == [200]  # de-duplicated on write
    assert restored.cases["A-2"].statuses == [404]
    assert restored.source_url == "https://api.example.com"
    assert "$.price" in {f["path"] for f in _fields(path, "A-1")}


def test_the_file_is_lf_only_and_one_field_per_line(tmp_path: Path) -> None:
    path = save(make_contract(), tmp_path / "contract.json")
    raw = path.read_bytes()
    assert b"\r\n" not in raw

    text = raw.decode()
    # A reviewer must see a type change as a one-line diff, not an eight-line one.
    assert '{"path": "$.price", "types": ["number"], "required": true}' in text


def test_values_are_never_recorded(tmp_path: Path) -> None:
    """A recorded value goes stale the moment the data changes. See ADR-001."""
    path = save(make_contract(), tmp_path / "contract.json")
    text = path.read_text(encoding="utf-8")
    assert "19.9" not in text
    assert "not found" not in text


def test_a_missing_contract_says_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="parity-gate record"):
        load(tmp_path / "absent.json")


def test_an_unknown_schema_version_is_refused_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({"schema_version": 99, "cases": {}}), encoding="utf-8")
    with pytest.raises(ContractError, match="schema_version"):
        load(path)


def test_invalid_json_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid JSON"):
        load(path)


def _fields(path: Path, case_id: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"][case_id]["schema"]["fields"]
