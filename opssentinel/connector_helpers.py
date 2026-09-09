"""Bounded IO primitives shared by the controller and allowlisted host agent."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit
import uuid

import httpx

MAX_BODY = 65536


class ResponseTooLargeError(ValueError):
    """A received HTTP response exceeded the bounded probe payload."""

    def __init__(self, status_code: int):
        super().__init__("HTTP response exceeds the 64 KiB limit")
        self.status_code = status_code


def validate_http_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("HTTP target must be a nonempty URL without whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only explicit http:// or https:// targets are supported")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Credentials and fragments are not allowed in targets")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid target port")
    # Explicitly configured private/loopback hosts are intended monitoring targets.
    # Cloud metadata and link-local targets are not application health endpoints.
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() in {"metadata.google.internal", "metadata.goog"}:
            raise ValueError("Cloud metadata endpoints are not supported")
    else:
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError("Link-local, multicast and unspecified targets are not supported")
    return value


def validate_agent_base(value: str) -> str:
    validate_http_url(value)
    if urlsplit(value).query:
        raise ValueError("Host agent base URLs cannot contain a query string")
    return value.rstrip("/")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def tail_lines(path: Path, limit: int = 4096) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read(limit).decode("utf-8", errors="replace").splitlines()[-30:]
    except OSError:
        return []


async def http_request(url: str, *, method: str = "GET", token: str | None = None,
                       payload: dict | None = None, timeout: float = 8) -> tuple[int, bytes]:
    validate_http_url(url)
    headers = {"Authorization": "Bearer " + token} if token else {}

    async def request() -> tuple[int, bytes]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 3)),
                                     follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, url, headers=headers, json=payload) as response:
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    chunks.extend(chunk[:MAX_BODY + 1 - len(chunks)])
                    if len(chunks) > MAX_BODY:
                        raise ResponseTooLargeError(response.status_code)
                return response.status_code, bytes(chunks)

    # Include connection, response headers and all body reads in one deadline.
    return await asyncio.wait_for(request(), timeout=timeout)


def bounded_command(args: list[str], *, timeout: float = 15, limit: int = 65536) -> dict:
    """Run a fixed argv, drain output into a bounded tail, kill only our CLI child."""
    process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = bytearray()

    def drain() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > limit:
                    del output[:-limit]
        finally:
            process.stdout.close()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=5)
    reader.join(timeout=2)
    return {"ok": process.returncode == 0 and not timed_out,
            "returncode": process.returncode, "timed_out": timed_out,
            "output": bytes(output).decode("utf-8", errors="replace")}
