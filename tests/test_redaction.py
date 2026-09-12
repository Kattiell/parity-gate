"""Redaction is the control that keeps evidence safe to share, so it is tested
for both directions: secrets must disappear, ordinary data must survive."""

from __future__ import annotations

import pytest

from parity_gate.redaction import MASK, find_secrets, redact, redact_headers, redact_text


@pytest.mark.parametrize(
    "secret",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
        "Bearer sk_live_9f8a7b6c5d4e3f2a1b",
        "ghp_16CharactersMinimumAAAA",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    ],
)
def test_known_credential_shapes_are_masked(secret: str) -> None:
    assert MASK in redact_text(f"value={secret} end")
    assert secret not in redact_text(f"value={secret} end")


def test_personal_data_is_masked() -> None:
    text = "contato gabriel@example.com CPF 123.456.789-00 CNPJ 12.345.678/0001-99"
    cleaned = redact_text(text)
    assert "example.com" not in cleaned
    assert "123.456.789-00" not in cleaned
    assert "12.345.678/0001-99" not in cleaned


def test_ordinary_values_are_left_alone() -> None:
    payload = {"id": 42, "title": "Impact drill 750W", "price": 459.0, "tags": ["a", "b"]}
    assert redact(payload) == payload


def test_sensitive_keys_are_masked_whatever_their_shape() -> None:
    payload = {"user": {"name": "Gabriel", "accessToken": "plain", "Api-Key": "x"}}
    cleaned = redact(payload)
    assert cleaned["user"]["name"] == "Gabriel"
    assert cleaned["user"]["accessToken"] == MASK
    assert cleaned["user"]["Api-Key"] == MASK


def test_redaction_reaches_into_nested_lists() -> None:
    payload = {"items": [{"password": "hunter2"}, {"password": "hunter3"}]}
    assert [item["password"] for item in redact(payload)["items"]] == [MASK, MASK]


def test_url_credentials_are_removed() -> None:
    assert "hunter2" not in redact_text("postgres://admin:hunter2@db.internal:5432/app")


def test_authorization_header_is_masked_by_name_not_by_content() -> None:
    headers = redact_headers({"Authorization": "Opaque abc", "Accept": "application/json"})
    assert headers["Authorization"] == MASK
    assert headers["Accept"] == "application/json"


def test_recursion_is_bounded() -> None:
    deep: dict = {}
    node = deep
    for _ in range(200):
        node["next"] = {}
        node = node["next"]
    redact(deep)  # must not raise RecursionError


def test_find_secrets_ignores_personal_data() -> None:
    # Personal data is redacted from evidence but must not block a run.
    assert find_secrets("gabriel@example.com") == []
    assert find_secrets("ghp_16CharactersMinimumAAAA") == ["github-token"]
