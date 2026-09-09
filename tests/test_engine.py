import copy
from datetime import datetime, timedelta, timezone

import pytest

from opssentinel.analyzer import Analyzer
from opssentinel.engine import Conflict, Engine
from opssentinel.store import Store


class FakeConnectors:
    enable_demo = False

    def __init__(self, *, repaired=True, unknown=False):
        self.healthy = False
        self.suggested = "restart_service"
        self.executions = []
        self.repaired = repaired
        self.unknown = unknown

    async def start(self):
        pass

    async def close(self):
        pass

    async def observe(self, service):
        return {"healthy": self.healthy, "reachable": self.healthy, "summary": "Unavailable" if not self.healthy else "OK",
                "checks": [{"name": "business", "ok": self.healthy, "detail": "HTTP"}],
                "facts": {} if self.healthy else {"suggested_action": self.suggested},
                "logs": ["password=super-secret"]}

    async def execute(self, service, action):
        self.executions.append((service["_operation_id"], action))
        if self.unknown:
            raise TimeoutError("Response lost after remote command")
        self.healthy = self.repaired
        return {"ok": True, "summary": "Accepted", "details": {}}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("OPS_MODEL_NAME", raising=False)
    store = Store(tmp_path / "state.sqlite3")
    store.add_service({"name": "API", "connector": "agent", "target": "http://localhost:9876",
                       "agent_service": "api", "agent_token": "secret-host-token", "interval_seconds": 5,
                       "failure_threshold": 2, "recovery_threshold": 2, "auto_actions": ["restart_service"],
                       "enabled": True}, "svc")
    connector = FakeConnectors()
    engine = Engine(store, connector, Analyzer(), cooldown_seconds=0)
    yield engine, store, connector
    store.close()


async def test_auto_recovery_requires_fresh_business_observations(harness):
    engine, store, connector = harness
    await engine.scan_service("svc")
    assert store.active_incident("svc") is None
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    assert incident["status"] == "verifying"
    assert len(connector.executions) == 1
    assert store.db.execute("SELECT status FROM actions").fetchone()[0] == "completed"
    await engine.scan_service("svc")
    assert store.active_incident("svc")["status"] == "verifying"
    await engine.scan_service("svc")
    assert store.active_incident("svc") is None
    resolved = store.get_incident(incident["id"])
    assert resolved["resolution_kind"] == "mitigated"
    assert resolved["resolved_at"]


async def test_policy_approval_is_one_operation_and_duplicate_approval_rejected(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"auto_actions": []})
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    assert incident["status"] == "awaiting_approval"
    for _ in range(4):
        await engine.scan_service("svc")
    assert len(store.list_incidents()) == 1
    assert not connector.executions
    await engine.approve(incident["id"])
    with pytest.raises(Conflict):
        await engine.approve(incident["id"])
    assert len(connector.executions) == 1
    assert store.get_service("svc")["auto_actions"] == []


async def test_stale_approval_does_not_execute_prior_plan(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"auto_actions": []})
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    connector.suggested = "rollback_release"
    with pytest.raises(Conflict):
        await engine.approve(incident["id"])
    assert not connector.executions
    assert store.active_incident("svc")["status"] == "escalated"


async def test_unknown_remote_outcome_never_repeats_write(harness):
    engine, store, connector = harness
    connector.unknown = True
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    for _ in range(5):
        await engine.scan_service("svc")
    assert len(connector.executions) == 1
    assert store.active_incident("svc")["status"] == "escalated"


@pytest.mark.parametrize("outcome", ["refused", "response_lost", "unknown_success", "interrupted"])
async def test_unconfirmed_attempt_then_external_recovery_is_not_system_mitigation(harness, outcome):
    engine, store, connector = harness
    if outcome == "interrupted":
        service = store.record_observation("svc", await connector.observe(store.get_service("svc")))
        incident = store.create_incident(service, service["latest"])
        store.begin_action(incident, "restart_service")
        store.recover_interrupted()
    else:
        async def unconfirmed(service, action):
            connector.executions.append((service["_operation_id"], action))
            if outcome == "response_lost":
                raise TimeoutError("Remote result is unknown")
            return {"ok": outcome == "unknown_success", "summary": "Rejected or unconfirmed",
                    "details": {"outcome_unknown": outcome == "unknown_success"}}
        connector.execute = unconfirmed
        await engine.scan_service("svc")
        await engine.scan_service("svc")
        incident = store.active_incident("svc")
    assert store.get_incident(incident["id"])["attempts"] == 1
    connector.healthy = True  # An operator or another system restored availability.
    await engine.scan_service("svc")
    assert store.active_incident("svc") is not None  # Recovery still needs two fresh probes.
    await engine.scan_service("svc")
    resolved = store.get_incident(incident["id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolution_kind"] == "externally_recovered"
    assert not store.has_confirmed_latest_action(incident["id"])


async def test_earlier_success_does_not_mask_later_failed_action_when_service_recovers(harness):
    engine, store, connector = harness
    connector.repaired = False  # Command succeeds, but the service remains unhealthy.
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    assert store.has_confirmed_latest_action(incident["id"])
    connector.unknown = True
    await engine.scan_service("svc")  # A subsequent attempt has an unknown outcome.
    assert store.get_incident(incident["id"])["attempts"] == 2
    assert not store.has_confirmed_latest_action(incident["id"])
    connector.healthy = True
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    assert store.get_incident(incident["id"])["resolution_kind"] == "externally_recovered"


async def test_failed_verification_has_finite_retry_budget(harness):
    engine, store, connector = harness
    connector.repaired = False
    for _ in range(8):
        await engine.scan_service("svc")
    assert len(connector.executions) == 2
    assert connector.executions[0][0] != connector.executions[1][0]
    assert store.active_incident("svc")["status"] == "escalated"


async def test_paused_service_manual_scan_never_remediates(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"enabled": False})
    for _ in range(4):
        await engine.scan_service("svc", manual=True)
    assert store.get_service("svc")["latest"]
    assert not connector.executions
    assert not store.list_incidents()


async def test_stop_keeps_observation_and_blocks_future_actions(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"auto_actions": []})
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident = store.active_incident("svc")
    await engine.dismiss(incident["id"], "Taking over manually")
    store.patch_service("svc", {"auto_actions": ["restart_service"]})
    await engine.scan_service("svc")
    assert not connector.executions
    assert store.active_incident("svc")["stopped_by_user"]
    connector.healthy = True
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    assert store.get_incident(incident["id"])["resolution_kind"] == "externally_recovered"


async def test_http_evidence_cannot_grant_write_privileges(harness):
    engine, store, connector = harness
    store.patch_service("svc", {"connector": "http"})
    for _ in range(4):
        await engine.scan_service("svc")
    assert store.active_incident("svc")["action"] is None
    assert not connector.executions


async def test_restart_preserves_incident_and_does_not_replay_action(harness):
    engine, store, connector = harness
    service = store.get_service("svc")
    service = store.record_observation("svc", await connector.observe(service))
    incident = store.create_incident(service, service["latest"])
    store.update_incident(incident["id"], action="restart_service")
    operation_id = store.begin_action(incident, "restart_service")
    await engine.start(schedule=False)
    for _ in range(3):
        await engine.scan_service("svc")
    assert not connector.executions
    assert store.active_incident("svc")["status"] == "escalated"
    assert store.db.execute("SELECT status FROM actions WHERE id=?", (operation_id,)).fetchone()[0] == "uncertain"
    await engine.close()


async def test_secrets_are_removed_from_dashboard_and_evidence(harness):
    engine, store, connector = harness
    await engine.scan_service("svc")
    import json
    output = json.dumps(engine.state())
    assert "secret-host-token" not in output
    assert "super-secret" not in output
    assert "agent_token" not in output


async def test_parallel_scans_do_not_duplicate_incidents_or_actions(harness):
    import asyncio
    engine, store, connector = harness
    await asyncio.gather(*(engine.scan_service("svc") for _ in range(8)))
    assert len(store.list_incidents()) == 1
    assert len(connector.executions) == 1


async def test_malformed_telemetry_does_not_leave_stuck_investigation(harness):
    engine, store, connector = harness
    async def malformed(service):
        return {"healthy": False, "facts": "not-a-map", "metrics": {"cpu_percent": float("nan")}}
    connector.observe = malformed
    for _ in range(4):
        await engine.scan_service("svc")
    assert store.active_incident("svc")["status"] == "escalated"
    assert not connector.executions


async def test_critical_evidence_outlives_rolling_observation_history(harness):
    engine, store, connector = harness
    await engine.scan_service("svc")
    await engine.scan_service("svc")
    incident_id = store.active_incident("svc")["id"]
    for _ in range(205):
        await engine.scan_service("svc")
    incident = store.get_incident(incident_id)
    assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 200
    assert incident["evidence"]["initial"]["healthy"] is False
    assert incident["evidence"]["before_actions"][0]["healthy"] is False
    assert incident["evidence"]["recovery"]["healthy"] is True
