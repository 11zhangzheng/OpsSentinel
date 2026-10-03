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


def test_rotation_descriptor_must_match_approved_inode(config_path, tmp_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    log = tmp_path / "managed.log"
    log.write_bytes(b"approved" * 300)
    service = {**runtime.services["web"], "managed_log_path": str(log), "max_log_bytes": 1024}
    monkeypatch.setattr(runtime, "_container", lambda _: {})
    service["_expected_context_key"] = connector_helpers.target_fingerprint(
        {"facts": runtime._target_facts(service, {})}, "rotate_logs")
    rotate = runtime._rotate
    replacement = b"unapproved replacement" * 200

    def replace_then_rotate(s):
        log.rename(log.with_suffix(".saved"))
        log.write_bytes(replacement)
        return rotate(s)

    monkeypatch.setattr(runtime, "_rotate", replace_then_rotate)
    outcome = runtime._perform(service, "rotate_logs", "rotation-race")
    assert not outcome["ok"]
    assert log.read_bytes() == replacement and not log.with_name("managed.log.1").exists()


@pytest.mark.parametrize("boundary", ["archive", "validation"])
def test_restore_reinspects_container_at_each_mutation(config_path, tmp_path, monkeypatch, boundary):
    from test_host_agent import prepare_config_restore, recent_container
    from opssentinel import host_agent
    runtime = HostRuntime(load_config(config_path))
    service, managed, _ = prepare_config_restore(runtime, tmp_path)
    container = {**recent_container(), "id": "approved-container"}
    monkeypatch.setattr(runtime, "_container", lambda _: dict(container))
    service["_expected_context_key"] = connector_helpers.target_fingerprint(
        {"facts": runtime._target_facts(service, container)}, "restore_config")

    async def unhealthy(*args):
        return {"ok": False}

    monkeypatch.setattr(runtime, "_probe", unhealthy)
    original_open = host_agent.os.open
    def archive_open(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if boundary == "archive" and str(path).endswith(".previous"):
            container["id"] = "replacement-container"
        return descriptor

    monkeypatch.setattr(host_agent.os, "open", archive_open)
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        if boundary == "validation" and args[-2:] == ["config", "--quiet"]:
            container["id"] = "replacement-container"
        return {"ok": True, "returncode": 0, "timed_out": False, "output": ""}

    monkeypatch.setattr(host_agent, "bounded_command", command)
    outcome = runtime._perform(service, "restore_config", "restore-race")
    assert not outcome["ok"]
    assert managed.read_bytes() == b"mode: broken\n"
    assert not any("--force-recreate" in argv for argv in commands)


@pytest.mark.parametrize("action", ["restart_service", "rollback_release", "restore_config", "rotate_logs"])
def test_bound_host_actions_still_execute_on_unchanged_target(config_path, tmp_path, monkeypatch, action):
    from test_host_agent import configure_rollback, prepare_config_restore, recent_container
    runtime = HostRuntime(load_config(config_path))
    service = runtime.services["web"]
    container = {**recent_container(), "id": "unchanged-target", "docker_health": "unhealthy"}
    configure_rollback(service)
    managed = None
    if action == "restore_config":
        service, managed, _ = prepare_config_restore(runtime, tmp_path)
    elif action == "rotate_logs":
        managed = tmp_path / "bound.log"
        managed.write_bytes(b"x"*2048)
        service.update(managed_log_path=str(managed), max_log_bytes=1024)
    monkeypatch.setattr(runtime, "_container", lambda _: dict(container))
    async def unhealthy(*args):
        return {"ok": False}
    monkeypatch.setattr(runtime, "_probe", unhealthy)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs:
                        {"ok": True, "returncode": 0, "timed_out": False, "output": ""})
    expected = connector_helpers.target_fingerprint({"facts": runtime._target_facts(service, container)}, action)
    outcome = runtime.execute("web", action, "bound-valid-"+action, expected, "b"*64)
    assert outcome["ok"] and outcome["details"]["verification_required"]
    if action == "restore_config":
        assert managed.read_bytes() == b"mode: working\n"
    elif action == "rotate_logs":
        assert managed.read_bytes() == b""
