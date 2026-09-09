import asyncio
import threading
from types import SimpleNamespace

import pytest

from opssentinel.host_agent import HostRuntime


@pytest.fixture
def runtime(tmp_path):
    return HostRuntime({"state_dir": tmp_path / "host-state", "services": {"web": {
        "compose_file": str(tmp_path / "compose.yaml"), "compose_service": "web",
        "health_url": "http://127.0.0.1:1/health", "allowed_actions": [],
        "max_log_bytes": 1024, "rollback_window_seconds": 600,
    }}})


@pytest.mark.parametrize("action", ["restart_service", "rotate_logs"])
async def test_host_suggests_evidenced_action_only_when_allowlisted(runtime, tmp_path, monkeypatch, action):
    service = runtime.services["web"]
    if action == "rotate_logs":
        log = tmp_path / "managed.log"
        log.write_bytes(b"x" * 2048)
        service["managed_log_path"] = str(log)
    monkeypatch.setattr(runtime, "_container", lambda _: {"known": True, "exists": True,
                        "running": action != "restart_service", "summary": "Container observed"})
    monkeypatch.setattr("opssentinel.host_agent.bounded_command", lambda *args, **kwargs: {"ok": True, "output": ""})
    async def probe(name, url):
        return {"name": name, "ok": True, "detail": "HTTP 200", "reachable": True}
    monkeypatch.setattr(runtime, "_probe", probe)
    denied = await runtime.observe("web")
    assert not denied["healthy"]
    assert "suggested_action" not in denied["facts"]
    service["allowed_actions"] = [action]
    permitted = await runtime.observe("web")
    assert permitted["facts"]["suggested_action"] == action


async def test_concurrent_services_share_interval_sample_without_blocking_loop(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()

    def interval_sample(interval):
        calls.append((interval, threading.get_ident()))
        entered.set()
        assert release.wait(3), "event loop did not release the worker"
        return 42.5

    monkeypatch.setattr("opssentinel.host_agent.psutil.cpu_percent", interval_sample)
    first = asyncio.create_task(runtime._host_metrics())
    second = asyncio.create_task(runtime._host_metrics())
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert not first.done() and not second.done()
    finally:
        release.set()
    one, two = await asyncio.gather(first, second)
    assert one["cpu_percent"] == two["cpu_percent"] == 42.5
    assert len(calls) == 1 and calls[0] == (0.2, calls[0][1])
    assert calls[0][1] != main_thread
    one["log_bytes"] = 12345
    assert "log_bytes" not in await runtime._host_metrics()
    runtime._metrics_sample_at -= 2
    await runtime._host_metrics()
    assert len(calls) == 2


async def test_missing_host_metric_does_not_fabricate_zero_or_fail_service(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_container", lambda _: {"known": True, "exists": False,
                        "running": True, "summary": "Container running"})
    async def probe(name, url):
        return {"name": name, "ok": True, "detail": "HTTP 200", "reachable": True}
    monkeypatch.setattr(runtime, "_probe", probe)
    monkeypatch.setattr("opssentinel.host_agent.psutil.cpu_percent", lambda **kwargs: float("nan"))
    monkeypatch.setattr("opssentinel.host_agent.psutil.virtual_memory", lambda: SimpleNamespace(percent=73))
    def unavailable(*args):
        raise PermissionError("disk query unavailable")
    monkeypatch.setattr("opssentinel.host_agent.psutil.disk_usage", unavailable)
    observation = await runtime.observe("web")
    assert observation["healthy"] and observation["reachable"]
    assert observation["metrics"] == {"memory_percent": 73}
    assert "suggested_action" not in observation["facts"]
