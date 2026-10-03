from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .telemetry import TelemetryMixin, maintenance_active


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


class Store(TelemetryMixin):
    """Small synchronous transactions; no database transaction spans a tool call."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS services (
                id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, service_id TEXT NOT NULL REFERENCES services(id),
                status TEXT NOT NULL, document TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident
                ON incidents(service_id) WHERE status != 'resolved';
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                incident_id TEXT, service_id TEXT, kind TEXT NOT NULL,
                message TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, service_id TEXT NOT NULL,
                created_at TEXT NOT NULL, document TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observations_service ON observations(service_id, seq);
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                action TEXT NOT NULL, status TEXT NOT NULL, document TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS actions_incident ON actions(incident_id);
        """)
        self.db.commit()
        self.init_telemetry()

    def close(self):
        with self.lock:
            self.db.close()

    def add_service(self, config: dict, service_id: str | None = None) -> dict:
        sid = service_id or new_id()
        state = {"health": "unknown", "last_check_at": None, "next_check_at": None,
                 "consecutive_failures": 0, "consecutive_successes": 0, "latest": None}
        with self.lock, self.db:
            self.db.execute("INSERT INTO services VALUES (?,?,?)",
                            (sid, json.dumps(config), json.dumps(state)))
        return self.get_service(sid)

    def get_service(self, sid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
        defaults = {"http_probe": {"timeout_seconds": 5, "expected_status": None, "body_contains": ""},
                    "resource_rules": [], "maintenance_until": None, "maintenance_reason": ""}
        return {"id": row["id"], **defaults, **json.loads(row["config"]), **json.loads(row["state"])} if row else None

    def list_services(self) -> list[dict]:
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM services ORDER BY rowid")]
        return [self.get_service(sid) for sid in ids]

    def patch_service(self, sid: str, changes: dict) -> dict:
        with self.lock, self.db:
            row = self.db.execute("SELECT config FROM services WHERE id=?", (sid,)).fetchone()
            if not row:
                raise KeyError(sid)
            config = {**json.loads(row[0]), **changes}
            self.db.execute("UPDATE services SET config=? WHERE id=?", (json.dumps(config), sid))
            if "resource_rules" in changes:
                self.sync_resource_rules(sid, changes["resource_rules"], now())
        return self.get_service(sid)

    def reset_check_counts(self, sid, *, invalidate=False):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM services WHERE id=?", (sid,)).fetchone()
            if not row:
                raise KeyError(sid)
            state = {**json.loads(row[0]), "consecutive_failures": 0, "consecutive_successes": 0}
            if invalidate:
                state.update(health="unknown", last_check_at=None, latest=None, next_check_at=None)
            self.db.execute("UPDATE services SET state=? WHERE id=?", (json.dumps(state), sid))
            self.db.execute("DELETE FROM resource_states WHERE service_id=?", (sid,))

    def record_observation(self, sid: str, snapshot: dict) -> dict:
        stamp = now()
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM services WHERE id=?", (sid,)).fetchone()
            state = json.loads(row[0])
            healthy = bool(snapshot["healthy"])
            state.update(health="healthy" if healthy else "unhealthy", last_check_at=stamp,
                         latest=snapshot,
                         consecutive_failures=0 if healthy else state["consecutive_failures"] + 1,
                         consecutive_successes=state.get("consecutive_successes", 0) + 1 if healthy else 0)
            self.db.execute("UPDATE services SET state=? WHERE id=?", (json.dumps(state), sid))
            cursor = self.db.execute("INSERT INTO observations(service_id,created_at,document) VALUES(?,?,?)",
                            (sid, stamp, json.dumps(snapshot)))
            self.record_telemetry(cursor.lastrowid, sid, stamp, snapshot, maintenance_active(self.get_service(sid)))
            self.db.execute("DELETE FROM observations WHERE service_id=? AND seq NOT IN "
                            "(SELECT seq FROM observations WHERE service_id=? ORDER BY seq DESC LIMIT 200)", (sid, sid))
        return self.get_service(sid)

    def set_next_check(self, sid: str, stamp: str):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM services WHERE id=?", (sid,)).fetchone()
            if row:
                state = {**json.loads(row[0]), "next_check_at": stamp}
                self.db.execute("UPDATE services SET state=? WHERE id=?", (json.dumps(state), sid))

    def active_incident(self, sid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT document FROM incidents WHERE service_id=? AND status!='resolved'", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_incident(self, iid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT document FROM incidents WHERE id=?", (iid,)).fetchone()
        return json.loads(row[0]) if row else None

    def create_incident(self, service: dict, snapshot: dict) -> dict:
        stamp = now()
        incident = {"id": new_id(), "service_id": service["id"], "service_name": service["name"],
                    "title": snapshot.get("summary", "服务健康检查失败")[:200],
                    "severity": "critical" if not snapshot.get("reachable") else "warning",
                    "status": "investigating", "diagnosis": "正在收集诊断证据", "action": None,
                    "attempts": 0, "created_at": stamp, "updated_at": stamp,
                    "resolved_at": None, "resolution_kind": None, "last_action_at": None,
                    "diagnostic_source": "rules", "context_key": None, "stopped_by_user": False,
                    "evidence": {"initial": snapshot, "before_actions": [], "recovery": None}}
        with self.lock, self.db:
            self.db.execute("INSERT INTO incidents VALUES(?,?,?,?)",
                            (incident["id"], service["id"], incident["status"], json.dumps(incident)))
        self.event("detected", f"连续 {service['consecutive_failures']} 次检查失败，建立事故", service["id"], incident["id"])
        return incident

    def update_incident(self, iid: str, **changes) -> dict:
        with self.lock, self.db:
            row = self.db.execute("SELECT document FROM incidents WHERE id=?", (iid,)).fetchone()
            if not row:
                raise KeyError(iid)
            doc = {**json.loads(row[0]), **changes, "updated_at": now()}
            self.db.execute("UPDATE incidents SET status=?,document=? WHERE id=?",
                            (doc["status"], json.dumps(doc), iid))
        return doc

    def list_incidents(self, limit: int = 100) -> list[dict]:
        # Keep active incidents visible even when there are many historical ones.
        with self.lock:
            rows = self.db.execute("SELECT document FROM incidents ORDER BY (status='resolved'), rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_agent_run(self, iid: str, run: dict):
        """Persist a copied run increment; already-written evidence is immutable."""
        from .models import EvidenceRecord
        encoded = json.dumps(run, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 262144:
            raise ValueError("Agent run exceeds 256 KiB")
        incoming = json.loads(encoded)
        records = incoming.get("evidence_records", [])
        for record in records:
            EvidenceRecord.model_validate(record)
            if record["incident_id"] != iid or record["run_id"] != run["run_id"]:
                raise ValueError("Evidence ownership mismatch")
        with self.lock, self.db:
            incident = self.get_incident(iid)
            if not incident:
                raise KeyError(iid)
            if any(r["service_id"] != incident["service_id"] for r in records):
                raise ValueError("Evidence service mismatch")
            runs = incident.get("agent_runs", [])
            previous = next((r for r in runs if r["run_id"] == incoming["run_id"]), None)
            if previous:
                old = previous.get("evidence_records", [])
                if records[:len(old)] != old:
                    raise ValueError("Evidence records are immutable")
                runs = [incoming if r["run_id"] == incoming["run_id"] else r for r in runs]
            else:
                if len(runs) >= 3:
                    raise ValueError("Incident run budget exhausted")
                runs = [*runs, incoming]
            self.update_incident(iid, agent_runs=runs)

    def incident_counts(self) -> dict:
        with self.lock:
            return {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM incidents GROUP BY status")}

    def event(self, kind: str, message: str, sid: str | None = None, iid: str | None = None):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(id,incident_id,service_id,kind,message,created_at) VALUES(?,?,?,?,?,?)",
                            (new_id(), iid, sid, kind, message[:3000], now()))

    def events(self, iid: str | None = None, limit: int = 60) -> list[dict]:
        with self.lock:
            if iid:
                rows = self.db.execute("SELECT * FROM events WHERE incident_id=? ORDER BY seq DESC LIMIT ?", (iid, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [{k: r[k] for k in r.keys() if k != "seq"} for r in rows]

    def begin_action(self, incident: dict, action: str) -> str:
        """Atomically persist intent and attempt count before issuing a remote write."""
        aid, stamp = new_id(), now()
        with self.lock, self.db:
            current = self.get_incident(incident["id"])
            current.update(status="remediating", attempts=current["attempts"] + 1,
                           last_action_at=stamp, updated_at=stamp)
            self.db.execute("UPDATE incidents SET status=?,document=? WHERE id=?",
                            ("remediating", json.dumps(current), current["id"]))
            self.db.execute("INSERT INTO actions VALUES(?,?,?,?,?)",
                            (aid, current["id"], action, "running", json.dumps({"started_at": stamp})))
        return aid

    def finish_action(self, aid: str, result: dict):
        with self.lock, self.db:
            self.db.execute("UPDATE actions SET status=?,document=? WHERE id=?",
                            ("completed" if result.get("ok") else "failed", json.dumps({**result, "finished_at": now()}), aid))

    def has_confirmed_latest_action(self, iid: str) -> bool:
        """A persisted attempt is not evidence that an action actually succeeded."""
        with self.lock:
            row = self.db.execute(
                "SELECT status,document FROM actions WHERE incident_id=? ORDER BY rowid DESC LIMIT 1",
                (iid,),
            ).fetchone()
        if row is None or row["status"] != "completed":
            return False
        document = json.loads(row["document"])
        details = document.get("details", {})
        return document.get("ok") is True and isinstance(details, dict) and not details.get("outcome_unknown")

    def recover_interrupted(self):
        for incident in self.list_incidents(limit=100000):
            if incident["status"] in {"remediating", "investigating"}:
                self.update_incident(incident["id"], status="escalated",
                                     diagnosis="控制器曾在调查或执行中断开；请核对现场状态。已停止自动重放。")
                self.event("recovery", "重启恢复：保留事故和操作记录，未盲目重复外部动作", incident["service_id"], incident["id"])
        with self.lock, self.db:
            self.db.execute("UPDATE actions SET status='uncertain' WHERE status='running'")
