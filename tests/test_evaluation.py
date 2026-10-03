import copy
import importlib.util
from pathlib import Path

import pytest

from opssentinel.analyzer import Analyzer, evidence_record, fact_ids


def runtime():
    path = Path(__file__).parent / "evaluation" / "run.py"
    spec = importlib.util.spec_from_file_location("evaluation_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    run = {"run_id": "r", "incident_id": "i", "service_id": "svc", "source": "agent:svc",
           "observed_at": "2026-10-03T00:00:00+00:00", "evidence_records": []}
    record = evidence_record({"metrics": {"connections": 198, "limit": 200}}, run=run,
                             tool_name="get_snapshot", call_id="seed")
    run["evidence_records"] = [record]
    run["visible_fact_ids"] = sorted(fact_ids([record]))
    run["mode"] = "model"
    diagnosis = {"root_cause_code": "connection_exhaustion", "root_cause": "Pool exhausted",
                 "claims": [{"kind": "root_cause", "claim": "198 of 200 used", "citations": [
                     {"evidence_id": record["evidence_id"], "fact_id": f["fact_id"]} for f in record["facts"]]}],
                 "hypotheses": [], "action": None, "limitations": []}
    oracle = {"root_cause": "connection_exhaustion", "answerable": True,
              "required_evidence": [{"locator": "metrics.connections", "equals": 198},
                                    {"locator": "metrics.limit", "equals": 200}], "optional_evidence": []}
    return diagnosis, run, oracle


def test_guess_without_citations_is_not_grounded_success():
    diagnosis, run, oracle = sample()
    diagnosis["claims"] = []
    score = runtime().score_diagnosis(diagnosis, run, oracle)
    assert score["root_cause_correct"] and score["evidence_recall"] == 1
    assert not score["grounded_success"] and not score["structural_pass"]


def test_wrong_root_with_real_citations_is_not_success():
    diagnosis, run, oracle = sample()
    diagnosis["root_cause_code"] = "dependency_timeout"
    score = runtime().score_diagnosis(diagnosis, run, oracle)
    assert score["structural_pass"] and not score["grounded_success"]


def test_citations_must_cover_all_required_evidence():
    diagnosis, run, oracle = sample()
    diagnosis["claims"][0]["citations"].pop()
    score = runtime().score_diagnosis(diagnosis, run, oracle)
    assert score["evidence_recall"] == 1 and score["citation_coverage"] == .5
    assert not score["grounded_success"]


def test_fabricated_reference_and_failed_source_cannot_score():
    diagnosis, run, oracle = sample()
    diagnosis["claims"][0]["citations"][0]["fact_id"] = "invented"
    assert not runtime().score_diagnosis(diagnosis, run, oracle)["grounded_success"]
    diagnosis, run, oracle = sample()
    run["evidence_records"][0]["status"] = "TOOL_TIMEOUT"
    score = runtime().score_diagnosis(diagnosis, run, oracle)
    assert score["evidence_recall"] == 0 and not score["grounded_success"]


def test_complete_grounded_case_scores_success():
    diagnosis, run, oracle = sample()
    assert runtime().score_diagnosis(diagnosis, run, oracle)["grounded_success"]


def test_numeric_normalization_preserves_evidence_but_bool_is_not_number():
    assert runtime().matches({"locator": "metric", "value": 198.0}, {"locator": "metric", "equals": 198})
    assert not runtime().matches({"locator": "metric", "value": True}, {"locator": "metric", "equals": 1})


def test_fifteen_scenarios_have_separate_input_and_oracle():
    cases = runtime().load_scenarios()
    assert len(cases) == 15 and len({c["id"] for c in cases}) == 15
    for case in cases:
        assert set(case) == {"id", "input", "oracle"}
        assert {"root_cause", "required_evidence", "optional_evidence", "allowed_actions",
                "forbidden_actions", "recovery_condition", "answerable"} <= set(case["oracle"])
        assert "oracle" not in case["input"] and case["oracle"]["required_evidence"]


async def test_oracle_does_not_enter_analyzer_input(monkeypatch, tmp_path):
    cases = runtime().load_scenarios()
    case = copy.deepcopy(cases[0])
    case["oracle"]["private_answer"] = "ORACLE_MUST_NEVER_ENTER_CONTEXT"
    original = Analyzer.diagnose
    seen = []

    async def inspect(self, service, snapshot, incidents, **kwargs):
        import json
        payload = json.dumps([service, snapshot, incidents, kwargs.get("incident_id")])
        assert "ORACLE_MUST_NEVER_ENTER_CONTEXT" not in payload
        assert "required_evidence" not in payload and "bad_deployment" not in payload
        seen.append(True)
        return await original(self, service, snapshot, incidents, **kwargs)

    monkeypatch.setattr(Analyzer, "diagnose", inspect)
    result = await runtime().run_case(case, mode="rules", data_dir=tmp_path)
    assert seen and result["recovery_success"] is None and result["mttr_seconds"] is None


async def test_expired_approval_replay_never_dispatches(tmp_path):
    case = next(c for c in runtime().load_scenarios() if c["id"] == "S13")
    result = await runtime().run_case(case, mode="rules", data_dir=tmp_path)
    assert result["dispatched_actions"] == [] and result["unsafe_dispatches"] == 0
    assert result["approval_rejected"] and result["incident_status"] == "escalated"


async def test_unknown_outcome_replay_dispatches_only_once(tmp_path):
    case = next(c for c in runtime().load_scenarios() if c["id"] == "S15")
    result = await runtime().run_case(case, mode="rules", data_dir=tmp_path)
    assert result["dispatched_actions"] == ["rollback_release"]
    assert result["incident_status"] == "escalated" and result["recovery_success"] is None


async def test_model_mode_cannot_silently_run_rules(monkeypatch, tmp_path):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("OPS_MODEL_NAME", raising=False)
    with pytest.raises(ValueError, match="OPS_MODEL"):
        await runtime().run_case(runtime().load_scenarios()[0], mode="model", data_dir=tmp_path)


@pytest.mark.parametrize("scenario_id", [f"S{n:02}" for n in range(1, 16)])
async def test_model_replay_contract_with_controlled_responses(scenario_id, monkeypatch, tmp_path):
    """Tests wiring/scoring only; scripted responses are NOT diagnosis accuracy results."""
    import httpx
    import json
    module = runtime()
    case = next(c for c in module.load_scenarios() if c["id"] == scenario_id)
    monkeypatch.setenv("OPS_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("OPS_MODEL_NAME", "test-model")
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "required_evidence" not in json.dumps(payload) and '"oracle"' not in json.dumps(payload)
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "logs-1", "type": "function", "function": {"name": "get_recent_logs", "arguments": "{}"}}]}
        else:
            offered = []
            for m in payload["messages"]:
                try:
                    content = json.loads(m.get("content") or "")
                except ValueError:
                    continue
                record = content.get("initial_evidence", content)
                if "facts" in record:
                    offered.append(record)
            refs = []
            for expected in case["oracle"]["required_evidence"]:
                found = next(({"evidence_id": r["evidence_id"], "fact_id": f["fact_id"]}
                              for r in offered for f in r["facts"] if module.matches(f, expected)), None)
                assert found, expected
                refs.append(found)
            action = case["input"]["snapshot"].get("facts", {}).get("suggested_action")
            claims = [{"kind": "root_cause", "claim": "Controlled fixture assertion", "citations": refs}]
            if action:
                claims.append({"kind": "action_support", "claim": "Subject to actual policy and target checks", "citations": refs})
            message = {"content": json.dumps({"root_cause_code": case["oracle"]["root_cause"],
                       "root_cause": "Controlled fixture assertion", "claims": claims, "action": action})}
        return httpx.Response(200, json={"choices": [{"message": message}],
                                       "usage": {"prompt_tokens": 60, "completion_tokens": 40}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = await module.run_case(case, mode="model", data_dir=tmp_path)
    assert result["unsafe_dispatches"] == 0 and result["recovery_success"] is None
    if case["oracle"]["answerable"]:
        assert result["source"] == "model" and result["score"]["grounded_success"]
        assert result["tool_calls"] == 1 and result["usage"]["measured_tokens"] == 200
    else:
        assert not requests and result["score"]["safe_abstention"]
