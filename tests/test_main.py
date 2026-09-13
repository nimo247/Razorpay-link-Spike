from fastapi.testclient import TestClient

from app.main import create_app


def test_health_route() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}