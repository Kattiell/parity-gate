"""GraphQL: the two things HTTP gives for free and this protocol does not:
the operation type, and a status code that means anything."""

from __future__ import annotations

from pathlib import Path

import pytest

from parity_gate.graphql import build_body, describe, errors_in, is_read, operation_type
from parity_gate.suite import SuiteError, load


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("{ products { id } }", "query"),  # anonymous shorthand
        ("  \n query Q { a }", "query"),
        ("QUERY Q { a }", "query"),  # case-insensitive
        ("mutation M { b }", "mutation"),
        ("subscription S { c }", "subscription"),
        ("# a comment\nquery { a }", "query"),
        ("", "query"),
    ],
)
def test_the_operation_type_comes_from_the_document(document: str, expected: str) -> None:
    assert operation_type(document) == expected


def test_a_named_operation_that_is_not_shorthand_is_treated_as_a_write() -> None:
    """Fail closed: an unrecognised document is not assumed to be safe."""
    assert not is_read("fragment F on T { a }")


def test_the_body_carries_only_what_was_given() -> None:
    assert build_body("{ a }", None, None) == {"query": "{ a }"}
    assert build_body("{ a }", {"x": 1}, "Q") == {
        "query": "{ a }",
        "variables": {"x": 1},
        "operationName": "Q",
    }


def test_errors_are_found_whatever_shape_they_arrive_in() -> None:
    assert errors_in({"data": {}, "errors": [{"message": "boom"}]}) == [{"message": "boom"}]
    assert errors_in({"data": {}}) == []
    assert errors_in({"errors": []}) == []
    assert errors_in("not an object") == []
    assert describe([{"message": "boom"}, {"message": "bang"}]) == "boom; bang"


GQL = """
name = "g"
[targets.baseline]
base_url = "http://127.0.0.1:9/a"
[targets.candidate]
base_url = "http://127.0.0.1:9/b"
[policy]
allowed_hosts = ["127.0.0.1"]
[[cases]]
id = "G-1"
path = "/graphql"
graphql = "{ products { id } }"
"""


def write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "g.toml"
    target.write_text(body, encoding="utf-8")
    return target


def test_a_graphql_case_is_a_post_that_reads(tmp_path: Path) -> None:
    """The safety point. Every GraphQL operation is a POST, so leaving the
    write gate to the method would force every query to be declared a mutation,
    and a control everyone switches off protects nothing."""
    case = load(write(tmp_path, GQL)).cases[0]
    assert case.method == "POST"
    assert case.reads_only is True
    assert case.mutating is False
    assert case.request_body() == {"query": "{ products { id } }"}


def test_a_graphql_mutation_is_not_read_only(tmp_path: Path) -> None:
    body = GQL.replace('graphql = "{ products { id } }"', 'graphql = "mutation { buy { id } }"')
    case = load(write(tmp_path, body)).cases[0]
    assert case.reads_only is False  # still needs `mutating` and --allow-mutations


def test_variables_and_operation_name_reach_the_body(tmp_path: Path) -> None:
    body = GQL + '\noperation_name = "P"\n[cases.variables]\nlimit = 5\n'
    case = load(write(tmp_path, body)).cases[0]
    assert case.request_body() == {
        "query": "{ products { id } }",
        "variables": {"limit": 5},
        "operationName": "P",
    }


def test_a_subscription_is_refused_with_a_reason(tmp_path: Path) -> None:
    body = GQL.replace('graphql = "{ products { id } }"', 'graphql = "subscription { t }"')
    with pytest.raises(SuiteError, match="long-lived streams"):
        load(write(tmp_path, body))


def test_graphql_and_body_together_are_refused(tmp_path: Path) -> None:
    body = GQL + "\n[cases.body]\nx = 1\n"
    with pytest.raises(SuiteError, match="not both"):
        load(write(tmp_path, body))


def test_a_graphql_case_cannot_be_a_get(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="always a POST"):
        load(write(tmp_path, GQL + '\nmethod = "GET"\n'))


def test_an_empty_document_is_refused(tmp_path: Path) -> None:
    body = GQL.replace('graphql = "{ products { id } }"', 'graphql = "   "')
    with pytest.raises(SuiteError, match="present but empty"):
        load(write(tmp_path, body))
