import json

import httpx

from opssentinel.analyzer import Analyzer


async def test_optional_model_uses_read_only_evidence_and_returns_valid_plan(monkeypatch):
    analyzer = Analyzer()
    analyzer.api_key, analyzer.model = "example-test-key", "test-model"
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_snapshot", "arguments": "{}"}}]}}]})
        assert payload["messages"][-1]["role"] == "tool"
        evidence = json.loads(payload["messages"][-1]["content"])
        business = next(f for f in evidence["facts"] if f["kind"] == "business")
        citations = [{"evidence_id": evidence["evidence_id"], "fact_id": business["fact_id"]}]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
            "content": json.dumps({"root_cause_code": "unknown", "root_cause": "业务检查失败，退出原因仍不确定",
                "claims": [{"kind": "observation", "claim": "当前检查失败", "citations": citations},
                           {"kind": "action_support", "claim": "连接器提供重启候选，执行层仍须验证", "citations": citations}],
                "action": "restart_service"})}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    plan = await analyzer.diagnose({"connector": "agent", "agent_token": "host-private-key"},
                                  {"healthy": False, "facts": {"suggested_action": "restart_service"},
                                   "logs": ["token=host-private-key"]}, [])
    assert plan["source"] == "model"
    assert plan["action"] == "restart_service"
    assert len(seen) == 2
    assert "host-private-key" not in json.dumps(seen)


async def test_model_cannot_invent_executable_tools(monkeypatch):
    analyzer = Analyzer()
    analyzer.api_key, analyzer.model = "example-test-key", "test-model"
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "bad", "function": {"name": "run_shell", "arguments": '{"command":"delete files"}'}}]}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    plan = await analyzer.diagnose({"connector": "http"}, {"summary": "HTTP 500", "facts": {}}, [])
    assert plan["source"] == "rules_fallback"
    assert plan["action"] is None
