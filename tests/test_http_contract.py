import asyncio
from contextlib import suppress
import gzip
import json
import time

import pytest

from opssentinel.connector_helpers import MAX_BODY
from opssentinel.connectors import ConnectorManager


@pytest.fixture
async def probe_server():
    responses = {}
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split(b" ", 2)[1].decode("ascii")
            response = responses[path]
            await asyncio.sleep(response.get("delay", 0))
            body = response.get("body", b"")
            code = response.get("status", 200)
            headers = f"HTTP/1.1 {code} Test\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
            if response.get("gzip"):
                headers += "Content-Encoding: gzip\r\n"
            writer.write(headers.encode() + b"\r\n")
            if response.get("drip"):
                for byte in body:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(response["drip"])
            else:
                writer.write(body)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield url, responses
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def service(url, **probe):
    return {"connector": "http", "target": url, "http_probe": probe}


@pytest.mark.parametrize("code,expected,healthy", [
    (200, None, True), (201, None, True), (503, None, False),
    (201, 200, False), (401, 401, True), (302, None, False),
])
async def test_real_http_status_contract(tmp_path, probe_server, code, expected, healthy):
    url, responses = probe_server
    responses["/status"] = {"status": code, "body": b"private response contents"}
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/status", expected_status=expected))
    assert observation["healthy"] is healthy
    assert observation["reachable"]
    assert observation["checks"][0]["ok"] is healthy
    assert str(code) in observation["checks"][0]["detail"]
    assert observation["latency_ms"] >= 0
    assert "private response" not in json.dumps(observation)
    assert not observation["facts"] and not observation["logs"]


@pytest.mark.parametrize("match,healthy", [("业务就绪", True), ("业务失败", False)])
async def test_utf8_body_contract_never_records_pattern_or_response(tmp_path, probe_server, match, healthy):
    url, responses = probe_server
    responses["/body"] = {"body": "private-api-key=supersecret 业务就绪".encode()}
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/body", body_contains=match))
    assert observation["healthy"] is healthy
    assert observation["checks"][1]["name"] == "response_body"
    assert observation["checks"][1]["ok"] is healthy
    encoded = json.dumps(observation, ensure_ascii=False)
    assert match not in encoded and "supersecret" not in encoded
    assert (await ConnectorManager(tmp_path, False).execute(service(url), "restart_service"))["ok"] is False


async def test_matching_body_does_not_override_status_failure(tmp_path, probe_server):
    url, responses = probe_server
    responses["/wrong-status"] = {"status": 503, "body": b"ready"}
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/wrong-status", body_contains="ready"))
    assert not observation["healthy"]
    assert [item["ok"] for item in observation["checks"]] == [False, True]
    assert "status code" in observation["summary"]


@pytest.mark.parametrize("size,healthy", [(MAX_BODY, True), (MAX_BODY + 1, False), (MAX_BODY * 4, False)])
async def test_response_bound_applies_even_if_prefix_matches(tmp_path, probe_server, size, healthy):
    url, responses = probe_server
    responses["/payload"] = {"body": b"ready" + b"z" * (size - 5)}
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/payload", body_contains="ready"))
    assert observation["healthy"] is healthy
    assert observation["reachable"]
    if not healthy:
        assert observation["checks"][0]["name"] == "response_size"
        assert "64 KiB" in observation["summary"]
    assert "zzzz" not in json.dumps(observation)


async def test_bound_applies_to_decompressed_payload(tmp_path, probe_server):
    url, responses = probe_server
    responses["/compressed"] = {"body": gzip.compress(b"z" * (MAX_BODY + 1)), "gzip": True}
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/compressed"))
    assert not observation["healthy"] and observation["reachable"]
    assert observation["checks"][0]["name"] == "response_size"


@pytest.mark.parametrize("response", [{"delay": 2, "body": b"ready"}, {"body": b"1234567890", "drip": 0.3}])
async def test_total_timeout_includes_slow_headers_and_dripping_body(tmp_path, probe_server, response):
    url, responses = probe_server
    responses["/slow"] = response
    started = time.monotonic()
    observation = await ConnectorManager(tmp_path, False).observe(service(url + "/slow", timeout_seconds=1))
    elapsed = time.monotonic() - started
    assert not observation["healthy"]
    assert observation["checks"][0]["name"] == "timeout"
    assert "1 second timeout" in observation["summary"]
    assert 0.9 <= elapsed < 2.5


async def test_connection_failure_does_not_disclose_exception_contents(tmp_path, monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError("http://private-host/path?token=supersecret")
    monkeypatch.setattr("opssentinel.connectors.http_request", fail)
    observation = await ConnectorManager(tmp_path, False).observe(service("http://localhost"))
    assert not observation["healthy"] and not observation["reachable"]
    assert "OSError" in observation["summary"]
    assert "supersecret" not in json.dumps(observation)


@pytest.mark.parametrize("probe", [{"timeout_seconds": 0}, {"timeout_seconds": 31}, {"timeout_seconds": True},
                                  {"expected_status": 99}, {"expected_status": 600}, {"body_contains": "x" * 501}])
async def test_invalid_direct_connector_contract_refuses_request(tmp_path, monkeypatch, probe):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid probe must not issue a request")
    monkeypatch.setattr("opssentinel.connectors.http_request", unexpected)
    observation = await ConnectorManager(tmp_path, False).observe(service("http://localhost", **probe))
    assert not observation["healthy"] and "configuration is invalid" in observation["summary"]
