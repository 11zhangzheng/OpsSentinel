from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import __version__
from .analyzer import ACTION_NAMES, Analyzer, context_key
from .models import ACTIONS, Observation
from .redact import redact
from .store import Store, now
from .telemetry import maintenance_active

logger = logging.getLogger(__name__)


class Conflict(Exception):
    pass


class Engine:
    def __init__(self, store: Store, connectors, analyzer=None, *, cooldown_seconds=20, max_attempts=2):
        self.store, self.connectors = store, connectors
        self.analyzer = analyzer or Analyzer()
        self.cooldown_seconds, self.max_attempts = cooldown_seconds, max_attempts
        self.started_at = now()
        self.locks: dict[str, asyncio.Lock] = {}
        self.probe_slots = asyncio.Semaphore(4)
        self.analysis_slots = asyncio.Semaphore(2)
        self.action_slots = asyncio.Semaphore(2)
        self.tasks: dict[str, asyncio.Task] = {}
        self.scheduler = None
        self.stopping = False

    def lock(self, sid):
        return self.locks.setdefault(sid, asyncio.Lock())

    async def start(self, *, schedule=True):
        self.store.recover_interrupted()
        await self.connectors.start()
        if self.connectors.enable_demo:
            config = self.connectors.demo_service_config()
            config.pop("id", None)
            config["name"] = "演练 · 示例订单服务"
            if self.store.get_service("demo-service"):
                # Preserve the user's pause and approval policy across restarts.
                self.store.patch_service("demo-service", {"target": config["target"]})
            else:
                self.store.add_service(config, "demo-service")
        elif self.store.get_service("demo-service"):
            self.store.patch_service("demo-service", {"enabled": False})
        if schedule:
            self.scheduler = asyncio.create_task(self.run(), name="opssentinel-scheduler")

    async def close(self):
        self.stopping = True
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        pending = list(self.tasks.values())
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=5)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self.connectors.close()

    async def run(self):
        while not self.stopping:
            try:
                for service in self.store.list_services():
                    sid = service["id"]
                    if not service["enabled"] or sid in self.tasks:
                        continue
                    due = service.get("next_check_at")
                    if due and datetime.fromisoformat(due) > datetime.now(timezone.utc):
                        continue
                    task = asyncio.create_task(self.scan_service(sid), name=f"scan-{sid}")
                    self.tasks[sid] = task
                    task.add_done_callback(lambda t, key=sid: self._finished(key, t))
            except Exception:
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(1)

    def _finished(self, sid, task):
        self.tasks.pop(sid, None)
        if not task.cancelled() and task.exception():
            self.store.event("error", "巡检遇到内部异常，后续巡检仍会继续", sid)
            logger.error("Service scan failed: %s", type(task.exception()).__name__)

    async def _observe(self, service):
        try:
            async with self.probe_slots:
                snapshot = await asyncio.wait_for(self.connectors.observe(service), timeout=40)
            snapshot = Observation.model_validate(snapshot).model_dump()
        except Exception as exc:
            snapshot = {"healthy": False, "reachable": False,
                        "summary": f"连接器检查未完成（{type(exc).__name__}）", "latency_ms": None,
                        "checks": [], "metrics": {}, "logs": [], "facts": {}}
        snapshot = redact(snapshot, (service.get("agent_token", ""), self.analyzer.api_key))
        snapshot["observed_at"] = now()
        if service["connector"] == "demo" and self.connectors.enable_demo:
            current_target = self.connectors.demo_service_config()["target"]
            if current_target != service["target"]:
                self.store.patch_service(service["id"], {"target": current_target})
        service = self.store.record_observation(service["id"], snapshot)
        self.store.evaluate_resources(service, snapshot, now(), suppress_new=not service["enabled"] or maintenance_active(service))
        return service, snapshot

    def _expire_maintenance(self, service):
        if service.get("maintenance_until") and not maintenance_active(service):
            self.store.patch_service(service["id"], {"maintenance_until": None, "maintenance_reason": ""})
            self.store.reset_check_counts(service["id"])
            self.store.event("maintenance_ended", "维护窗口到期，重新累计新鲜检查后恢复事故发现与处置", service["id"])
            return self.store.get_service(service["id"])
        return service

    async def maintenance(self, sid, *, minutes=None, reason=""):
        if self.lock(sid).locked():
            raise Conflict("服务正在检查或处置，请稍后设置维护；当前动作无法中途撤销")
        async with self.lock(sid):
            service = self.store.get_service(sid)
            if not service:
                raise KeyError(sid)
            until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat() if minutes else None
            service = self.store.patch_service(sid, {"maintenance_until": until, "maintenance_reason": reason if minutes else ""})
            self.store.reset_check_counts(sid)
            self.store.event("maintenance_started" if until else "maintenance_ended",
                             f"进入维护窗口 {minutes} 分钟：{reason}；继续采样，暂停新事故与处置" if until else "维护结束，重新累计新鲜检查后恢复事故发现与处置", sid)
            return self.service_view(service)

    async def scan_service(self, sid: str, *, manual=False):
        async with self.lock(sid):
            service = self.store.get_service(sid)
            if not service:
                raise KeyError(sid)
            if not service["enabled"] and not manual:
                return
            service = self._expire_maintenance(service)
            if self.service_view(service)["freshness"] == "stale":
                self.store.reset_check_counts(sid)
            next_stamp = (datetime.now(timezone.utc) + timedelta(seconds=service["interval_seconds"])).isoformat()
            self.store.set_next_check(sid, next_stamp)
            service, snapshot = await self._observe(service)
            incident = self.store.active_incident(sid)
            if snapshot["healthy"]:
                if incident and service["consecutive_successes"] >= service["recovery_threshold"]:
                    confirmed = (incident["status"] == "verifying" and not incident.get("stopped_by_user")
                                 and self.store.has_confirmed_latest_action(incident["id"]))
                    kind = "mitigated" if confirmed else "externally_recovered"
                    evidence = {**incident.get("evidence", {}), "recovery": snapshot}
                    self.store.update_incident(incident["id"], status="resolved", resolved_at=now(), resolution_kind=kind, evidence=evidence)
                    self.store.event("resolved", f"连续 {service['consecutive_successes']} 次新鲜探针通过，业务恢复；根因是否消除仍需单独确认", sid, incident["id"])
                elif incident:
                    self.store.event("verification", f"恢复观察 {service['consecutive_successes']}/{service['recovery_threshold']}：探针通过", sid, incident["id"])
                return
            if not service["enabled"] or maintenance_active(service):
                return  # Manual checks on paused services never perform writes.
            if not incident:
                if service["consecutive_failures"] < service["failure_threshold"]:
                    return
                incident = self.store.create_incident(service, snapshot)
                await self._diagnose_and_route(service, snapshot, incident)
            elif incident["status"] == "verifying":
                last = incident.get("last_action_at")
                if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < self.cooldown_seconds:
                    return
                if incident["attempts"] >= self.max_attempts:
                    self.store.update_incident(incident["id"], status="escalated")
                    self.store.event("escalated", "恢复验证未通过且已到处置次数上限，停止自动动作", sid, incident["id"])
                else:
                    await self._diagnose_and_route(service, snapshot, incident)
            # awaiting_approval and escalated remain under observation without duplicate actions.

    async def _diagnose_and_route(self, service, snapshot, incident):
        self.store.update_incident(incident["id"], status="investigating")
        try:
            async with self.analysis_slots:
                diagnosis = await asyncio.wait_for(self.analyzer.diagnose(service, snapshot,
                              [i for i in self.store.list_incidents() if i["service_id"] == service["id"]]), timeout=35)
        except Exception:
            diagnosis = self.analyzer.rule_diagnosis(service, snapshot)
        action = diagnosis.get("action")
        if action not in ACTIONS or service["connector"] == "http":
            action = None
        allowed = snapshot.get("facts", {}).get("allowed_actions")
        if service["connector"] == "agent" and allowed is not None and (not isinstance(allowed, list) or action not in allowed):
            action = None
        incident = self.store.update_incident(incident["id"], diagnosis=diagnosis["diagnosis"], action=action,
                         diagnostic_source=diagnosis["source"], context_key=diagnosis.get("context_key", context_key(snapshot)))
        self.store.event("diagnosis", diagnosis["diagnosis"], service["id"], incident["id"])
        if not action:
            self.store.update_incident(incident["id"], status="escalated")
            self.store.event("escalated", "无已验证的处置路径，保持观测并等待人工调查", service["id"], incident["id"])
        elif action in service["auto_actions"]:
            await self._execute(service, incident, action)
        else:
            self.store.update_incident(incident["id"], status="awaiting_approval")
            self.store.event("approval", f"已准备 {ACTION_NAMES[action]}，当前服务策略需要批准这一次操作", service["id"], incident["id"])

    async def _execute(self, service, incident, action, *, approved=False):
        async with self.action_slots:
            await self._execute_in_slot(service, incident, action, approved=approved)

    async def _execute_in_slot(self, service, incident, action, *, approved=False):
        # Independently enforce permissions; model output never grants authority.
        service = self.store.get_service(service["id"])
        incident = self.store.get_incident(incident["id"])
        if not service["enabled"] or service["connector"] not in {"demo", "agent"} or action not in ACTIONS:
            raise Conflict("服务已暂停或连接器不允许处置")
        if maintenance_active(service):
            raise Conflict("服务处于维护窗口，只观察，不执行恢复动作")
        if incident["stopped_by_user"] or incident["attempts"] >= self.max_attempts:
            raise Conflict("事故已停止自动处置或次数达到上限")
        if not approved and action not in service["auto_actions"]:
            self.store.update_incident(incident["id"], status="awaiting_approval")
            return
        # Recheck after diagnosis: a model call or approval delay may have made evidence stale.
        service, fresh = await self._observe(service)
        if fresh["healthy"]:
            self.store.update_incident(incident["id"], status="verifying")
            self.store.event("verification", "执行前探针已恢复，取消动作并进入恢复观察", service["id"], incident["id"])
            return
        if context_key(fresh) != incident["context_key"] or fresh.get("facts", {}).get("suggested_action") != action:
            self.store.update_incident(incident["id"], status="escalated")
            self.store.event("stale", "现场证据已变化，原处置方案失效；没有执行旧动作", service["id"], incident["id"])
            if approved:
                raise Conflict("现场状态已经变化，原批准方案已失效，请查看最新证据")
            return
        evidence = incident.get("evidence", {})
        evidence["before_actions"] = [*evidence.get("before_actions", []), fresh][-self.max_attempts:]
        self.store.update_incident(incident["id"], evidence=evidence)
        operation_id = self.store.begin_action(incident, action)
        self.store.event("action_started", f"执行 {ACTION_NAMES[action]}；操作编号 {operation_id[:10]}", service["id"], incident["id"])
        try:
            result = await asyncio.wait_for(self.connectors.execute({**service, "_operation_id": operation_id}, action), timeout=100)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("Invalid action result")
        except asyncio.CancelledError:
            # Intent remains persisted. On restart it becomes uncertain and must not be blindly replayed.
            raise
        except Exception as exc:
            result = {"ok": False, "summary": f"操作结果无法确认（{type(exc).__name__}），需核对现场后处理",
                      "details": {"outcome_unknown": True}}
        result = redact(result, (service.get("agent_token", ""), self.analyzer.api_key))
        if isinstance(result.get("details"), dict) and result["details"].get("outcome_unknown"):
            result["ok"] = False
        self.store.finish_action(operation_id, result)
        self.store.event("action_result", result.get("summary", "动作已返回"), service["id"], incident["id"])
        self.store.update_incident(incident["id"], status="verifying" if result["ok"] else "escalated")
        if result["ok"]:
            self.store.event("verification", "动作完成，等待连续新鲜业务探针通过，尚未宣告恢复", service["id"], incident["id"])
        else:
            self.store.event("escalated", "执行失败或结果不明；未重复发出写操作", service["id"], incident["id"])

    async def approve(self, iid):
        incident = self.store.get_incident(iid)
        if not incident:
            raise KeyError(iid)
        async with self.lock(incident["service_id"]):
            incident = self.store.get_incident(iid)
            if incident["status"] != "awaiting_approval" or not incident["action"]:
                raise Conflict("事故已不处于待批准状态，请刷新")
            service = self.store.get_service(incident["service_id"])
            if maintenance_active(service):
                raise Conflict("服务处于维护窗口，结束维护后才能批准处置")
            self.store.event("approved", "操作员批准本次具体处置；不会修改服务的长期授权", service["id"], iid)
            await self._execute(service, incident, incident["action"], approved=True)

    async def dismiss(self, iid, reason):
        incident = self.store.get_incident(iid)
        if not incident:
            raise KeyError(iid)
        async with self.lock(incident["service_id"]):
            incident = self.store.get_incident(iid)
            if incident["status"] == "resolved":
                raise Conflict("事故已恢复，无需停止")
            self.store.update_incident(iid, status="escalated", stopped_by_user=True)
            self.store.event("stopped", f"操作员停止自动处置：{reason}。健康监测继续。", incident["service_id"], iid)

    def service_view(self, service):
        result = {k: v for k, v in service.items() if k not in {"agent_token", "agent_token_env"}}
        active = maintenance_active(service)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(service["last_check_at"])).total_seconds() if service.get("last_check_at") else None
        result["maintenance_active"] = active
        result["freshness"] = ("paused" if not service["enabled"] else "maintenance" if active else "unknown" if age is None
                               else "stale" if age > max(30, 3 * service["interval_seconds"] + 40) else "fresh")
        return result

    def state(self):
        services = []
        for service in self.store.list_services():
            services.append(self.service_view(service))
        incidents = [{**i, "events": list(reversed(self.store.events(i["id"]))) } for i in self.store.list_incidents()]
        counts = self.store.incident_counts()
        return {"services": services, "incidents": incidents, "resource_alerts": self.store.resource_alerts(),
                "summary": {"services": len(services), "healthy": sum(s["health"] == "healthy" and s["freshness"] == "fresh" for s in services),
                            "unhealthy": sum(s["health"] == "unhealthy" and s["freshness"] == "fresh" for s in services),
                            "stale_services": sum(s["freshness"] == "stale" for s in services),
                            "maintenance_services": sum(s["maintenance_active"] for s in services),
                            "firing_alerts": self.store.firing_resource_count(),
                            "open_incidents": sum(n for status, n in counts.items() if status != "resolved"),
                            "resolved_incidents": counts.get("resolved", 0),
                            "awaiting_approval": counts.get("awaiting_approval", 0)},
                "runtime": {"version": __version__, "started_at": self.started_at,
                            "mode": "demo" if self.connectors.enable_demo else "live",
                            "model_enabled": self.analyzer.enabled, "scan_running": bool(self.tasks)},
                "recent_events": self.store.events(limit=25)}
