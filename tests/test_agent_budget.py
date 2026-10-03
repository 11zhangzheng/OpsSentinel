import asyncio
import copy
import json

import httpx
import pytest

from opssentinel import analyzer as module
from opssentinel.analyzer import Analyzer


def enable(monkeypatch, handler):
    agent = Analyzer()
    agent.api_key, agent.model = "test-key", "test-model"
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return agent


async def test_huge_logs_preserve_numeric_evidence_without_oversized_requests(monkeypatch):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        text = json.dumps(payload, ensure_ascii=False)
        assert len(text.encode()) <= 6680
        assert '198' in text and '200' in text
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "root_cause_code": "unknown", "root_cause": "Evidence requires more investigation", "claims": [], "action": None})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    agent = enable(monkeypatch, handler)
    result = await agent.diagnose({"id": "s", "connector": "http"},
        {"healthy": False, "logs": ["normal "*100000, "timeout active=198 limit=200"],
         "facts": {"connections": {"active": 198, "limit": 200}}}, [], incident_id="i")
    assert result["source"] == "model"
    assert requests
    assert result["agent_run"]["usage"]["measured_tokens"] == 120
    assert result["agent_run"]["evidence_records"][0]["truncated"]


async def test_context_exhaustion_never_calls_provider(monkeypatch):
    calls = []
    agent = enable(monkeypatch, lambda request: calls.append(request))
    agent.context_window = 1600
    result = await agent.diagnose({"id": "s", "connector": "http"}, {"healthy": False}, [], incident_id="i")
    assert not calls
    assert result["agent_run"]["stop_reason"] == "CONTEXT_EXHAUSTED"
    assert result["action"] is None


async def test_model_turn_limit_and_full_usage_accounting(monkeypatch):
    seen = []
    def handler(request):
        data = json.loads(request.content)
        seen.append(data)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c-{len(seen)}", "function": {"name": "get_snapshot", "arguments": "{}"}}]}}]})
    result = await enable(monkeypatch, handler).diagnose({"id": "s", "connector": "http"}, {"healthy": False}, [], incident_id="i")
    assert len(seen) == 4
    assert seen[-1]["tool_choice"] == "none"
    run = result["agent_run"]
    assert run["stop_reason"] == "MAX_TURNS"
    assert run["usage"]["measured_tokens"] == 60
    assert run["tool_call_count"] <= 16
    assert run["duration_ms"] >= 0


async def test_wall_clock_deadline_is_distinct_from_model_failure(monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(500)
    agent = enable(monkeypatch, handler)
    agent.run_timeout = 0.01
    result = await agent.diagnose({"id": "s", "connector": "http"}, {"healthy": False}, [], incident_id="i")
    assert result["agent_run"]["stop_reason"] == "RUN_TIMEOUT"


async def test_cancellation_persists_reason_and_propagates(monkeypatch):
    started = asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.sleep(10)
    saved = []
    agent = enable(monkeypatch, handler)
    task = asyncio.create_task(agent.diagnose({"id": "s", "connector": "http"}, {"healthy": False}, [],
                               incident_id="i", persist_run_update=lambda run: saved.append(copy.deepcopy(run))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved[-1]["stop_reason"] == "CANCELLED"


def test_projection_removes_complete_old_exchange_and_preserves_pinned_fact():
    project = getattr(module, "project_context", None)
    assert callable(project), "Context projection is missing"
    record = {"evidence_id": "r:E01", "status": "OK", "facts": [
        {"fact_id": "F1", "kind": "business", "locator": "metrics.active", "value": 198}]}
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "incident"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old", "function": {"name": "get_snapshot", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old", "content": json.dumps(record)},
        {"role": "assistant", "content": "x"*1500},
        {"role": "assistant", "tool_calls": [{"id": "new", "function": {"name": "get_recent_logs", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "new", "content": json.dumps({"status": "EMPTY_RESULT", "facts": []})}]
    original = copy.deepcopy(messages)
    projected = project(messages, [record], {"r:E01:F1"}, ceiling=1500, tools=[])
    text = json.dumps(projected)
    assert '198' in text and 'r:E01' in text
    ids = [m.get("tool_call_id") for m in projected if m["role"] == "tool"]
    assert "old" not in ids and "new" in ids
    assert messages == original


async def test_fact_omitted_from_projection_cannot_be_cited(monkeypatch):
    saved, requests = [], []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        record = saved[-1]["evidence_records"][0]
        hidden = next(f for f in record["facts"] if f["locator"].startswith("logs"))
        citation = {"evidence_id": record["evidence_id"], "fact_id": hidden["fact_id"]}
        candidate = {"root_cause_code": "dependency_timeout", "root_cause": "dependency timeout",
                     "claims": [{"kind": "root_cause", "claim": "timeout", "citations": [citation]}], "action": None}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate)}}]})
    agent = enable(monkeypatch, handler)
    agent.context_window = 4000
    result = await agent.diagnose({"id": "s", "connector": "http"},
        {"healthy": False, "logs": ["not sent "*1000]*5}, [], incident_id="i", persist_run_update=lambda run: saved.append(run))
    assert requests
    assert result["source"] == "rules_fallback"
    assert "UNSEEN_REFERENCE" in result["agent_run"]["stop_detail"]


async def test_tool_timeout_is_an_error_observation_not_an_empty_result(monkeypatch):
    n = 0
    async def slow_tool(*args):
        await asyncio.sleep(1)
    def handler(request):
        nonlocal n
        n += 1
        if n == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "logs", "function": {
                "name": "get_recent_logs", "arguments": "{}"}}]}
        else:
            message = {"content": json.dumps({"root_cause_code": "unknown", "root_cause": "日志不可用", "claims": [], "action": None})}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    agent = enable(monkeypatch, handler)
    assert callable(getattr(module, "read_diagnostic_tool", None)), "Bounded tool execution is missing"
    monkeypatch.setattr(module, "read_diagnostic_tool", slow_tool)
    agent.tool_timeout = 0.01
    result = await agent.diagnose({"id": "s", "connector": "http"}, {"healthy": False}, [], incident_id="i")
    assert result["agent_run"]["tool_events"][0]["status"] == "TOOL_TIMEOUT"


async def test_stale_snapshot_cannot_authorize_rule_action(monkeypatch):
    monkeypatch.delenv("OPS_MODEL_API_KEY", raising=False)
    result = await Analyzer().diagnose({"id": "s", "connector": "agent"},
        {"healthy": False, "observed_at": "2000-01-01T00:00:00+00:00", "facts": {"suggested_action": "restart_service"}}, [], incident_id="i")
    assert result["action"] is None
    assert result["agent_run"]["stop_reason"] == "NO_EVIDENCE"
