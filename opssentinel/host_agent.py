"""An authenticated Linux host agent with administrator-defined service allowlists.

No endpoint accepts a command, a file path, or an image to deploy. Successful
actions mean command completion; the controller must still verify recovery.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import tempfile
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
import psutil
import uvicorn
import yaml

from .connector_helpers import bounded_command, http_request, validate_http_url

ACTION_NAMES = {"restart_service", "rollback_release", "restore_config", "rotate_logs"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-fA-F]{64}$")
CONFIG_LIMIT = 1048576


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["restart_service", "rollback_release", "restore_config", "rotate_logs"]
    operation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]+$")


def result(ok: bool, summary: str, **details: object) -> dict:
    return {"ok": ok, "summary": summary, "details": details}


def config_snapshot(path_value: str) -> tuple[bytes, os.stat_result]:
    """Read one fixed regular config, rejecting symlinks and oversized input."""
    path = Path(path_value)
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Config paths must be absolute and contain no symlinks")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > CONFIG_LIMIT or before.st_nlink != 1:
        raise ValueError("Config must be a single-link regular file of at most 1 MiB")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Config identity changed during inspection")
        data = handle.read(CONFIG_LIMIT + 1)
        if len(data) > CONFIG_LIMIT or len(data) != before.st_size:
            raise ValueError("Config size changed or exceeds 1 MiB")
    return data, before


def replace_config(path: Path, payload: bytes, original_info: os.stat_result) -> None:
    """Atomic replacement in the same directory, preserving config permissions."""
    if len(payload) > CONFIG_LIMIT:
        raise ValueError("Config exceeds 1 MiB")
    descriptor, temporary = tempfile.mkstemp(prefix=".opssentinel-config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), stat.S_IMODE(original_info.st_mode))
            if hasattr(os, "fchown"):
                os.fchown(handle.fileno(), original_info.st_uid, original_info.st_gid)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_config(path: Path) -> dict:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ValueError("Agent config requires a services mapping")
    services = {}
    identities = set()
    managed_paths = set()
    for identifier, raw in data["services"].items():
        if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) or not isinstance(raw, dict):
            raise ValueError("Each service requires a simple fixed ID and settings mapping")
        service = dict(raw)
        compose = Path(str(service.get("compose_file", "")))
        if not compose.is_absolute() or not compose.is_file():
            raise ValueError(f"Service {identifier} requires an existing absolute compose_file")
        name = service.get("compose_service", "")
        if not isinstance(name, str) or not ID_PATTERN.fullmatch(name):
            raise ValueError(f"Service {identifier} has an invalid compose_service")
        service["compose_file"] = str(compose.resolve())
        identity = (service["compose_file"], name)
        if identity in identities:
            raise ValueError("Duplicate aliases for one Compose service are not supported")
        identities.add(identity)
        service["health_url"] = validate_http_url(service.get("health_url", ""))
        if service.get("business_url"):
            service["business_url"] = validate_http_url(service["business_url"])
        allowed = service.get("allowed_actions", service.get("auto_actions", []))
        if not isinstance(allowed, list) or any(action not in ACTION_NAMES for action in allowed):
            raise ValueError(f"Service {identifier} has invalid allowed_actions")
        service["allowed_actions"] = allowed
        service["max_log_bytes"] = int(service.get("max_log_bytes", 10485760))
        service["retained_log_count"] = int(service.get("retained_log_count", 2))
        if not 1024 <= service["max_log_bytes"] <= 52428800 or not 1 <= service["retained_log_count"] <= 10:
            raise ValueError("Managed log thresholds must be 1 KiB–50 MiB; retain 1–10 archives")
        if service.get("managed_log_path"):
            log = Path(service["managed_log_path"])
            if not log.is_absolute():
                raise ValueError("managed_log_path must be absolute")
            if log.is_symlink():
                raise ValueError("Symlink managed logs are not supported")
            service["managed_log_path"] = str(log)
        restore_keys = ("managed_config_path", "known_good_config_path", "known_good_config_sha256")
        if any(service.get(key) for key in restore_keys):
            if not all(service.get(key) for key in restore_keys):
                raise ValueError("Config restoration requires both absolute paths and the known-good SHA-256")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(service["known_good_config_sha256"])):
                raise ValueError("known_good_config_sha256 must be a 64-character SHA-256")
            for key in restore_keys[:2]:
                config_snapshot(str(service[key]))
                service[key] = str(Path(service[key]).absolute())
            if service["managed_config_path"] == service["known_good_config_path"]:
                raise ValueError("Managed config and known-good backup must be different files")
            if service["managed_config_path"] in managed_paths:
                raise ValueError("A managed config cannot be written by multiple service IDs")
            managed_paths.add(service["managed_config_path"])
        service["rollback_window_seconds"] = int(service.get("rollback_window_seconds", 600))
        if not 30 <= service["rollback_window_seconds"] <= 3600:
            raise ValueError("rollback_window_seconds must be between 30 and 3600")
        services[identifier] = service
    state_dir = Path(data.get("state_dir", path.parent / ".ops-agent"))
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    return {"services": services, "state_dir": state_dir.resolve()}


class HostRuntime:
    def __init__(self, config: dict):
        self.services = config["services"]
        self.state_dir = Path(config["state_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.state_dir / "operations.sqlite3"
        self._locks = {identifier: threading.Lock() for identifier in self.services}
        with self._db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, service_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_started_action_per_service ON operations(service_id) WHERE status='started'")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _service(self, identifier: str) -> dict:
        if identifier not in self.services:
            raise HTTPException(404, "Unknown configured service")
        return self.services[identifier]

    @staticmethod
    def _compose(service: dict) -> list[str]:
        return ["docker", "compose", "-f", service["compose_file"]]

    def _container(self, service: dict) -> dict:
        try:
            found = bounded_command(self._compose(service) + ["ps", "-a", "-q", service["compose_service"]], timeout=5, limit=4096)
            if not found["ok"]:
                return {"known": False, "running": False, "summary": "Docker Compose inspection failed"}
            ids = found["output"].strip().splitlines()
            if not ids:
                return {"known": True, "running": False, "exists": False, "summary": "Configured container is absent"}
            if len(ids) != 1 or not re.fullmatch(r"[0-9a-fA-F]{12,64}", ids[0]):
                return {"known": False, "running": False, "summary": "Exactly one configured service container is required"}
            inspected = bounded_command(["docker", "inspect", "--format", "{{json .}}", ids[0]], timeout=5, limit=65536)
            if not inspected["ok"]:
                return {"known": False, "running": False, "summary": "Container inspection failed"}
            container = json.loads(inspected["output"])
            labels = container.get("Config", {}).get("Labels", {}) or {}
            if labels.get("com.docker.compose.service") != service["compose_service"]:
                return {"known": False, "running": False, "summary": "Container service identity mismatch"}
            state = container.get("State", {})
            return {"known": True, "exists": True, "id": ids[0], "running": state.get("Running") is True,
                    "docker_health": state.get("Health", {}).get("Status"),
                    "current_image": container.get("Config", {}).get("Image"),
                    "created_at": container.get("Created"),
                    "summary": "Container running" if state.get("Running") else "Container is stopped"}
        except (OSError, ValueError, TypeError):
            return {"known": False, "running": False, "summary": "Docker inspection unavailable"}

    async def _probe(self, name: str, url: str) -> dict:
        try:
            code, _ = await http_request(url, timeout=5)
            return {"name": name, "ok": 200 <= code < 300, "detail": f"HTTP {code}", "reachable": True}
        except Exception as exc:
            return {"name": name, "ok": False, "detail": f"Probe failed ({type(exc).__name__})", "reachable": False}

    def _config_evidence(self, service: dict) -> dict:
        keys = ("managed_config_path", "known_good_config_path", "known_good_config_sha256")
        if not all(service.get(key) for key in keys):
            return {"eligible": False, "reason": "No explicit known-good config contract is configured"}
        try:
            current, _ = config_snapshot(service["managed_config_path"])
            known, _ = config_snapshot(service["known_good_config_path"])
            digest = hashlib.sha256(known).hexdigest()
            valid = hmac.compare_digest(digest, service["known_good_config_sha256"].lower())
            differs = current != known
            return {"eligible": valid and differs, "known_good_hash_matches": valid, "config_differs": differs,
                    "current_config_sha256": hashlib.sha256(current).hexdigest(), "known_good_config_sha256": digest,
                    "reason": "Verified known-good config differs" if valid and differs else "Backup hash mismatch or config already matches"}
        except (OSError, ValueError) as exc:
            return {"eligible": False, "reason": f"Config precondition failed ({type(exc).__name__})"}

    def _rollback_evidence(self, service: dict, container: dict) -> dict:
        previous = service.get("previous_image", "")
        expected = service.get("expected_current_image", "")
        valid_images = isinstance(previous, str) and isinstance(expected, str) and IMAGE_PATTERN.fullmatch(previous) and IMAGE_PATTERN.fullmatch(expected)
        if not valid_images or previous == expected or service.get("rollback_data_compatible") is not True or not service.get("business_url"):
            return {"eligible": False, "reason": "Rollback requires distinct expected/previous immutable digests, data compatibility and a business probe"}
        if not container.get("known") or not container.get("exists") or container.get("current_image") != expected:
            return {"eligible": False, "reason": "Current container does not match the expected release"}
        try:
            created = datetime.fromisoformat(container["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError("Missing creation timezone")
            age = (datetime.now(timezone.utc) - created).total_seconds()
            eligible = 0 <= age <= service["rollback_window_seconds"]
            return {"eligible": eligible, "release_age_seconds": round(age, 1),
                    "reason": "Expected release is within rollback window" if eligible else "Release is outside rollback window"}
        except (ValueError, TypeError, KeyError):
            return {"eligible": False, "reason": "Container creation time is unavailable"}

    async def observe(self, identifier: str) -> dict:
        service = self._service(identifier)
        started = time.monotonic()
        calls = [asyncio.to_thread(self._container, service), self._probe("health_http", service["health_url"])]
        if service.get("business_url"):
            calls.append(self._probe("business_http", service["business_url"]))
        values = await asyncio.gather(*calls)
        container, *probes = values
        checks = [{"name": "container", "ok": container.get("known") is True and container.get("running") is True,
                   "detail": container["summary"]}, *[{key: item[key] for key in ("name", "ok", "detail")} for item in probes]]
        if container.get("docker_health"):
            checks.append({"name": "docker_health", "ok": container["docker_health"] == "healthy", "detail": container["docker_health"]})
        metrics = {"cpu_percent": psutil.cpu_percent(), "memory_percent": psutil.virtual_memory().percent,
                   "disk_percent": psutil.disk_usage(str(self.state_dir)).percent}
        if service.get("managed_log_path"):
            log = Path(service["managed_log_path"])
            try:
                info = log.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("not a regular log")
                metrics["log_bytes"] = info.st_size
                checks.append({"name": "managed_log", "ok": info.st_size <= service["max_log_bytes"],
                               "detail": f"{info.st_size} / {service['max_log_bytes']} bytes"})
            except (OSError, ValueError):
                checks.append({"name": "managed_log", "ok": False, "detail": "Configured managed log unavailable"})
        logs = []
        if container.get("known") and container.get("exists"):
            try:
                log_result = await asyncio.to_thread(bounded_command,
                    self._compose(service) + ["logs", "--no-color", "--tail", "30", service["compose_service"]], timeout=3, limit=8192)
                if log_result["ok"]:
                    logs = log_result["output"].splitlines()[-30:]
            except OSError:
                pass
        healthy = all(check["ok"] for check in checks)
        facts = {"current_image": container.get("current_image"), "previous_image": service.get("previous_image"),
                 "expected_current_image": service.get("expected_current_image"), "container_created_at": container.get("created_at"),
                 "docker_state_known": container.get("known"), "allowed_actions": service["allowed_actions"],
                 "rollback_data_compatible": service.get("rollback_data_compatible") is True}
        config_evidence = await asyncio.to_thread(self._config_evidence, service)
        rollback_evidence = self._rollback_evidence(service, container)
        facts["config_restore"] = config_evidence
        facts["release_rollback"] = rollback_evidence
        if "current_config_sha256" in config_evidence:
            facts["config_hash"] = config_evidence["current_config_sha256"]
            facts["known_good_config_hash"] = config_evidence["known_good_config_sha256"]
        # Suggest only a directly evidenced mitigation; do not guess rollback from arbitrary log prose.
        if not healthy:
            if config_evidence["eligible"] and any(not probe["ok"] for probe in probes) and "restore_config" in service["allowed_actions"]:
                facts["suggested_action"] = "restore_config"
            elif rollback_evidence["eligible"] and any(probe["name"] == "business_http" and not probe["ok"] for probe in probes) and "rollback_release" in service["allowed_actions"]:
                facts["suggested_action"] = "rollback_release"
            elif metrics.get("log_bytes", 0) > service["max_log_bytes"]:
                facts["suggested_action"] = "rotate_logs"
            elif container.get("known") and container.get("exists") and (not container.get("running") or container.get("docker_health") == "unhealthy"):
                facts["suggested_action"] = "restart_service"
        return {"healthy": healthy, "reachable": any(probe["reachable"] for probe in probes),
                "summary": "Configured host service healthy" if healthy else "Configured host service failed health checks",
                "latency_ms": round((time.monotonic() - started) * 1000, 1), "checks": checks,
                "metrics": metrics, "logs": logs, "facts": facts}

    def _existing(self, operation: str, identifier: str, action: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT service_id,action,status,result FROM operations WHERE operation_id=?", (operation,)).fetchone()
        if row is None:
            return None
        if row[0] != identifier or row[1] != action:
            raise HTTPException(409, "Operation ID already belongs to a different action")
        if row[2] == "completed":
            value = json.loads(row[3])
            value["details"]["replayed"] = True
            return value
        return result(False, "Operation is in progress or its outcome is unknown; inspect before issuing another operation",
                      operation_id=operation, outcome_unknown=True)

    def execute(self, identifier: str, action: str, operation: str) -> dict:
        service = self._service(identifier)
        old = self._existing(operation, identifier, action)
        if old is not None:
            return old
        if action not in service["allowed_actions"]:
            return result(False, "Action is not in this host service's fixed allowlist", operation_id=operation)
        if not self._locks[identifier].acquire(blocking=False):
            return result(False, "Another action is already running for this service", operation_id=operation)
        try:
            # Re-check after the lock: a previous same-ID action may have just finished.
            old = self._existing(operation, identifier, action)
            if old is not None:
                return old
            try:
                with self._db() as conn:
                    conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", (operation, identifier, action, "started", None, time.time()))
            except sqlite3.IntegrityError:
                old = self._existing(operation, identifier, action)
                if old is not None:
                    return old
                return result(False, "Service already has an in-progress or interrupted action; inspect before continuing",
                              operation_id=operation, service_busy=True)
            try:
                outcome = self._perform(service, action, operation)
            except Exception as exc:
                outcome = result(False, f"Action failed ({type(exc).__name__}); inspect service state", outcome_unknown=True)
            outcome.setdefault("details", {})["operation_id"] = operation
            # A timed-out Docker client does not establish whether the daemon
            # completed a mutation. Retain its service lock for manual inspection.
            status = "started" if outcome["details"].get("outcome_unknown") else "completed"
            with self._db() as conn:
                conn.execute("UPDATE operations SET status=?,result=? WHERE operation_id=?", (status, json.dumps(outcome), operation))
            return outcome
        finally:
            self._locks[identifier].release()

    def _perform(self, service: dict, action: str, operation: str) -> dict:
        if action == "restore_config":
            return self._restore_config(service, operation)
        if action == "rotate_logs":
            return self._rotate(service)
        container = self._container(service)
        if not container.get("known"):
            return result(False, "Refused: cannot establish configured container identity and state")
        if action == "restart_service":
            if not container.get("exists"):
                return result(False, "Refused: configured container is absent; restart does not recreate services")
            probes = asyncio.run(self._probe("health", service["health_url"]))
            business_ok = True
            if service.get("business_url"):
                business_ok = asyncio.run(self._probe("business", service["business_url"]))["ok"]
            if container.get("running") and container.get("docker_health") != "unhealthy" and probes["ok"] and business_ok:
                return result(False, "Refused: service is already healthy; restart precondition is not met")
            command = self._compose(service) + ["restart", "--no-deps", "--timeout", "10", service["compose_service"]]
        elif action == "rollback_release":
            evidence = self._rollback_evidence(service, container)
            if not evidence["eligible"]:
                return result(False, "Refused: " + evidence["reason"])
            previous = service["previous_image"]
            if asyncio.run(self._probe("business", service["business_url"]))["ok"]:
                return result(False, "Refused: business probe is healthy; rollback precondition is not met")
            available = bounded_command(["docker", "image", "inspect", "--format", "{{.Id}}", previous], timeout=5, limit=4096)
            if not available["ok"]:
                return result(False, "Refused: previous immutable image is not locally available; no automatic pull")
            refreshed = self._rollback_evidence(service, self._container(service))
            if not refreshed["eligible"]:
                return result(False, "Refused: rollback context changed before mutation; " + refreshed["reason"])
            # Persist this override as evidence; do not modify the administrator's compose file.
            digest = hashlib.sha256(operation.encode()).hexdigest()[:24]
            override = self.state_dir / ("rollback-" + digest + ".yaml")
            override.write_text(yaml.safe_dump({"services": {service["compose_service"]: {"image": previous}}}), encoding="utf-8")
            command = self._compose(service) + ["-f", str(override), "up", "-d", "--no-deps", "--no-build", "--pull", "never", service["compose_service"]]
        else:
            return result(False, "Unsupported action")
        execution = bounded_command(command, timeout=45, limit=8192)
        if not execution["ok"]:
            return result(False, "Compose action failed or timed out; inspect before retrying",
                          timed_out=execution["timed_out"], returncode=execution["returncode"], outcome_unknown=True)
        return result(True, "Compose action completed; repeated health/business probes must verify recovery",
                      verification_required=True, mitigation=True)

    def _restore_config(self, service: dict, operation: str) -> dict:
        evidence = self._config_evidence(service)
        if not evidence["eligible"]:
            return result(False, "Refused: " + evidence["reason"])
        container = self._container(service)
        if not container.get("known") or not container.get("exists"):
            return result(False, "Refused: cannot establish existing configured container identity")
        healthy = asyncio.run(self._probe("health", service["health_url"]))["ok"]
        if service.get("business_url"):
            healthy = asyncio.run(self._probe("business", service["business_url"]))["ok"] and healthy
        if healthy:
            return result(False, "Refused: fresh probes are healthy; config restore precondition is not met")
        refreshed = self._container(service)
        if not refreshed.get("known") or not refreshed.get("exists") or refreshed.get("id") != container.get("id"):
            return result(False, "Refused: container identity changed during config recovery checks")
        path = Path(service["managed_config_path"])
        original, info = config_snapshot(str(path))
        known_good, _ = config_snapshot(service["known_good_config_path"])
        if original == known_good or hashlib.sha256(known_good).hexdigest() != service["known_good_config_sha256"].lower():
            return result(False, "Refused: config contract changed during precondition checks")
        archive_name = "config-" + hashlib.sha256(operation.encode()).hexdigest()[:24] + ".previous"
        archive = self.state_dir / archive_name
        descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        replaced = False
        deployment_started = False
        try:
            # Recheck immediately before the atomic write, avoiding clobbering an
            # administrator edit made while probes or archive IO were running.
            current, _ = config_snapshot(str(path))
            if current != original:
                return result(False, "Refused: managed config changed before replacement", archive=archive_name)
            replace_config(path, known_good, info)
            replaced = True
            validated = bounded_command(self._compose(service) + ["config", "--quiet"], timeout=10, limit=4096)
            if not validated["ok"]:
                raise RuntimeError("Fixed Compose configuration validation failed")
            deployment_started = True
            deployed = bounded_command(self._compose(service) + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", service["compose_service"]], timeout=40, limit=8192)
            if not deployed["ok"]:
                raise RuntimeError("Configured service recreation failed or timed out")
            return result(True, "Known-good config restored and configured service recreated; fresh probes must verify recovery",
                          archive=archive_name, verification_required=True, mitigation=True)
        except Exception as exc:
            restored_original = False
            if replaced:
                try:
                    current, _ = config_snapshot(str(path))
                    if current == known_good:
                        replace_config(path, original, info)
                        restored_original = True
                except (OSError, ValueError):
                    pass
            return result(False, f"Config restoration failed ({type(exc).__name__}); inspect service before retrying",
                          archive=archive_name, original_file_restored=restored_original,
                          outcome_unknown=deployment_started or not restored_original)

    def _rotate(self, service: dict) -> dict:
        if not service.get("managed_log_path"):
            return result(False, "Refused: no dedicated managed_log_path is configured")
        path = Path(service["managed_log_path"])
        # Copy-truncate preserves the writer's inode. Only this exact administrator-
        # allowlisted log and its numbered archives are touched; never Docker internals.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= service["max_log_bytes"]:
            return result(False, "Refused: managed log is not regular or below the rotation threshold")
        if info.st_size > 52428800:
            return result(False, "Refused: managed log exceeds the 50 MiB bounded rotation limit")
        archives = [path.with_name(path.name + f".{number}") for number in range(1, service["retained_log_count"] + 1)]
        if any(candidate.is_symlink() or (candidate.exists() and not candidate.is_file()) for candidate in archives):
            return result(False, "Refused: archive path is not a regular owned log archive")
        for source, destination in zip(reversed(archives[:-1]), reversed(archives[1:])):
            if source.exists():
                os.replace(source, destination)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev:
                return result(False, "Refused: managed log changed during precondition checks")
            with os.fdopen(descriptor, "r+b", closefd=False) as original:
                archive_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                archive_fd = os.open(archives[0], archive_flags, 0o600)
                with os.fdopen(archive_fd, "wb") as backup:
                    remaining = info.st_size
                    while remaining:
                        chunk = original.read(min(65536, remaining))
                        if not chunk:
                            break
                        backup.write(chunk)
                        remaining -= len(chunk)
                    backup.flush()
                    os.fsync(backup.fileno())
                # Refuse truncation if the writer grew the file while copying.
                if os.fstat(descriptor).st_size != info.st_size:
                    return result(False, "Log changed while copying; preserved source and refused truncation")
                original.truncate(0)
                original.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return result(True, "Dedicated managed log rotated; verify service health", verification_required=True,
                      bytes_rotated=info.st_size, copytruncate=True)


def create_app(config_path: str | Path, token: str | None = None) -> FastAPI:
    credential = token if token is not None else os.environ.get("OPS_AGENT_TOKEN", "")
    if len(credential) < 24:
        raise ValueError("OPS_AGENT_TOKEN must contain at least 24 characters")
    runtime = HostRuntime(load_config(Path(config_path)))
    app = FastAPI(title="OpsSentinel Host Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    def authorize(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization or ""
        expected = "Bearer " + credential
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(401, "Host agent authentication required", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "service": "OpsSentinel host agent"}

    @app.get("/v1/services/{identifier}/observe", dependencies=[Depends(authorize)])
    async def observe(identifier: str) -> dict:
        return await runtime.observe(identifier)

    @app.post("/v1/services/{identifier}/actions", dependencies=[Depends(authorize)])
    async def action(identifier: str, payload: ActionRequest) -> dict:
        return await asyncio.to_thread(runtime.execute, identifier, payload.action, payload.operation_id)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Allowlisted Linux Docker Compose host agent")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    arguments = parser.parse_args()
    if os.name != "posix":
        parser.error("The production host agent requires Linux; use --demo on the controller for the local exercise")
    uvicorn.run(create_app(arguments.config), host=arguments.host, port=arguments.port, access_log=False)


if __name__ == "__main__":
    main()
