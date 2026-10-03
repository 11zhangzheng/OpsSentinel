import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from test_engine import harness
from test_host_agent import config_path
from opssentinel.engine import Conflict
from opssentinel.host_agent import HostRuntime, load_config
from opssentinel import connector_helpers


async def pending(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"auto_actions": []})
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    return engine, store, connector, store.active_incident("svc")


@pytest.mark.parametrize("change", ["expired", "wrong_plan", "policy", "target", "missing_target"])
async def test_old_approval_never_dispatches_a_changed_plan(harness, change):
    engine, store, connector, incident = await pending(harness)
    assert incident.get("proposal"), "Bound action proposal is missing"
    plan_id = incident["proposal"]["plan_id"]
    if change == "expired":
        proposal = {**incident["proposal"], "expires_at": "2000-01-01T00:00:00+00:00"}
        store.update_incident(incident["id"], proposal=proposal)
    elif change == "wrong_plan":
        plan_id = "old-page-plan"
    elif change == "policy":
        store.patch_service("svc", {"http_probe": {"timeout_seconds": 10}})
    elif change == "target":
        connector.container_id = "another-container"
    else:
        connector.container_id = None
    with pytest.raises(Conflict):
        await engine.approve(incident["id"], plan_id)
    assert not connector.executions


async def test_valid_plan_binds_intent_and_expected_target(harness):
    engine, store, connector, incident = await pending(harness)
    assert incident.get("proposal")
    await engine.approve(incident["id"], incident["proposal"]["plan_id"])
    assert len(connector.executions) == 1
    row = store.db.execute("SELECT document FROM actions WHERE incident_id=?", (incident["id"],)).fetchone()
    import json
    doc = json.loads(row[0])
    assert doc["plan_id"] == incident["proposal"]["plan_id"]
    assert doc["target_fingerprint"] == incident["proposal"]["target_fingerprint"]


async def test_verification_deadline_stops_further_mutations(harness):
    engine, store, connector = harness
    connector.repaired = False
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    assert incident.get("verification_deadline_at"), "Bound verification deadline is missing"
    store.update_incident(incident["id"], verification_deadline_at="2000-01-01T00:00:00+00:00")
    await engine.scan_service("svc")
    assert len(connector.executions) == 1
    assert store.active_incident("svc")["status"] == "escalated"


def test_target_fingerprint_rejects_missing_identity():
    fn = getattr(connector_helpers, "target_fingerprint", None)
    assert callable(fn), "Target fingerprint contract is missing"
    with pytest.raises(ValueError):
        fn({"facts": {"current_image": "image"}}, "restart_service")


def test_host_refuses_target_change_before_command(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service = {**runtime.services["web"], "_expected_context_key": "a"*64}
    monkeypatch.setattr(runtime, "_container", lambda _: {"known": True, "exists": True, "running": False,
        "id": "new-container", "current_image": "image", "created_at": "2026-10-03T00:00:00Z"})
    async def failed(*args):
        return {"ok": False}
    monkeypatch.setattr(runtime, "_probe", failed)
    commands = []
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kw: commands.append(args) or {"ok": True, "timed_out": False})
    outcome = runtime._perform(service, "restart_service", "target-check")
    assert not commands
    assert not outcome["ok"]


def test_host_operation_id_is_bound_to_plan(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    monkeypatch.setattr(runtime, "_perform", lambda *args: {"ok": True, "summary": "done", "details": {}})
    assert runtime.execute("web", "restart_service", "same-operation", expected_context_key="a"*64, plan_id="b"*64)["ok"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        runtime.execute("web", "restart_service", "same-operation", expected_context_key="a"*64, plan_id="c"*64)


async def test_approval_expiring_during_reobservation_never_dispatches(harness):
    import asyncio
    engine, store, connector, incident = await pending(harness)
    proposal = {**incident["proposal"], "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=0.02)).isoformat()}
    store.update_incident(incident["id"], proposal=proposal)
    original = connector.observe
    async def delayed(service):
        await asyncio.sleep(0.04)
        return await original(service)
    connector.observe = delayed
    with pytest.raises(Conflict):
        await engine.approve(incident["id"], proposal["plan_id"])
    assert not connector.executions


def test_authenticated_host_request_requires_bound_target_and_plan(config_path):
    from fastapi.testclient import TestClient
    from opssentinel.host_agent import create_app
    from test_host_agent import TOKEN
    with TestClient(create_app(config_path, token=TOKEN)) as client:
        response = client.post("/v1/services/web/actions", headers={"Authorization": "Bearer "+TOKEN},
                               json={"action": "restart_service", "operation_id": "unbound-operation"})
        assert response.status_code == 422
