import pytest
from fastapi.testclient import TestClient

from opssentinel.app import create_app
from opssentinel.process_lock import ProcessLock

TOKEN = "controller-test-token-with-32-characters"
HEADERS = {"X-OpsSentinel-Request": "dashboard"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, schedule=False, api_token="")) as client:
        yield client


def test_empty_state_and_real_service_registration(client):
    assert client.get("/api/state").json()["services"] == []
    body = {"name": "Public health", "target": "http://localhost:9999/health", "connector": "http"}
    added = client.post("/api/services", json=body, headers=HEADERS)
    assert added.status_code == 201
    assert added.json()["health"] == "unknown"
    assert client.post("/api/services", json=body, headers=HEADERS).status_code == 409
    sid = added.json()["id"]
    assert client.patch(f"/api/services/{sid}", json={"enabled": False}, headers=HEADERS).json()["enabled"] is False


def test_controller_auth_and_mutation_origin(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, api_token=TOKEN, schedule=False)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/state").status_code == 401
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/api/state", headers=headers).status_code == 200
        body = {"name": "Test", "target": "http://localhost"}
        assert client.post("/api/services", json=body, headers=headers).status_code == 403
        headers.update(HEADERS)
        headers["Origin"] = "https://evil.example"
        assert client.post("/api/services", json=body, headers=headers).status_code == 403


def test_remote_without_token_is_denied_even_if_uvicorn_cli_bypassed(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, api_token="", schedule=False), client=("192.0.2.9", 54321)) as client:
        assert client.get("/api/state").status_code == 403


def test_http_monitor_cannot_enable_recovery_and_invalid_requests_dont_echo_secret(client):
    payload = {"name": "Bad", "target": "http://localhost", "connector": "http",
               "auto_actions": ["restart_service"], "agent_token": "do-not-expose-this"}
    response = client.post("/api/services", json=payload, headers=HEADERS)
    assert response.status_code == 422
    assert "do-not-expose-this" not in response.text
    assert client.post("/api/services", json={"name": "bad", "target": "file:///etc/passwd"}, headers=HEADERS).status_code == 422
    assert client.post("/api/services", json={"name": "bad", "target": "http://user:password@localhost"}, headers=HEADERS).status_code == 422


def test_agent_token_never_returned(client):
    body = {"name": "Managed", "connector": "agent", "target": "http://localhost:9876",
            "agent_service": "api", "agent_token": "a-secret-only-for-host"}
    response = client.post("/api/services", json=body, headers=HEADERS)
    assert response.status_code == 201
    assert "a-secret-only-for-host" not in response.text
    assert "a-secret-only-for-host" not in client.get("/api/state").text


def test_demo_is_opt_in(client):
    assert client.post("/api/demo/fault", json={"fault": "bad_release"}, headers=HEADERS).status_code == 404


def test_only_one_controller_owns_state(tmp_path):
    first = ProcessLock(tmp_path / "controller.lock")
    second = ProcessLock(tmp_path / "controller.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
