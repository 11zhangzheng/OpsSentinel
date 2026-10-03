import copy
import json

import httpx
import pytest

from opssentinel.analyzer import Analyzer
from opssentinel import models
from opssentinel.store import Store


def record(**changes):
    return {"evidence_id": "r:E01", "incident_id": "i", "service_id": "s", "run_id": "r",
            "tool_call_id": "c", "tool_name": "get_snapshot", "source": "agent:s",
            "observed_at": "2026-10-03T10:00:00+00:00", "collected_at": "2026-10-03T10:00:01+00:00",
            "status": "OK", "facts": [{"fact_id": "F01", "locator": "checks[0].ok", "kind": "business", "value": False}],
            "excerpt": None, "truncated": False, **changes}


def diagnosis(evidence="r:E01"):
    return {"root_cause_code": "process_exit", "root_cause": "The managed process stopped",
            "claims": [{"kind": "root_cause", "claim": "Process is stopped",
                        "citations": [{"evidence_id": evidence, "fact_id": "F01"}]}],
            "action": None, "hypotheses": [], "limitations": []}


@pytest.mark.parametrize("mutation,code", [
    ("missing", "UNKNOWN_EVIDENCE_ID"), ("incident", "CROSS_INCIDENT_REFERENCE"),
    ("run", "CROSS_RUN_REFERENCE"), ("failed", "FAILED_SOURCE_REFERENCE"),
    ("fact", "UNKNOWN_FACT_ID"), ("unseen", "UNSEEN_REFERENCE"),
    ("uncited", "UNGROUNDED_CLAIM"), ("action", "MISSING_ACTION_SUPPORT"),
])
def test_grounding_rejects_untrusted_references(mutation, code):
    # Removing source/ownership checks must permit these invalid claims and fail this test.
    validator = getattr(models, "validate_grounding", None)
    assert callable(validator), "Deterministic grounding validator is missing"
    item, candidate, visible = record(), diagnosis(), {"r:E01:F01"}
    if mutation == "missing":
        candidate = diagnosis("invented")
    elif mutation == "incident":
        item["incident_id"] = "another"
    elif mutation == "run":
        item["run_id"] = "old"
    elif mutation == "failed":
        item["status"] = "TOOL_TIMEOUT"
    elif mutation == "fact":
        candidate["claims"][0]["citations"][0]["fact_id"] = "fake"
    elif mutation == "unseen":
        visible = set()
    elif mutation == "uncited":
        candidate["claims"][0]["citations"] = []
    else:
        candidate["action"] = "restart_service"
    result = validator(candidate, [item], incident_id="i", run_id="r", service_id="s", visible_fact_ids=visible)
    assert result["status"] == "FAIL"
    assert code in result["errors"]


def test_structural_grounding_does_not_claim_semantic_truth():
    validator = getattr(models, "validate_grounding", None)
    assert callable(validator)
    result = validator(diagnosis(), [record()], incident_id="i", run_id="r", service_id="s",
                       visible_fact_ids={"r:E01:F01"})
    assert result == {"status": "PASS", "errors": []}


async def test_rules_produce_trusted_grounded_records_and_persist_immutable_prefix(monkeypatch, tmp_path):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    store = Store(tmp_path / "state.sqlite3")
    try:
        service = store.add_service({"name": "A", "connector": "agent", "enabled": True}, "s")
        snapshot = {"healthy": False, "summary": "Process stopped", "checks": [{"name": "container", "ok": False}],
                    "facts": {"suggested_action": "restart_service", "docker_state_known": True, "container_running": False}}
        incident = store.create_incident(service, snapshot)
        saver = getattr(store, "save_agent_run", None)
        assert callable(saver), "Run persistence is missing"
        result = await Analyzer().diagnose(service, snapshot, [], incident_id=incident["id"],
                                          persist_run_update=lambda run: saver(incident["id"], run))
        assert result["grounding"]["status"] == "PASS"
        assert result["action"] == "restart_service"
        run = store.get_incident(incident["id"])["agent_runs"][0]
        assert run["evidence_records"][0]["incident_id"] == incident["id"]
        altered = copy.deepcopy(run)
        altered["evidence_records"][0]["facts"][0]["value"] = "forged"
        with pytest.raises(ValueError, match="immutable"):
            saver(incident["id"], altered)
        assert store.get_incident(incident["id"])["agent_runs"][0] == run
    finally:
        store.close()


async def test_successful_local_read_cannot_launder_failed_collection(monkeypatch):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    result = await Analyzer().diagnose({"id": "s", "connector": "agent"},
        {"healthy": False, "summary": "Collector timed out", "source_status": {"snapshot": "TOOL_TIMEOUT"},
         "facts": {"suggested_action": "restart_service"}}, [], incident_id="i")
    assert "agent_run" in result
    assert result["agent_run"]["stop_reason"] == "NO_EVIDENCE"
    assert result["action"] is None
    assert not any(f["kind"] == "business" for r in result["agent_run"]["evidence_records"] for f in r["facts"])


@pytest.mark.parametrize("arguments", ['{"command":"delete"}', '[]', 'not json'])
async def test_no_argument_tools_validate_model_arguments(monkeypatch, arguments):
    analyzer = Analyzer()
    analyzer.api_key, analyzer.model = "test-key", "test-model"
    seen = []
    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c-{len(seen)}", "function": {"name": "get_snapshot", "arguments": arguments}}]}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    result = await analyzer.diagnose({"id": "s", "connector": "http"}, {"healthy": False, "checks": []}, [], incident_id="i")
    assert "agent_run" in result
    events = result["agent_run"]["tool_events"]
    assert events and all(e["status"] == "INVALID_ARGUMENT" for e in events)
    assert all(r["tool_call_id"] == "seed" or r["status"] != "OK" for r in result["agent_run"]["evidence_records"])
