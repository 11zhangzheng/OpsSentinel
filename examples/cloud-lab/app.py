"""Disposable, stateless HTTP target for the first real-server recovery drill."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTED = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in {"/health", "/ready", "/"}:
            code = 200
            payload = {"ok": True, "service": "opssentinel-test-api",
                       "uptime_seconds": round(time.monotonic() - STARTED, 2)}
        else:
            code, payload = 404, {"error": "not_found"}
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
