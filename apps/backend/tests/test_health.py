from fastapi.testclient import TestClient

from prawdowid.api.app import app


def test_health_reports_api_liveness() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
