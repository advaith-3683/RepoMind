import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from app.spa import SPAStaticFiles


@pytest.fixture
def client(tmp_path):
    (tmp_path / "index.html").write_text("<html>INDEX</html>")
    (tmp_path / "styles.css").write_text("body{}")
    return TestClient(Starlette(routes=[Mount("/", SPAStaticFiles(directory=tmp_path, html=True))]))


@pytest.mark.parametrize("path", ["/", "/workspace", "/workspace/", "/evaluations", "/documentation"])
def test_view_routes_serve_index(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "INDEX" in r.text


def test_assets_still_served(client):
    assert client.get("/styles.css").text == "body{}"


@pytest.mark.parametrize("path", ["/nope", "/missing.js", "/workspace/extra", "/api-typo"])
def test_everything_else_is_a_real_404(client, path):
    assert client.get(path).status_code == 404


def test_only_get_head_fall_back(client):
    assert client.post("/workspace").status_code in (404, 405)
