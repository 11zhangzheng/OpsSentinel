"""A real localhost subprocess used solely for the isolated recovery exercise."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path

import psutil

from .connector_helpers import atomic_json

LOG_LIMIT = 131072


def status(data_dir: Path) -> dict:
    try:
        release = json.loads((data_dir / "release.json").read_text(encoding="utf-8"))
        config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
        image_ok = release.get("current_image") == "exercise:v1"
        config_ok = config.get("mode") == "valid"
        log_bytes = (data_dir / "application.log").stat().st_size
        logs_ok = log_bytes <= LOG_LIMIT
        healthy = image_ok and config_ok and logs_ok
        suggested = "rollback_release" if not image_ok else "restore_config" if not config_ok else "rotate_logs" if not logs_ok else None
        return {"healthy": healthy, "summary": "Isolated exercise service healthy" if healthy else "Isolated exercise service has an injected fault",
                "checks": [{"name": "release", "ok": image_ok, "detail": str(release.get("current_image"))},
                           {"name": "business_config", "ok": config_ok, "detail": "Configuration valid" if config_ok else "Invalid exercise configuration"},
                           {"name": "managed_log", "ok": logs_ok, "detail": f"{log_bytes} / {LOG_LIMIT} bytes"}],
                "metrics": {"log_bytes": log_bytes},
                "facts": {**release, "suggested_action": suggested, "exercise": True}}
    except (OSError, ValueError):
        return {"healthy": False, "summary": "Exercise configuration unreadable", "checks": [],
                "metrics": {}, "facts": {"exercise": True, "suggested_action": "restore_config"}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created", type=float, required=True)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/health", "/business"}:
                self.send_error(404)
                return
            snapshot = status(args.data_dir)
            payload = json.dumps(snapshot).encode()
            self.send_response(200 if snapshot["healthy"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *values: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    atomic_json(args.port_file, {"port": server.server_address[1], "pid": os.getpid()})
    try:
        while True:
            try:
                parent = psutil.Process(args.parent_pid)
                if abs(parent.create_time() - args.parent_created) > 0.01 or not parent.is_running():
                    break
            except psutil.Error:
                break
            server.handle_request()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
