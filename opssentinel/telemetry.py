from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone


METRIC_NAMES = {"latency_ms": "响应耗时", "cpu_percent": "主机 CPU 使用率",
                "memory_percent": "主机内存使用率", "disk_percent": "代理数据目录所在磁盘使用率"}
RETENTION_SECONDS = 7 * 24 * 3600


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def number(value, *, percent=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if value >= 0 and (not percent or value <= 100) else None


def maintenance_active(service, at=None):
    until = service.get("maintenance_until")
    if not until:
        return False
    return datetime.fromisoformat(until).timestamp() > (at if at is not None else datetime.now(timezone.utc).timestamp())


class TelemetryMixin:
    """Numerical history and resource warnings share the controller's transaction lock."""

    def init_telemetry(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS telemetry (
                observation_seq INTEGER PRIMARY KEY, service_id TEXT NOT NULL,
                ts REAL NOT NULL, healthy INTEGER NOT NULL, maintenance INTEGER NOT NULL,
                latency_ms REAL, cpu_percent REAL, memory_percent REAL, disk_percent REAL);
            CREATE INDEX IF NOT EXISTS telemetry_service_time ON telemetry(service_id, ts);
            CREATE INDEX IF NOT EXISTS telemetry_expiry ON telemetry(ts);
            CREATE TABLE IF NOT EXISTS resource_states (
                service_id TEXT NOT NULL, metric TEXT NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(service_id, metric));
            CREATE TABLE IF NOT EXISTS resource_alerts (
                id TEXT PRIMARY KEY, service_id TEXT NOT NULL, metric TEXT NOT NULL,
                status TEXT NOT NULL, document TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_firing_resource
                ON resource_alerts(service_id, metric) WHERE status='firing';
        """)
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM metadata WHERE key='v02_history_backfilled'").fetchone():
                for row in self.db.execute("SELECT * FROM observations").fetchall():
                    self.record_telemetry(row["seq"], row["service_id"], row["created_at"], json.loads(row["document"]), False)
                self.db.execute("INSERT INTO metadata VALUES('v02_history_backfilled','1')")

    def record_telemetry(self, seq, sid, stamp, snapshot, maintenance):
        metrics = snapshot.get("metrics", {})
        values = [number(snapshot.get("latency_ms"))]
        values.extend(number(metrics.get(key), percent=True) for key in ("cpu_percent", "memory_percent", "disk_percent"))
        ts = datetime.fromisoformat(stamp).timestamp()
        self.db.execute("INSERT OR IGNORE INTO telemetry VALUES(?,?,?,?,?,?,?,?,?)",
                        (seq, sid, ts, int(snapshot["healthy"]), int(maintenance), *values))
        self.db.execute("DELETE FROM telemetry WHERE ts<?", (ts - RETENTION_SECONDS,))

    def history(self, sid, hours=24, *, at=None):
        end = at if at is not None else datetime.now(timezone.utc).timestamp()
        since, bucket = end - hours * 3600, hours * 3600 // 240
        with self.lock:
            rows = self.db.execute("""SELECT MIN(239,CAST((ts-?)/? AS INTEGER)) AS bucket,
                COUNT(*) AS samples, SUM(healthy) AS healthy_samples, SUM(maintenance) AS maintenance_samples,
                AVG(latency_ms) AS latency_ms, AVG(cpu_percent) AS cpu_percent,
                AVG(memory_percent) AS memory_percent, AVG(disk_percent) AS disk_percent,
                MAX(latency_ms) AS latency_max_ms, MAX(cpu_percent) AS cpu_max_percent,
                MAX(memory_percent) AS memory_max_percent, MAX(disk_percent) AS disk_max_percent
                FROM telemetry WHERE service_id=? AND ts>=? AND ts<=? GROUP BY bucket ORDER BY bucket""",
                (since, bucket, sid, since, end)).fetchall()
            stats = self.db.execute("""SELECT COUNT(*) AS sample_count, SUM(healthy) AS healthy_count,
                AVG(latency_ms) AS avg_latency_ms, MIN(ts) AS first_at, MAX(ts) AS last_at
                FROM telemetry WHERE service_id=? AND ts>=? AND ts<=?""", (sid, since, end)).fetchone()
            latencies = [r[0] for r in self.db.execute("SELECT latency_ms FROM telemetry WHERE service_id=? AND ts>=? AND ts<=? AND latency_ms IS NOT NULL ORDER BY latency_ms", (sid, since, end))]
        points = [{"at": iso(since + row["bucket"] * bucket), **{key: row[key] for key in row.keys() if key != "bucket"}} for row in rows]
        count = stats["sample_count"]
        summary = {"sample_count": count, "healthy_count": stats["healthy_count"] or 0,
                   "success_rate": round((stats["healthy_count"] or 0) * 100 / count, 3) if count else None,
                   "avg_latency_ms": stats["avg_latency_ms"],
                   "p95_latency_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
                   "first_at": iso(stats["first_at"]) if count else None, "last_at": iso(stats["last_at"]) if count else None}
        return {"service_id": sid, "hours": hours, "bucket_seconds": bucket, "points": points, "summary": summary}

    def resource_alerts(self, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT document FROM resource_alerts ORDER BY (status='resolved'), rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def firing_resource_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM resource_alerts WHERE status='firing'").fetchone()[0]

    def _save_resource_alert(self, alert):
        self.db.execute("UPDATE resource_alerts SET status=?,document=? WHERE id=?",
                        (alert["status"], json.dumps(alert), alert["id"]))

    def acknowledge_resource_alert(self, aid, note, stamp):
        with self.lock, self.db:
            row = self.db.execute("SELECT document FROM resource_alerts WHERE id=?", (aid,)).fetchone()
            if not row:
                raise KeyError(aid)
            alert = json.loads(row[0])
            if alert["acknowledged_at"]:
                return alert
            alert.update(acknowledged_at=stamp, acknowledged_note=note, updated_at=stamp)
            self._save_resource_alert(alert)
            self.event("resource_acknowledged", "已确认资源预警：" + METRIC_NAMES[alert["metric"]], alert["service_id"])
            return alert

    def sync_resource_rules(self, sid, rules, stamp):
        """Changing policy retires its previous warning without claiming recovery."""
        expected = {r["metric"]: r for r in rules}
        with self.lock, self.db:
            for row in self.db.execute("SELECT document FROM resource_alerts WHERE service_id=? AND status='firing'", (sid,)).fetchall():
                alert = json.loads(row[0])
                rule = expected.get(alert["metric"])
                if not rule or any(rule[k] != alert[k] for k in ("above", "recover_below", "for_checks")):
                    alert.update(status="resolved", resolved_at=stamp, updated_at=stamp, resolution_reason="rule_changed")
                    self._save_resource_alert(alert)
                    self.event("resource_rule_changed", "预警规则已更改，归档旧预警（不计为指标恢复）", sid)
            self.db.execute("DELETE FROM resource_states WHERE service_id=?", (sid,))

    def evaluate_resources(self, service, snapshot, stamp, *, suppress_new=False):
        sid = service["id"]
        with self.lock, self.db:
            for rule in service.get("resource_rules", []):
                metric = rule["metric"]
                value = number(snapshot.get("latency_ms") if metric == "latency_ms" else snapshot.get("metrics", {}).get(metric), percent=metric != "latency_ms")
                row = self.db.execute("SELECT document FROM resource_states WHERE service_id=? AND metric=?", (sid, metric)).fetchone()
                state = json.loads(row[0]) if row else {"above_count": 0, "below_count": 0}
                state["above_count"] = state["above_count"] + 1 if value is not None and value > rule["above"] and not suppress_new else 0
                state["below_count"] = state["below_count"] + 1 if value is not None and value < rule["recover_below"] else 0
                self.db.execute("INSERT OR REPLACE INTO resource_states VALUES(?,?,?)", (sid, metric, json.dumps(state)))
                row = self.db.execute("SELECT document FROM resource_alerts WHERE service_id=? AND metric=? AND status='firing'", (sid, metric)).fetchone()
                alert = json.loads(row[0]) if row else None
                if alert:
                    alert.update(value=value, updated_at=stamp)
                    if value is not None:
                        alert["peak_value"] = max(alert["peak_value"], value)
                    if state["below_count"] >= rule["for_checks"]:
                        alert.update(status="resolved", resolved_at=stamp, resolution_reason="recovered")
                        self.event("resource_resolved", METRIC_NAMES[metric] + "连续回落至恢复阈值以下", sid)
                    self._save_resource_alert(alert)
                elif state["above_count"] >= rule["for_checks"] and not suppress_new:
                    alert = {"id": uuid.uuid4().hex, "service_id": sid, "service_name": service["name"],
                             **rule, "status": "firing", "value": value, "peak_value": value,
                             "created_at": stamp, "updated_at": stamp, "resolved_at": None,
                             "acknowledged_at": None, "acknowledged_note": "", "resolution_reason": None}
                    self.db.execute("INSERT INTO resource_alerts VALUES(?,?,?,?,?)", (alert["id"], sid, metric, "firing", json.dumps(alert)))
                    self.event("resource_firing", f"{METRIC_NAMES[metric]}连续 {rule['for_checks']} 次超过 {rule['above']}，当前值 {value}；仅预警，不自动重启", sid)
