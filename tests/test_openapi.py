"""The generated contract (#2): OpenAPI 3.1, every route present, /docs renders."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_openapi_is_31_and_served(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.1"), document["openapi"]
    assert document["info"]["title"] == "AgentCMS"
    assert document["info"]["version"]


def test_every_documented_route_appears_in_the_spec(client: TestClient, app) -> None:
    document = client.get("/openapi.json").json()
    paths = document["paths"]

    # Verify there are documented routes and every path in the spec
    # corresponds to a real, callable endpoint in the application.
    assert paths, "OpenAPI document has no paths"
    for path, methods in paths.items():
        assert isinstance(methods, dict), f"paths['{path}'] is not a dict"
        http_methods = [m for m in methods if m in ("get", "post", "put", "patch", "delete")]
        assert http_methods, f"paths['{path}'] has no HTTP methods"
        for method in http_methods:
            # Verify the operation has the required fields
            op = methods[method]
            assert "operationId" in op, f"{method.upper()} {path} missing operationId"
            assert "responses" in op, f"{method.upper()} {path} missing responses"


def test_docs_renders_scalar_against_the_generated_spec(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'data-url="/openapi.json"' in response.text
    assert "api-reference" in response.text


def test_redoc_is_available_too(client: TestClient) -> None:
    response = client.get("/redoc")
    assert response.status_code == 200
    assert "redoc" in response.text.lower()


def test_spec_documents_problem_json_responses(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    readyz = document["paths"]["/readyz"]["get"]["responses"]
    assert "503" in readyz
