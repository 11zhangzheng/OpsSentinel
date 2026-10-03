from __future__ import annotations

import asyncio
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
from urllib.parse import quote
import uuid

import httpx
import psutil

from .connector_helpers import ResponseTooLargeError, atomic_json, http_request, tail_lines, validate_agent_base, target_fingerprint
from .demo_service import LOG_LIMIT

ACTIONS = {"restart_service", "rollback_release", "restore_config", "rotate_logs"}


def failure(summary: str, status: str = "TOOL_ERROR") -> dict:
    return {"healthy": False, "reachable": False, "summary": summary, "latency_ms": None,
            "checks": [{"name": "connection", "ok": False, "detail": summary}],
            "metrics": {}, "logs": [], "facts": {}, "source_status": {"snapshot": status}}


class ConnectorManager:
    def __init__(self, data_dir: Path, enable_demo: bool = True):
        self.data_dir = Path(data_dir)
        self.demo_dir = self.data_dir / "demo"
        self.enable_demo = enable_demo
        self._child: subprocess.Popen | None = None
        self._owned_process: psutil.Process | None = None
        self._target = ""
        self._demo_lock = asyncio.Lock()
        self._port_file = self.demo_dir / ("port-" + uuid.uuid4().hex + ".json")

    async def start(self) -> None:
        if not self.enable_demo:
            return
        self.demo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.demo_dir / "release.json").exists():
            atomic_json(self.demo_dir / "release.json", {"current_image": "exercise:v1", "previous_image": "exercise:v1"})
        if not (self.demo_dir / "config.json").exists():
            atomic_json(self.demo_dir / "config.json", {"mode": "valid"})
        (self.demo_dir / "application.log").touch(exist_ok=True)
        with sqlite3.connect(self.demo_dir / "operations.sqlite3") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, action TEXT NOT NULL, result TEXT)")
        await self._start_child()

    async def _start_child(self) -> None:
        if self._child is not None and self._child.poll() is None:
            return
        self._port_file.unlink(missing_ok=True)
        args = [sys.executable, "-m", "opssentinel.demo_service", "--data-dir", str(self.demo_dir.resolve()),
                "--port-file", str(self._port_file.resolve()), "--parent-pid", str(os.getpid()),
                "--parent-created", str(psutil.Process().create_time())]
        self._child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False,
                                       cwd=str(Path(__file__).resolve().parent.parent))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._child.poll() is not None:
                raise RuntimeError("Isolated exercise subprocess failed to start")
            if self._port_file.exists():
                data = json.loads(self._port_file.read_text(encoding="utf-8"))
                # Windows virtualenv launchers may delegate to a child python.exe.
                # Verify ancestry, rather than assuming the HTTP server's PID is
                # the redirector PID returned by Popen.
                process = psutil.Process(int(data["pid"]))
                owned = process.pid == self._child.pid or any(parent.pid == self._child.pid for parent in process.parents())
                if owned:
                    self._owned_process = process
                    self._target = f"http://127.0.0.1:{int(data['port'])}/health"
                    return
            await asyncio.sleep(0.05)
        await self._stop_child()
        raise TimeoutError("Isolated exercise subprocess startup timed out")

    async def _stop_child(self) -> None:
        child = self._child
        process = self._owned_process
        if process is not None:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, 3)
            except psutil.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
            except psutil.NoSuchProcess:
                pass
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, 3)
            except subprocess.TimeoutExpired:
                child.kill()
                await asyncio.to_thread(child.wait, 3)
        self._child = None
        self._owned_process = None

    async def close(self) -> None:
        async with self._demo_lock:
            await self._stop_child()
            self._port_file.unlink(missing_ok=True)

    def demo_service_config(self) -> dict:
        return {"id": "demo-service", "name": "Isolated recovery exercise", "connector": "demo",
                "target": self._target, "interval_seconds": 5, "failure_threshold": 2,
                "recovery_threshold": 2, "auto_actions": sorted(ACTIONS), "enabled": self.enable_demo,
                "agent_service": ""}

    def _demo_target_facts(self):
        release = json.loads((self.demo_dir/"release.json").read_text(encoding="utf-8"))
        config = (self.demo_dir/"config.json").read_bytes()
        log = (self.demo_dir/"application.log").stat()
        return {**release, "exercise_identity": str(self.demo_dir.resolve()),
                "config_hash": hashlib.sha256(config).hexdigest(),
                "known_good_config_hash": hashlib.sha256(json.dumps({"mode": "valid"}).encode()).hexdigest(),
                "managed_log_identity": f"{log.st_dev}:{log.st_ino}"}

    async def observe(self, service: dict) -> dict:
        connector = service.get("connector")
        started = time.monotonic()
        try:
            if connector == "agent":
                base = validate_agent_base(service["target"])
                identifier = quote(str(service.get("agent_service", "")), safe="")
                if not identifier:
                    return failure("A configured host service ID is required")
                code, body = await http_request(base + "/v1/services/" + identifier + "/observe",
                                                token=service.get("agent_token"), timeout=30)
                if code != 200:
                    return failure(f"Host agent returned HTTP {code}")
                result = json.loads(body)
                if not isinstance(result, dict) or not isinstance(result.get("healthy"), bool):
                    return failure("Host agent returned an invalid observation")
                return result
            if connector not in {"http", "demo"}:
                return failure("Unsupported connector")
            if connector == "demo" and (not self.enable_demo or self._child is None or self._child.poll() is not None):
                result = failure("Owned exercise process is stopped")
                result["facts"] = {"suggested_action": "restart_service", "exercise": True, "process_running": False}
                result["facts"].update(self._demo_target_facts())
                result["source_status"] = {"snapshot": "OK"}
                result["logs"] = tail_lines(self.demo_dir / "application.log")
                return result
            if connector == "http":
                return await self._observe_http(service)
            target = self._target
            code, body = await http_request(target)
            result = {"healthy": 200 <= code < 300, "reachable": True,
                      "summary": f"HTTP health probe returned {code}",
                      "latency_ms": round((time.monotonic() - started) * 1000, 1),
                      "checks": [{"name": "http", "ok": 200 <= code < 300, "detail": f"HTTP {code}"}],
                      "metrics": {}, "logs": [], "facts": {}}
            if connector == "demo":
                details = json.loads(body)
                business_code, _ = await http_request(self._target.replace("/health", "/business"))
                result.update(details)
                result["healthy"] = details.get("healthy") is True and business_code == 200
                result["checks"].append({"name": "business_http", "ok": business_code == 200, "detail": f"HTTP {business_code}"})
                result["logs"] = tail_lines(self.demo_dir / "application.log")
                result["facts"].update(self._demo_target_facts())
            return result
        except Exception as exc:
            # Never return raw exception URLs/headers, which may contain secrets.
            return failure(f"Probe failed ({type(exc).__name__})")

    async def _observe_http(self, service: dict) -> dict:
        """Evaluate an explicit HTTP contract without retaining response content."""
        probe = service.get("http_probe") or {}
        timeout = probe.get("timeout_seconds", 5.0)
        expected = probe.get("expected_status")
        contains = probe.get("body_contains", "")
        # Service models validate persisted configuration. Keep this boundary safe
        # for direct connector use and older/custom service stores as well.
        if (not isinstance(timeout, (float, int)) or isinstance(timeout, bool) or not 1 <= timeout <= 30
                or (expected is not None and (not isinstance(expected, int) or isinstance(expected, bool) or not 100 <= expected <= 599))
                or not isinstance(contains, str) or len(contains) > 500):
            return failure("HTTP probe configuration is invalid")
        started = time.monotonic()
        try:
            code, body = await http_request(service["target"], timeout=float(timeout))
            status_ok = code == expected if expected is not None else 200 <= code < 300
            expected_detail = str(expected) if expected is not None else "2xx"
            checks = [{"name": "http", "ok": status_ok, "detail": f"HTTP {code}; expected {expected_detail}"}]
            if contains:
                # Literal UTF-8 matching; neither the response nor the configured
                # match string is copied into observations, evidence or model logs.
                matched = contains.encode("utf-8") in body
                checks.append({"name": "response_body", "ok": matched,
                               "detail": "Required content matched" if matched else "Required content was not found"})
            healthy = all(check["ok"] for check in checks)
            problems = ["status code" if check["name"] == "http" else "required content" for check in checks if not check["ok"]]
            summary = f"HTTP contract passed (HTTP {code})" if healthy else "HTTP contract failed: " + ", ".join(problems)
            return {"healthy": healthy, "reachable": True, "summary": summary,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "checks": checks, "metrics": {}, "logs": [], "facts": {}}
        except ResponseTooLargeError as exc:
            return {"healthy": False, "reachable": True, "summary": "HTTP response exceeded the 64 KiB probe limit",
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "checks": [{"name": "response_size", "ok": False,
                                "detail": f"HTTP {exc.status_code}; response exceeded 64 KiB"}],
                    "metrics": {}, "logs": [], "facts": {}}
        except (TimeoutError, httpx.TimeoutException):
            observation = failure(f"HTTP probe exceeded the {float(timeout):g} second timeout")
            observation["source_status"] = {"snapshot": "OK"}
            observation["checks"][0]["name"] = "timeout"
            observation["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            return observation
        except Exception as exc:
            return failure(f"HTTP connection probe failed ({type(exc).__name__})")

    async def execute(self, service: dict, action: str) -> dict:
        if action not in ACTIONS:
            return {"ok": False, "summary": "Unsupported action", "details": {}}
        if service.get("connector") == "http":
            return {"ok": False, "summary": "HTTP connector is observe-only", "details": {}}
        if service.get("connector") == "agent":
            if not service.get("_expected_context_key") or not service.get("_plan_id"):
                return {"ok": False, "summary": "Missing bound target/plan identity", "details": {}}
            operation = service.get("_operation_id") or uuid.uuid4().hex
            try:
                base = validate_agent_base(service["target"])
                identifier = quote(str(service.get("agent_service", "")), safe="")
                if not identifier:
                    raise ValueError("Missing service ID")
                code, body = await http_request(base + "/v1/services/" + identifier + "/actions", method="POST",
                    token=service.get("agent_token"), payload={"action": action, "operation_id": operation,
                    "expected_context_key": service["_expected_context_key"], "plan_id": service["_plan_id"]}, timeout=90)
                if code != 200:
                    return {"ok": False, "summary": f"Host agent returned HTTP {code}", "details": {"operation_id": operation}}
                result = json.loads(body)
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    raise ValueError("Invalid action response")
                return result
            except Exception as exc:
                return {"ok": False, "summary": f"Host action outcome unavailable ({type(exc).__name__}); inspect before retrying",
                        "details": {"operation_id": operation, "outcome_unknown": True}}
        if service.get("connector") != "demo" or not self.enable_demo:
            return {"ok": False, "summary": "Exercise is not enabled", "details": {}}
        async with self._demo_lock:
            operation = service.get("_operation_id") or uuid.uuid4().hex
            with sqlite3.connect(self.demo_dir / "operations.sqlite3") as connection:
                previous = connection.execute("SELECT action,result FROM operations WHERE id=?", (operation,)).fetchone()
                if previous:
                    if previous[0] != action:
                        return {"ok": False, "summary": "Operation ID belongs to a different action", "details": {}}
                    if previous[1] is None:
                        return {"ok": False, "summary": "Previous exercise action outcome unknown; inspect before retrying",
                                "details": {"outcome_unknown": True, "operation_id": operation}}
                    outcome = json.loads(previous[1])
                    if service.get("_plan_id") and (outcome.get("details", {}).get("plan_id") != service["_plan_id"] or outcome.get("details", {}).get("expected_context_key") != service.get("_expected_context_key")):
                        return {"ok": False, "summary": "Operation belongs to a different plan", "details": {}}
                    if outcome.get("pending"):
                        return {"ok": False, "summary": "Previous action outcome unknown", "details": {"outcome_unknown": True}}
                    outcome["details"]["replayed"] = True
                    return outcome
                if service.get("_expected_context_key"):
                    if target_fingerprint({"facts": self._demo_target_facts()}, action) != service["_expected_context_key"]:
                        return {"ok": False, "summary": "Exercise target changed before mutation", "details": {}}
                intent = {"pending": True, "details": {"plan_id": service.get("_plan_id"), "expected_context_key": service.get("_expected_context_key")}}
                connection.execute("INSERT INTO operations VALUES (?,?,?)", (operation, action, json.dumps(intent)))
            outcome = await self._execute_demo(action)
            outcome["details"]["operation_id"] = operation
            outcome["details"].update(plan_id=service.get("_plan_id"), expected_context_key=service.get("_expected_context_key"))
            with sqlite3.connect(self.demo_dir / "operations.sqlite3") as connection:
                connection.execute("UPDATE operations SET result=? WHERE id=?", (json.dumps(outcome), operation))
            return outcome

    async def _execute_demo(self, action: str) -> dict:
        if action == "restart_service":
            await self._stop_child()
            await self._start_child()
        elif action == "rollback_release":
            release = json.loads((self.demo_dir / "release.json").read_text(encoding="utf-8"))
            if release.get("previous_image") != "exercise:v1":
                return {"ok": False, "summary": "No known compatible exercise release", "details": {}}
            release["current_image"] = release["previous_image"]
            atomic_json(self.demo_dir / "release.json", release)
        elif action == "restore_config":
            atomic_json(self.demo_dir / "config.json", {"mode": "valid"})
        elif action == "rotate_logs":
            log = self.demo_dir / "application.log"
            previous = self.demo_dir / "application.log.1"
            os.replace(log, previous)
            log.write_text("Exercise managed log rotated\n", encoding="utf-8")
        return {"ok": True, "summary": f"Exercise action {action} completed; fresh probes must verify recovery",
                "details": {"exercise": True, "verification_required": True}}

    async def inject_fault(self, fault: str) -> dict:
        if not self.enable_demo:
            return {"ok": False, "summary": "Exercise is not enabled", "details": {}}
        if fault not in {"bad_release", "bad_config", "process_exit", "log_pressure", "recover"}:
            return {"ok": False, "summary": "Unknown exercise fault", "details": {}}
        async with self._demo_lock:
            if fault == "bad_release":
                atomic_json(self.demo_dir / "release.json", {"current_image": "exercise:v2-broken", "previous_image": "exercise:v1"})
            elif fault == "bad_config":
                atomic_json(self.demo_dir / "config.json", {"mode": "invalid"})
            elif fault == "process_exit":
                await self._stop_child()
            elif fault == "log_pressure":
                (self.demo_dir / "application.log").write_text("EXERCISE log pressure\n" * (LOG_LIMIT // 10), encoding="utf-8")
            else:
                atomic_json(self.demo_dir / "release.json", {"current_image": "exercise:v1", "previous_image": "exercise:v1"})
                atomic_json(self.demo_dir / "config.json", {"mode": "valid"})
                (self.demo_dir / "application.log").write_text("Exercise reset\n", encoding="utf-8")
                await self._start_child()
            return {"ok": True, "summary": f"Injected {fault} in isolated localhost exercise", "details": {"exercise": True}}
