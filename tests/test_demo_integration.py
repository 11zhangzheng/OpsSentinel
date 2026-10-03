import pytest
from fastapi.testclient import TestClient

from opssentinel.app import create_app

HEADERS = {"X-OpsSentinel-Request": "dashboard"}


@pytest.mark.parametrize("fault,action", [("bad_release", "rollback_release"), ("bad_config", "restore_config"),
                                         ("process_exit", "restart_service"), ("log_pressure", "rotate_logs")])
def test_real_http_exercise_recovers_with_evidence(tmp_path, monkeypatch, fault, action):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    with TestClient(create_app(data_dir=tmp_path, demo=True, api_token="", schedule=False)) as client:
        assert client.post("/api/services/demo-service/scan", headers=HEADERS).status_code == 200
        assert client.get("/api/state").json()["services"][0]["latest"]["healthy"]
        assert client.post("/api/demo/fault", json={"fault": fault}, headers=HEADERS).json()["ok"]
        for _ in range(2):
            assert client.post("/api/services/demo-service/scan", headers=HEADERS).status_code == 200
        state = client.get("/api/state").json()
        incident = state["incidents"][0]
        assert incident["status"] == "verifying"
        assert incident["action"] == action
        assert incident["attempts"] == 1
        assert incident["evidence"]["initial"]["healthy"] is False
        assert len(incident["evidence"]["before_actions"]) == 1
        for _ in range(2):
            client.post("/api/services/demo-service/scan", headers=HEADERS)
        state = client.get("/api/state").json()
        assert state["incidents"][0]["status"] == "resolved"
        assert state["incidents"][0]["evidence"]["recovery"]["healthy"] is True
        assert state["summary"]["open_incidents"] == 0
        assert state["services"][0]["target"] == client.app.state.engine.connectors.demo_service_config()["target"]


def test_demo_incident_survives_controller_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    with TestClient(create_app(data_dir=tmp_path, demo=True, api_token="", schedule=False)) as client:
        client.patch("/api/services/demo-service", json={"auto_actions": []}, headers=HEADERS)
        client.post("/api/demo/fault", json={"fault": "bad_config"}, headers=HEADERS)
        for _ in range(2):
            client.post("/api/services/demo-service/scan", headers=HEADERS)
        incident = client.get("/api/state").json()["incidents"][0]
        assert incident["status"] == "awaiting_approval"
    with TestClient(create_app(data_dir=tmp_path, demo=True, api_token="", schedule=False)) as client:
        state = client.get("/api/state").json()
        assert state["incidents"][0]["id"] == incident["id"]
        assert state["incidents"][0]["status"] == "awaiting_approval"
        assert state["services"][0]["auto_actions"] == []
        assert client.post(f"/api/incidents/{incident['id']}/approve", headers=HEADERS, json={"plan_id": incident["proposal"]["plan_id"]}).status_code == 200
        for _ in range(2):
            client.post("/api/services/demo-service/scan", headers=HEADERS)
        assert client.get("/api/state").json()["incidents"][0]["status"] == "resolved"
