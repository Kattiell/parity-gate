"""Importing a spec: a starter suite, not an answer. The generated file has to
load, and the cases it cannot fill in have to be visible rather than wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from parity_gate.cli import EXIT_OK, EXIT_USAGE, main
from parity_gate.openapi import OpenAPIError, base_url_of, load_spec, to_suite
from parity_gate.suite import load

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Catalog API", "version": "2.1.0"},
    "servers": [{"url": "https://api.staging.example.com/v2"}],
    "paths": {
        "/products": {
            "get": {"operationId": "listProducts", "summary": "List products", "tags": ["catalog"]},
            "post": {"operationId": "createProduct", "summary": "Create", "tags": ["catalog"]},
        },
        "/products/{productId}": {
            "parameters": [
                {"name": "productId", "in": "path", "schema": {"type": "integer", "example": 42}}
            ],
            "get": {"operationId": "getProduct", "summary": "Fetch one", "tags": ["catalog"]},
        },
        "/orders/{orderId}": {
            "get": {
                "operationId": "getOrder",
                "tags": ["orders"],
                "parameters": [{"name": "orderId", "in": "path", "schema": {"type": "string"}}],
            }
        },
        "/health": {"get": {"summary": "Liveness", "tags": ["ops"]}},
    },
}


def render(**overrides) -> str:
    options = {"name": "gen", "base_url": "", "contract_path": "contracts/gen.json"}
    options.update(overrides)
    return to_suite(SPEC, **options)


def test_the_generated_suite_is_a_suite_this_tool_can_load(tmp_path: Path) -> None:
    target = tmp_path / "gen.toml"
    target.write_text(render(), encoding="utf-8")

    suite = load(target)
    assert [c.id for c in suite.cases] == ["GET-HEALTH", "LISTPRODUCTS", "GETPRODUCT"]
    assert suite.policy.allowed_hosts == ["api.staging.example.com"]
    assert suite.policy.max_retries == 0
    assert suite.baseline.snapshot == "contracts/gen.json"


def test_writes_are_left_out_unless_asked_for(tmp_path: Path) -> None:
    target = tmp_path / "safe.toml"
    target.write_text(render(), encoding="utf-8")
    assert all(c.method == "GET" for c in load(target).cases)

    target.write_text(render(include_writes=True), encoding="utf-8")
    created = {c.id: c for c in load(target).cases}["CREATEPRODUCT"]
    assert created.method == "POST"
    assert created.mutating is True  # and --allow-mutations is still required


def test_a_path_parameter_with_no_example_is_commented_out_not_guessed() -> None:
    """Inventing an order id would produce a case that fails for the wrong
    reason, which is worse than a case that does not run."""
    text = render()
    assert "# TODO: no example for path parameter(s) orderId" in text
    assert "# path = \"/orders/{orderId}\"" in text
    live = [line for line in text.splitlines() if not line.startswith("#")]
    assert not any("{orderId}" in line for line in live)


def test_an_example_is_used_when_the_spec_provides_one() -> None:
    assert 'path = "/products/42"' in render()


def test_tags_become_requirements_so_the_matrix_is_populated() -> None:
    assert 'requirement = "CATALOG"' in render()
    assert 'requirement = "OPS"' in render()


def test_the_base_url_can_be_overridden() -> None:
    assert 'base_url = "https://api.test.internal"' in render(
        base_url="https://api.test.internal/"
    )


def test_a_swagger_2_document_still_yields_a_base_url() -> None:
    swagger = {"swagger": "2.0", "host": "api.example.com", "basePath": "/v1", "paths": {}}
    assert base_url_of(swagger) == "https://api.example.com/v1"


def test_a_document_with_no_server_says_what_to_pass() -> None:
    with pytest.raises(OpenAPIError, match="--base-url"):
        to_suite({"paths": SPEC["paths"]}, name="x", base_url="", contract_path="c.json")


def test_yaml_is_refused_with_the_conversion_hint() -> None:
    with pytest.raises(OpenAPIError, match="looks like YAML"):
        load_spec("openapi: 3.0.0\npaths: {}\n", "spec.yaml")


def test_a_document_without_paths_is_not_an_openapi_document() -> None:
    with pytest.raises(OpenAPIError, match="not an OpenAPI document"):
        load_spec('{"info": {}}', "spec.json")


def test_duplicate_operation_ids_do_not_collide() -> None:
    spec = {
        "servers": [{"url": "https://x.example.com"}],
        "paths": {
            "/a": {"get": {"operationId": "same"}},
            "/b": {"get": {"operationId": "same"}},
        },
    }
    text = to_suite(spec, name="n", base_url="", contract_path="c.json")
    assert 'id = "SAME"' in text and 'id = "SAME-2"' in text


def test_the_cli_writes_a_file_and_refuses_to_clobber_one(tmp_path: Path) -> None:
    import json

    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(SPEC), encoding="utf-8")
    out = tmp_path / "suites" / "gen.toml"

    assert main(["import-openapi", "--spec", str(spec_file), "--out", str(out)]) == EXIT_OK
    assert out.is_file()
    assert main(["import-openapi", "--spec", str(spec_file), "--out", str(out)]) == EXIT_USAGE
    assert main(
        ["import-openapi", "--spec", str(spec_file), "--out", str(out), "--force"]
    ) == EXIT_OK
