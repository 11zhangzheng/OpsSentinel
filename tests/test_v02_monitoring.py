import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opssentinel.app import create_app
from opssentinel.engine import Conflict, Engine
from opssentinel.models import ResourceRule, ServiceCreate
from opssentinel.store import Store
from opssentinel.telemetry import iso


HEADERS = {"X-OpsSentinel-Request": "dashboard"}
RULE = {"metric": "memory_percent", "above": 90, "recover_below": 85, "for_checks": 3}


class Signals:
    enable_demo = False

    def __init__(self):
        self.healthy = True
        self.value = 40
        self.executions = []

    async def observe(self, service):
        return {"healthy": self.healthy, "reachable": True, "summary": "Business probe",
                "latency_ms": 10, "metrics": {"memory_percent": self.value},
                "facts": {"suggested_action": "restart_service", "allowed_actions": ["restart_service"],
                          "container_id": "monitored-container", "current_image": "example@sha256:"+"a"*64,
                          "container_created_at": "2026-10-03T00:00:00+00:00"}}

    async def execute(self, service, action):
        self.executions.append(action)
        self.healthy = True
        return {"ok": True, "summary": "Recovered", "details": {}}


def add(store, sid="svc", **overrides):
    config = ServiceCreate(name=sid, connector="agent", target="http://localhost:9876", agent_service=sid,
                           agent_token="private-token", interval_seconds=5,
                           auto_actions=["restart_service"], resource_rules=[RULE]).model_dump()
    store.add_service({**config, **overrides}, sid)


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    store = Store(tmp_path / "state.sqlite3")
    add(store)
    signals = Signals()
    engine = Engine(store, signals)
    yield engine, store, signals
    store.close()


async def scan_values(engine, signals, values):
    for value in values:
        signals.value = value
        await engine.scan_service("svc")


async def test_sustained_alert_hysteresis_missing_values_and_no_remediation(system):
    engine, store, signals = system
    await scan_values(engine, signals, [95, 95, None, 95, 95])
    assert not store.resource_alerts()
    await scan_values(engine, signals, [95, 99, 95])
    alert = store.resource_alerts()[0]
    assert alert["status"] == "firing" and alert["peak_value"] == 99
    assert len(store.resource_alerts()) == 1
    assert not signals.executions and not store.list_incidents()
    assert engine.state()["summary"]["healthy"] == 1
    assert engine.state()["summary"]["firing_alerts"] == 1
    await scan_values(engine, signals, [84, 84, None, 84, 85, 84, 84])
    assert store.resource_alerts()[0]["status"] == "firing"
    await scan_values(engine, signals, [84])
    assert store.resource_alerts()[0]["resolution_reason"] == "recovered"


async def test_alert_ack_is_idempotent_and_policy_change_is_not_recovery(system):
    engine, store, signals = system
    await scan_values(engine, signals, [99, 99, 99])
    alert = store.resource_alerts()[0]
    first = store.acknowledge_resource_alert(alert["id"], "Checking", "2026-09-09T00:00:00+00:00")
    second = store.acknowledge_resource_alert(alert["id"], "Again", "2026-09-10T00:00:00+00:00")
    assert first == second and first["status"] == "firing"
    assert sum(e["kind"] == "resource_acknowledged" for e in store.events()) == 1
    store.patch_service("svc", {"resource_rules": []})
    assert store.resource_alerts()[0]["resolution_reason"] == "rule_changed"
    assert store.firing_resource_count() == 0


async def test_resource_streak_survives_store_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    with_store = Store(path)
    add(with_store)
    service = with_store.get_service("svc")
    snapshot = {"metrics": {"memory_percent": 98}}
    for _ in range(2):
        with_store.evaluate_resources(service, snapshot, datetime.now(timezone.utc).isoformat())
    with_store.close()
    reopened = Store(path)
    try:
        reopened.evaluate_resources(reopened.get_service("svc"), snapshot, datetime.now(timezone.utc).isoformat())
        assert reopened.firing_resource_count() == 1
    finally:
        reopened.close()


async def test_maintenance_keeps_sampling_and_requires_fresh_post_window_failures(system):
    engine, store, signals = system
    signals.healthy = False
    await engine.maintenance("svc", minutes=30, reason="planned change")
    await scan_values(engine, signals, [99, 99, 99, 99])
    assert not store.list_incidents() and not store.resource_alerts() and not signals.executions
    history = store.history("svc", 1)
    assert history["summary"]["sample_count"] == 4
    assert sum(p["maintenance_samples"] for p in history["points"]) == 4
    await engine.maintenance("svc")
    await engine.scan_service("svc")
    assert not store.list_incidents()
    await engine.scan_service("svc")
    assert signals.executions == ["restart_service"]


async def test_maintenance_expiry_restarts_counts_and_blocks_approval(system):
    engine, store, signals = system
    signals.healthy = False
    store.patch_service("svc", {"auto_actions": []})
    await engine.maintenance("svc", minutes=1, reason="deploy")
    for _ in range(4):
        await engine.scan_service("svc")
    store.patch_service("svc", {"maintenance_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    await engine.scan_service("svc")
    assert not store.list_incidents()
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    assert incident["status"] == "awaiting_approval"
    await engine.maintenance("svc", minutes=1, reason="inspect")
    with pytest.raises(Conflict, match="维护"):
        await engine.approve(incident["id"], incident["proposal"]["plan_id"])
    assert not signals.executions


async def test_blocked_actions_do_not_starve_unrelated_probes(system):
    engine, store, signals = system
    add(store, "slow2", failure_threshold=1)
    add(store, "healthy3")
    store.patch_service("svc", {"failure_threshold": 1})
    release, both_started = asyncio.Event(), asyncio.Event()
    started = []

    async def observe(service):
        return {"healthy": service["id"] == "healthy3", "reachable": True,
                "facts": {"suggested_action": "restart_service",
                          "container_id": service["id"] + "-container",
                          "current_image": "example@sha256:" + "a" * 64,
                          "container_created_at": "2026-10-03T00:00:00+00:00"}}

    async def execute(service, action):
        started.append(service["id"])
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return {"ok": True, "details": {}}

    signals.observe, signals.execute = observe, execute
    tasks = [asyncio.create_task(engine.scan_service(sid)) for sid in ("svc", "slow2")]
    try:
        await asyncio.wait_for(both_started.wait(), 2)
        await asyncio.wait_for(engine.scan_service("healthy3"), 1)
        assert store.get_service("healthy3")["health"] == "healthy"
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_stale_success_is_not_current_health(system, monkeypatch):
    engine, store, signals = system
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr("opssentinel.store.now", lambda: old)
    store.record_observation("svc", {"healthy": True, "metrics": {}})
    state = engine.state()
    assert state["services"][0]["health"] == "healthy"
    assert state["services"][0]["freshness"] == "stale"
    assert state["summary"]["healthy"] == 0 and state["summary"]["stale_services"] == 1


def test_persisted_history_retention_aggregation_and_missing_data(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    add(store)
    end = datetime.now(timezone.utc).timestamp()
    for i in range(205):
        monkeypatch.setattr("opssentinel.store.now", lambda index=i: iso(end - 3500 + index))
        store.record_observation("svc", {"healthy": i % 2 == 0, "latency_ms": i, "metrics": {}})
    assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 200
    store.close()
    store = Store(path)
    try:
        history = store.history("svc", 1, at=end)
        assert history["summary"]["sample_count"] == 205
        assert history["summary"]["healthy_count"] == 103
        assert history["summary"]["p95_latency_ms"] == 194
        assert all(p["cpu_percent"] is None for p in history["points"])
        assert len(history["points"]) < 20  # Unobserved intervals are not filled with zeros.
        monkeypatch.setattr("opssentinel.store.now", lambda: iso(end + 8 * 86400))
        store.record_observation("svc", {"healthy": True, "metrics": {}})
        assert store.history("svc", 168, at=end + 8 * 86400)["summary"]["sample_count"] == 1
    finally:
        store.close()


def test_legacy_history_is_backfilled_once_and_keeps_service_policy(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    old = Store(path)
    add(old, enabled=False)
    old.record_observation("svc", {"healthy": True, "latency_ms": 40, "metrics": {}})
    old.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE telemetry")
        db.execute("DROP TABLE metadata")
    for _ in range(2):
        upgraded = Store(path)
        try:
            assert upgraded.history("svc")["summary"]["sample_count"] == 1
            assert upgraded.get_service("svc")["enabled"] is False
        finally:
            upgraded.close()


def test_new_api_validation_maintenance_and_empty_history(tmp_path):
    with TestClient(create_app(data_dir=tmp_path, schedule=False, api_token="")) as client:
        response = client.post("/api/services", headers=HEADERS, json={"name": "API", "target": "http://localhost:9999", "http_probe": {"expected_status": 204}})
        sid = response.json()["id"]
        assert response.status_code == 201
        assert client.get(f"/api/services/{sid}/history?hours=1").json()["summary"]["success_rate"] is None
        assert client.get(f"/api/services/{sid}/history?hours=2").status_code == 422
        assert client.get("/api/services/missing/history").status_code == 404
        bad = client.patch(f"/api/services/{sid}", headers=HEADERS, json={"resource_rules": [RULE]})
        assert bad.status_code == 422
        maintained = client.post(f"/api/services/{sid}/maintenance", headers=HEADERS, json={"minutes": 30, "reason": "upgrade"})
        assert maintained.json()["maintenance_active"] is True
        assert client.delete(f"/api/services/{sid}/maintenance", headers=HEADERS).json()["maintenance_active"] is False
        assert client.post(f"/api/services/{sid}/maintenance", headers=HEADERS, json={"minutes": 0, "reason": "upgrade"}).status_code == 422
        assert client.post("/api/resource-alerts/unknown/acknowledge", headers=HEADERS, json={"note": "seen"}).status_code == 404


@pytest.mark.parametrize("changes", [{"above": 101}, {"above": 80, "recover_below": 90}, {"for_checks": 0}, {"above": float("nan")}])
def test_invalid_resource_rules_rejected(changes):
    with pytest.raises(ValueError):
        ResourceRule.model_validate({**RULE, **changes})
