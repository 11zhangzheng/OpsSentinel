import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
import yaml

from opssentinel.host_agent import HostRuntime, create_app, load_config

TOKEN = "test-only-agent-token-that-is-long-enough"
PREVIOUS_IMAGE = "registry.example/web@sha256:" + "a" * 64
EXPECTED_IMAGE = "registry.example/web@sha256:" + "b" * 64


def configure_rollback(service):
    service.update(previous_image=PREVIOUS_IMAGE, expected_current_image=EXPECTED_IMAGE,
                   rollback_data_compatible=True, business_url="http://127.0.0.1:1/business")


def recent_container():
    return {"known": True, "exists": True, "running": True, "current_image": EXPECTED_IMAGE,
            "created_at": datetime.now(timezone.utc).isoformat(), "summary": "Container running"}


@pytest.fixture
def config_path(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {web: {image: example:v2}}", encoding="utf-8")
    config = {"state_dir": str(tmp_path / "state"), "services": {"web": {
        "compose_file": str(compose), "compose_service": "web", "health_url": "http://127.0.0.1:1/health",
        "allowed_actions": ["restart_service", "rollback_release", "rotate_logs", "restore_config"]}}}
    target = tmp_path / "agent.yaml"
    target.write_text(yaml.safe_dump(config), encoding="utf-8")
    return target


def test_auth_and_action_schema(config_path, monkeypatch):
    app = create_app(config_path, token=TOKEN)
    monkeypatch.setattr(app.state.runtime, "_perform", lambda *args: {"ok": True, "summary": "simulated", "details": {}})
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert TOKEN not in client.get("/health").text
        assert client.get("/v1/services/web/observe").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.post("/v1/services/web/actions", headers=headers, json={"action": "run_shell", "operation_id": "op-000001"}).status_code == 422
        assert client.post("/v1/services/web/actions", headers=headers, json={"action": "restart_service", "operation_id": "op-000001", "command": "rm -rf /"}).status_code == 422
        assert client.post("/v1/services/unknown/actions", headers=headers, json={"action": "restart_service", "operation_id": "op-000001"}).status_code == 404


def test_persistent_idempotency_and_collision(config_path, monkeypatch):
    config = load_config(config_path)
    first = HostRuntime(config)
    calls = []
    monkeypatch.setattr(first, "_perform", lambda *args: calls.append(args) or {"ok": True, "summary": "once", "details": {}})
    assert first.execute("web", "restart_service", "persisted-operation")["ok"]
    second = HostRuntime(config)
    monkeypatch.setattr(second, "_perform", lambda *args: pytest.fail("must not execute again"))
    assert second.execute("web", "restart_service", "persisted-operation")["details"]["replayed"]
    assert len(calls) == 1
    with pytest.raises(HTTPException) as caught:
        second.execute("web", "rotate_logs", "persisted-operation")
    assert caught.value.status_code == 409


def test_interrupted_operation_refuses_blind_replay(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    with sqlite3.connect(runtime.database) as conn:
        conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", ("interrupted-op", "web", "restart_service", "started", None, 1))
    monkeypatch.setattr(runtime, "_perform", lambda *args: pytest.fail("must not replay interrupted action"))
    outcome = runtime.execute("web", "restart_service", "interrupted-op")
    assert not outcome["ok"]
    assert outcome["details"]["outcome_unknown"]


def test_rollback_requires_immutable_compatible_local_image(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service = runtime.services["web"]
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    calls = []
    async def unhealthy(*args):
        return {"ok": False}
    monkeypatch.setattr(runtime, "_probe", unhealthy)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda args, **kwargs: calls.append(args) or {"ok": True, "output": "sha256:image", "returncode": 0, "timed_out": False})
    assert not runtime._perform(service, "rollback_release", "rollback-1")["ok"]
    service.update(previous_image="example:old", rollback_data_compatible=True)
    assert not runtime._perform(service, "rollback_release", "rollback-2")["ok"]
    assert not calls
    configure_rollback(service)
    outcome = runtime._perform(service, "rollback_release", "rollback-3")
    assert outcome["ok"] and outcome["details"]["verification_required"]
    assert "--no-deps" in calls[-1] and "--pull" in calls[-1] and calls[-1][-1] == "web"
    assert yaml.safe_load(next(runtime.state_dir.glob("rollback-*.yaml")).read_text())["services"]["web"]["image"] == service["previous_image"]


def test_rollback_refuses_recovered_service(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service = runtime.services["web"]
    configure_rollback(service)
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    async def healthy(*args):
        return {"ok": True}
    monkeypatch.setattr(runtime, "_probe", healthy)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: pytest.fail("must not roll back a recovered service"))
    assert not runtime._perform(service, "rollback_release", "healthy-rollback")["ok"]


def test_restart_refuses_absent_or_healthy_service(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service = runtime.services["web"]
    monkeypatch.setattr(runtime, "_container", lambda _: {"known": True, "exists": False})
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: pytest.fail("must not invoke restart"))
    assert not runtime._perform(service, "restart_service", "restart-1")["ok"]
    monkeypatch.setattr(runtime, "_container", lambda _: {"known": True, "exists": True, "running": True})

    async def healthy(*args):
        return {"ok": True}

    monkeypatch.setattr(runtime, "_probe", healthy)
    assert not runtime._perform(service, "restart_service", "restart-2")["ok"]


def test_dedicated_log_rotation_is_bounded_and_preserves_inode(config_path, tmp_path):
    runtime = HostRuntime(load_config(config_path))
    log = tmp_path / "application.log"
    log.write_bytes(b"x" * 2048)
    inode = log.stat().st_ino
    service = {**runtime.services["web"], "managed_log_path": str(log), "max_log_bytes": 1024}
    outcome = runtime._rotate(service)
    assert outcome["ok"]
    assert log.stat().st_size == 0 and log.stat().st_ino == inode
    assert log.with_name("application.log.1").read_bytes() == b"x" * 2048
    assert not runtime._rotate(service)["ok"]


def test_config_restore_refuses_missing_known_good_contract(config_path):
    runtime = HostRuntime(load_config(config_path))
    outcome = runtime.execute("web", "restore_config", "restore-operation")
    assert not outcome["ok"] and "known-good config contract" in outcome["summary"]


def test_allowlist_blocks_action_before_execution(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    runtime.services["web"]["allowed_actions"] = []
    monkeypatch.setattr(runtime, "_perform", lambda *args: pytest.fail("must respect allowlist"))
    assert not runtime.execute("web", "restart_service", "disallowed-operation")["ok"]


def test_short_token_is_rejected(config_path):
    with pytest.raises(ValueError, match="24"):
        create_app(config_path, token="short")


@pytest.mark.parametrize("change", ["wrong_image", "old_release", "future_time", "missing_created", "no_business_probe"])
def test_rollback_refuses_non_release_incidents(config_path, monkeypatch, change):
    runtime = HostRuntime(load_config(config_path))
    service = runtime.services["web"]
    configure_rollback(service)
    container = recent_container()
    if change == "wrong_image":
        container["current_image"] = "unrelated:v3"
    elif change == "old_release":
        container["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    elif change == "future_time":
        container["created_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif change == "missing_created":
        container.pop("created_at")
    else:
        service.pop("business_url")
    monkeypatch.setattr(runtime, "_container", lambda _: container)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: pytest.fail("must not invoke Docker mutation"))
    assert not runtime._perform(service, "rollback_release", "unrelated-incident")["ok"]


def prepare_config_restore(runtime, tmp_path):
    managed = tmp_path / "managed.yaml"
    backup = tmp_path / "known-good.yaml"
    managed.write_bytes(b"mode: broken\n")
    backup.write_bytes(b"mode: working\n")
    service = runtime.services["web"]
    service.update(managed_config_path=str(managed), known_good_config_path=str(backup),
                   known_good_config_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
    return service, managed, backup


def test_restore_config_real_files_and_fixed_compose_commands(config_path, tmp_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service, managed, backup = prepare_config_restore(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    async def unhealthy(*args):
        return {"ok": False}
    monkeypatch.setattr(runtime, "_probe", unhealthy)
    calls = []
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda args, **kwargs: calls.append(args) or {"ok": True, "output": "", "returncode": 0, "timed_out": False})
    outcome = runtime.execute("web", "restore_config", "restore-fixed-config")
    assert outcome["ok"] and outcome["details"]["verification_required"]
    assert managed.read_bytes() == backup.read_bytes()
    assert (runtime.state_dir / outcome["details"]["archive"]).read_bytes() == b"mode: broken\n"
    assert calls[0][-2:] == ["config", "--quiet"]
    assert "--force-recreate" in calls[1]
    assert calls[1][-1] == "web" and "--no-deps" in calls[1] and "--no-build" in calls[1]
    assert runtime.execute("web", "restore_config", "restore-fixed-config")["details"]["replayed"]
    assert len(calls) == 2


@pytest.mark.parametrize("failure_stage", ["validation", "deployment"])
def test_restore_failure_puts_original_config_back(config_path, tmp_path, monkeypatch, failure_stage):
    runtime = HostRuntime(load_config(config_path))
    service, managed, _ = prepare_config_restore(runtime, tmp_path)
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    async def unhealthy(*args):
        return {"ok": False}
    monkeypatch.setattr(runtime, "_probe", unhealthy)
    def command(args, **kwargs):
        fail = ("config" in args) == (failure_stage == "validation")
        return {"ok": not fail, "output": "", "returncode": 1 if fail else 0, "timed_out": fail}
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", command)
    outcome = runtime.execute("web", "restore_config", "restore-failure-config")
    assert not outcome["ok"]
    assert managed.read_bytes() == b"mode: broken\n"
    assert outcome["details"]["original_file_restored"]
    assert outcome["details"]["outcome_unknown"] == (failure_stage == "deployment")


@pytest.mark.parametrize("invalid", ["wrong_hash", "same_content", "oversized", "healthy"])
def test_restore_preconditions_refuse_without_modification(config_path, tmp_path, monkeypatch, invalid):
    runtime = HostRuntime(load_config(config_path))
    service, managed, backup = prepare_config_restore(runtime, tmp_path)
    if invalid == "wrong_hash":
        service["known_good_config_sha256"] = "0" * 64
    elif invalid == "same_content":
        managed.write_bytes(backup.read_bytes())
    elif invalid == "oversized":
        managed.write_bytes(b"x" * 1048577)
    before = managed.read_bytes()
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    async def probe(*args):
        return {"ok": invalid == "healthy"}
    monkeypatch.setattr(runtime, "_probe", probe)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: pytest.fail("must not mutate"))
    assert not runtime.execute("web", "restore_config", "restore-precondition-config")["ok"]
    assert managed.read_bytes() == before


def test_restore_refuses_symlink_backup(config_path, tmp_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service, managed, backup = prepare_config_restore(runtime, tmp_path)
    link = tmp_path / "backup-link.yaml"
    try:
        link.symlink_to(backup)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows test host")
    service["known_good_config_path"] = str(link)
    monkeypatch.setattr(runtime, "_container", lambda *args: pytest.fail("must reject before Docker inspection"))
    assert not runtime.execute("web", "restore_config", "symlink-restore-config")["ok"]
    assert managed.read_bytes() == b"mode: broken\n"


def test_unknown_mutation_outcome_keeps_service_lock(config_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    monkeypatch.setattr(runtime, "_perform", lambda *args: {"ok": False, "summary": "timed out", "details": {"outcome_unknown": True}})
    assert not runtime.execute("web", "restart_service", "unknown-mutation")["ok"]
    other = HostRuntime(load_config(config_path))
    monkeypatch.setattr(other, "_perform", lambda *args: pytest.fail("unknown prior effect must block service"))
    blocked = other.execute("web", "rotate_logs", "after-unknown-mutation")
    assert blocked["details"]["service_busy"]


def test_cross_instance_service_lock_and_interruption(config_path, monkeypatch):
    config = load_config(config_path)
    first, second = HostRuntime(config), HostRuntime(config)
    started, finish = threading.Event(), threading.Event()
    outcomes = []
    def slow_action(*args):
        started.set()
        assert finish.wait(5)
        return {"ok": True, "summary": "done", "details": {}}
    monkeypatch.setattr(first, "_perform", slow_action)
    monkeypatch.setattr(second, "_perform", lambda *args: pytest.fail("other runtime must not write concurrently"))
    thread = threading.Thread(target=lambda: outcomes.append(first.execute("web", "restart_service", "cross-instance-first")))
    thread.start()
    try:
        assert started.wait(3)
        busy = second.execute("web", "rotate_logs", "cross-instance-second")
        assert not busy["ok"] and busy["details"]["service_busy"]
    finally:
        finish.set()
        thread.join(5)
    assert outcomes[0]["ok"]
    with sqlite3.connect(first.database) as conn:
        conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", ("interrupted-lock", "web", "restart_service", "started", None, 1))
    busy = second.execute("web", "rotate_logs", "after-interrupted-lock")
    assert not busy["ok"] and busy["details"]["service_busy"]


async def test_observation_prioritizes_config_evidence_and_emits_hashes(config_path, tmp_path, monkeypatch):
    runtime = HostRuntime(load_config(config_path))
    service, managed, backup = prepare_config_restore(runtime, tmp_path)
    configure_rollback(service)
    monkeypatch.setattr(runtime, "_container", lambda _: recent_container())
    async def failed(name, url):
        return {"name": name, "ok": False, "detail": "HTTP 503", "reachable": True}
    monkeypatch.setattr(runtime, "_probe", failed)
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: {"ok": True, "output": "", "returncode": 0, "timed_out": False})
    snapshot = await runtime.observe("web")
    assert snapshot["facts"]["suggested_action"] == "restore_config"
    assert snapshot["facts"]["config_hash"] == hashlib.sha256(managed.read_bytes()).hexdigest()
    assert snapshot["facts"]["known_good_config_hash"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    service["allowed_actions"].remove("restore_config")
    assert (await runtime.observe("web"))["facts"]["suggested_action"] == "rollback_release"
