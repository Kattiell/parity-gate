"""Redaction is the control that keeps evidence safe to share, so it is tested
for both directions: secrets must disappear, ordinary data must survive.

parity-gate:allow-secrets-file - every credential-shaped string below is a
fixture or a pattern definition, never a live value.

Fixtures in a provider's exact format are split at the prefix. The runtime
value is identical, but the source no longer matches push-protection patterns:
GitHub opened a public-leak alert for the Google key, which was never real.
The split uses an explicit `+`: ruff format rejoins implicit concatenation
that fits on one line, which would silently put the literal back.
"""

from __future__ import annotations

import pytest

from parity_gate.redaction import MASK, find_secrets, redact, redact_headers, redact_text


@pytest.mark.parametrize(
    "secret",
    [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
        "Bearer sk_" + "live_9f8a7b6c5d4e3f2a1b",
        "ghp_16CharactersMinimumAAAA",
        "xo" + "xb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "AI" + "zaSyA1234567890abcdefghijklmnopqrstuv",
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


@pytest.mark.parametrize(
    "legitimate",
    [
        "7891234567890",  # EAN-13 barcode
        "5534991234567",  # Brazilian phone with country code
        "1757650331123456789",  # nanosecond timestamp
        "12345678000199",  # CNPJ without punctuation
    ],
)
def test_long_digit_runs_that_are_not_cards_survive(legitimate: str) -> None:
    """Evidence with a mangled barcode is evidence you cannot attach to a ticket.

    The card rule used to mask any run of 13-19 digits, which destroyed all of
    these while a 23-digit order number passed because it was one digit past
    the upper bound. Luhn is what makes the rule mean something.
    """
    assert redact({"v": legitimate})["v"] == legitimate


@pytest.mark.parametrize(
    "pan",
    ["4111111111111111", "5500005555555559", "378282246310005", "4111 1111 1111 1111"],
)
def test_real_card_numbers_are_still_masked(pan: str) -> None:
    assert redact({"v": pan})["v"] == MASK


def test_luhn_is_what_decides() -> None:
    from parity_gate.redaction import luhn

    assert luhn("4111111111111111")
    assert not luhn("4111111111111112")
    assert not luhn("123")  # too short to be a card at all


def test_english_prose_about_tokens_is_not_a_finding() -> None:
    """The scanner's own false positive, found by running it on this repository.

    "a token supplied through the environment" matched, because anything at all
    after the word counted as the credential. A scanner that flags its own
    documentation gets an exclusion, and then it stops scanning the directory
    that matters.
    """
    prose = "An integration test asserts a token supplied through the environment never leaks."
    assert find_secrets(prose) == []
    assert redact({"v": prose})["v"] == prose


def test_real_http_auth_values_are_still_caught() -> None:
    for value in ("Bearer ghp_16CharsMinimumAA", "Basic Z2FicmllbDpodW50ZXIyMzQ1Ng=="):
        assert redact({"v": value})["v"] == MASK


def test_an_empty_value_under_a_sensitive_key_is_left_visible() -> None:
    """Masking null protects nothing and hides whether anything was sent.

    Found end to end: the mock echoes the Authorization header it received, and
    the side that sent no credential reported "[REDACTED]" for a null, which
    made the evidence say the opposite of what happened.
    """
    assert redact({"authorization": None})["authorization"] is None
    assert redact({"token": ""})["token"] == ""
    assert redact({"password": "hunter2"})["password"] == MASK
