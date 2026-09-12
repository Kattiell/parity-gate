"""Stability triage: the four verdicts, and the mask suggestion that makes
VOLATILE_BODY actionable rather than merely informative."""

from __future__ import annotations

from parity_gate.differ import DiffOptions
from parity_gate.flaky import FLAKY_SHAPE, FLAKY_STATUS, STABLE, VOLATILE_BODY, classify


def test_identical_answers_are_stable() -> None:
    result = classify([200, 200, 200], [{"a": 1}] * 3, [10.0, 11.0, 12.0])
    assert result.verdict == STABLE
    assert result.trustworthy


def test_a_changing_status_outranks_everything_else() -> None:
    result = classify([200, 503, 200], [{"a": 1}, {"error": "busy"}, {"a": 1}], [1.0, 2.0, 3.0])
    assert result.verdict == FLAKY_STATUS
    assert not result.trustworthy
    assert "200" in result.detail and "503" in result.detail


def test_a_changing_shape_is_reported_separately_from_changing_values() -> None:
    result = classify([200, 200], [{"a": 1}, {"a": 1, "b": 2}], [1.0, 1.0])
    assert result.verdict == FLAKY_SHAPE
    assert not result.trustworthy


def test_moving_values_are_volatile_not_broken() -> None:
    result = classify([200, 200], [{"uptime": 1}, {"uptime": 2}], [1.0, 1.0])
    assert result.verdict == VOLATILE_BODY
    assert result.trustworthy  # a diff can still be taken, with a mask


def test_volatile_paths_are_suggested_in_maskable_form() -> None:
    payloads = [
        {"items": [{"id": 1, "ts": 10}, {"id": 2, "ts": 20}]},
        {"items": [{"id": 1, "ts": 11}, {"id": 2, "ts": 21}]},
    ]
    result = classify([200, 200], payloads, [1.0, 1.0])
    assert result.volatile_paths == ["$.items[].ts"]


def test_an_existing_mask_removes_the_volatility_it_covers() -> None:
    payloads = [{"ts": 1, "v": 1}, {"ts": 2, "v": 1}]
    result = classify([200, 200], payloads, [1.0, 1.0], DiffOptions(mask_paths=["$.ts"]))
    assert result.verdict == STABLE


def test_a_single_sample_makes_no_claim_about_stability() -> None:
    result = classify([200], [{"a": 1}], [5.0])
    assert result.verdict == STABLE
    assert "not measured" in result.detail


def test_latency_percentiles_come_from_the_samples() -> None:
    result = classify([200] * 3, [{"a": 1}] * 3, [10.0, 20.0, 90.0])
    assert result.p50_ms == 20.0
    assert result.max_ms == 90.0
