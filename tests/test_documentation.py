"""Agent documentation routes, and their wiring into the gateway app."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kasa_tapo_services.documentation import router


@pytest.fixture
def docs_client() -> TestClient:
    """A bare app with only the documentation router — no registry, no hardware."""

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_agent_docs_routes(docs_client: TestClient) -> None:
    r = docs_client.get("/agent-docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "allowed_actions" in r.text and "privacy" in r.text
    r = docs_client.get("/agent-docs/api-reference")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "/cameras/{camera_id}/control/ptz" in r.text
    assert "/plugs/{plug_id}/control/toggle" in r.text
    r = docs_client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in r.text and "(openapi.json)" in r.text


@pytest.mark.parametrize("prefix", ["", "/gateway", "/api/equipment/test/documentation"])
def test_index_links_without_hardware(prefix: str) -> None:
    service = FastAPI()
    service.include_router(router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as client:
        response = client.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == {"agent-docs", "agent-docs/api-reference", "openapi.json"}
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
            assert client.get(resolved).status_code == 200


def test_openapi_lists_documentation_routes(docs_client: TestClient) -> None:
    paths = docs_client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt"} <= set(paths)


def test_gateway_app_mounts_the_documentation_router() -> None:
    # The docs are only discoverable if main.py actually includes the router;
    # importing the app is enough (the registry is built in the lifespan).
    from kasa_tapo_services.main import app

    paths = {route.path for route in app.routes}
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt"} <= paths
