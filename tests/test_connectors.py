import json
from pathlib import Path

import pytest

from opssentinel.connectors import ConnectorManager
from opssentinel.connector_helpers import validate_http_url


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://localhost", "http://user:secret@localhost/", "http://localhost/#fragment", "http://169.254.169.254/", "http://localhost:99999", "http://localhost/\nheader"])
def test_rejects_unsafe_http_targets(target):
    with pytest.raises(ValueError):
        validate_http_url(target)


@pytest.mark.parametrize("fault,action", [("bad_release", "rollback_release"), ("bad_config", "restore_config"),
                                           ("process_exit", "restart_service"), ("log_pressure", "rotate_logs")])
async def test_real_subprocess_fault_recovery(tmp_path, fault, action):
    manager = ConnectorManager(tmp_path)
    try:
        await manager.start()
        config = manager.demo_service_config()
        assert (await manager.observe(config))["healthy"]
        assert (await manager.inject_fault(fault))["ok"]
        broken = await manager.observe(config)
        assert not broken["healthy"]
        assert broken["facts"]["suggested_action"] == action
        assert (await manager.execute(config, action))["ok"]
        recovered = await manager.observe(config)
        assert recovered["healthy"]
        assert any(check["name"] == "business_http" for check in recovered["checks"])
    finally:
        await manager.close()


async def test_second_manager_and_owned_process_cleanup(tmp_path):
    one = ConnectorManager(tmp_path / "one")
    two = ConnectorManager(tmp_path / "two")
    try:
        await one.start()
        await two.start()
        assert one.demo_service_config()["target"] != two.demo_service_config()["target"]
        await one.close()
        assert (await two.observe(two.demo_service_config()))["healthy"]
    finally:
        await one.close()
        await two.close()


async def test_demo_state_and_operation_id_survive_restart(tmp_path):
    first = ConnectorManager(tmp_path)
    try:
        await first.start()
        await first.inject_fault("bad_config")
    finally:
        await first.close()
    second = ConnectorManager(tmp_path)
    try:
        await second.start()
        config = second.demo_service_config()
        assert not (await second.observe(config))["healthy"]
        config["_operation_id"] = "test-operation-001"
        assert (await second.execute(config, "restore_config"))["ok"]
        await second.inject_fault("bad_config")
        replay = await second.execute(config, "restore_config")
        assert replay["details"]["replayed"]
        assert not (await second.observe(config))["healthy"]  # did not run twice
    finally:
        await second.close()


async def test_http_connector_really_probes_and_refuses_writes(tmp_path):
    manager = ConnectorManager(tmp_path)
    try:
        await manager.start()
        config = {**manager.demo_service_config(), "connector": "http"}
        assert (await manager.observe(config))["healthy"]
        assert not (await manager.execute(config, "restart_service"))["ok"]
    finally:
        await manager.close()


async def test_agent_connector_reuses_operation_id_and_never_returns_token(tmp_path, monkeypatch):
    calls = []

    async def fake_request(url, **kwargs):
        calls.append(kwargs)
        if kwargs.get("method") == "POST":
            return 200, json.dumps({"ok": True, "summary": "completed", "details": {}}).encode()
        return 401, b"do not expose the server body"

    monkeypatch.setattr("opssentinel.connectors.http_request", fake_request)
    manager = ConnectorManager(tmp_path, enable_demo=False)
    service = {"connector": "agent", "target": "http://127.0.0.1:9876", "agent_service": "web",
               "agent_token": "top-secret-token", "_operation_id": "stable-op-001",
               "_expected_context_key": "a"*64, "_plan_id": "b"*64}
    await manager.execute(service, "restart_service")
    await manager.execute(service, "restart_service")
    assert [call["payload"]["operation_id"] for call in calls] == ["stable-op-001", "stable-op-001"]
    observed = await manager.observe(service)
    assert not observed["healthy"]
    assert "top-secret" not in json.dumps(observed)


async def test_demo_disabled_has_no_child(tmp_path):
    manager = ConnectorManager(tmp_path, enable_demo=False)
    await manager.start()
    assert manager._child is None
    assert not (await manager.inject_fault("process_exit"))["ok"]
    await manager.close()
