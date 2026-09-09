from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .connectors import ConnectorManager
from .engine import Conflict, Engine
from .models import DismissRequest, FaultRequest, ServiceCreate, ServicePatch
from .process_lock import ProcessLock
from .store import Store


def public_service(service):
    return {k: v for k, v in service.items() if k not in {"agent_token", "agent_token_env"}}


def seed_config(store, config_path):
    if not config_path:
        return
    with Path(config_path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict) or not isinstance(document.get("services", []), list):
        raise ValueError("配置需要顶层 services 列表")
    existing = {(s["connector"], s["target"], s.get("agent_service", "")) for s in store.list_services()}
    for item in document.get("services", []):
        data = dict(item)
        sid = data.pop("id", None)
        reference = data.pop("agent_token_env", None)
        if reference:
            data["agent_token"] = os.environ.get(reference, "")
            if not data["agent_token"]:
                raise ValueError(f"缺少主机令牌环境变量 {reference}")
        config = ServiceCreate.model_validate(data).model_dump()
        identity = (config["connector"], config["target"], config.get("agent_service", ""))
        if identity not in existing:
            store.add_service(config, sid)
            existing.add(identity)
        elif reference:
            for service in store.list_services():
                if (service["connector"], service["target"], service.get("agent_service", "")) == identity:
                    store.patch_service(service["id"], {"agent_token": data["agent_token"]})


def create_app(*, data_dir: Path | str = ".opssentinel", demo=False, api_token=None,
               config_path=None, schedule=True) -> FastAPI:
    directory = Path(data_dir).resolve()
    token = os.getenv("OPS_API_TOKEN", "") if api_token is None else api_token
    if token and len(token) < 24:
        raise ValueError("OPS_API_TOKEN 至少需要 24 个字符")

    @asynccontextmanager
    async def lifespan(application):
        lock = ProcessLock(directory / "controller.lock")
        lock.acquire()
        store = None
        engine = None
        try:
            store = Store(directory / "state.sqlite3")
            seed_config(store, config_path)
            connectors = ConnectorManager(directory, enable_demo=demo)
            engine = Engine(store, connectors)
            application.state.engine = engine
            application.state.store = store
            await engine.start(schedule=schedule)
            yield
        finally:
            if engine:
                await engine.close()
            if store:
                store.close()
            lock.release()

    application = FastAPI(title="OpsSentinel", version=__version__, lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    allowed_hosts += [h.strip() for h in os.getenv("OPS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @application.middleware("http")
    async def access_control(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if token:
                supplied = request.headers.get("authorization", "")
                if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                    return JSONResponse({"detail": "请输入控制器访问令牌"}, status_code=401)
            else:
                peer = request.client.host if request.client else ""
                try:
                    is_local = ipaddress.ip_address(peer).is_loopback
                except ValueError:
                    is_local = peer == "testclient"
                if not is_local:
                    return JSONResponse({"detail": "远程访问需要先配置 OPS_API_TOKEN"}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("x-opssentinel-request") != "dashboard":
                    return JSONResponse({"detail": "缺少同源操作标识"}, status_code=403)
                origin = request.headers.get("origin")
                if origin and urlsplit(origin).netloc != request.headers.get("host"):
                    return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default response can echo a whole request including agent_token.
        detail = "; ".join(".".join(map(str, e["loc"][1:])) + ": " + e["msg"] for e in exc.errors())
        return JSONResponse({"detail": detail}, status_code=422)

    @application.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "服务或事故不存在"}, status_code=404)

    @application.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__}

    @application.get("/api/state")
    async def state(request: Request):
        return request.app.state.engine.state()

    @application.get("/api/events")
    async def events(request: Request):
        async def stream():
            while not await request.is_disconnected():
                yield "event: state\ndata: " + json.dumps(request.app.state.engine.state(), ensure_ascii=False) + "\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @application.post("/api/services", status_code=201)
    async def add_service(body: ServiceCreate, request: Request):
        store = request.app.state.store
        if len(store.list_services()) >= 100:
            raise HTTPException(409, "首版单控制器最多接入 100 个服务")
        for service in store.list_services():
            if (service["connector"], service["target"], service.get("agent_service", "")) == (body.connector, body.target, body.agent_service):
                raise HTTPException(409, "这个服务已接入")
        service = store.add_service(body.model_dump())
        store.event("service_added", f"接入服务：{service['name']}", service["id"])
        return public_service(service)

    @application.patch("/api/services/{sid}")
    async def patch_service(sid: str, body: ServicePatch, request: Request):
        engine = request.app.state.engine
        async with engine.lock(sid):
            service = engine.store.get_service(sid)
            if not service:
                raise KeyError(sid)
            changes = body.model_dump(exclude_none=True)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
                if not changes["name"]:
                    raise HTTPException(422, "服务名称不能为空")
            if service["connector"] == "http" and changes.get("auto_actions"):
                raise HTTPException(422, "HTTP 连接器只提供监测")
            service = engine.store.patch_service(sid, changes)
            engine.store.event("policy_changed", "服务设置已更新：" + "、".join(changes), sid)
            return public_service(service)

    @application.post("/api/services/{sid}/scan")
    async def scan_service(sid: str, request: Request):
        engine = request.app.state.engine
        if not engine.store.get_service(sid):
            raise KeyError(sid)
        if engine.lock(sid).locked():
            raise Conflict("这个服务正在巡检或处置，请稍后查看结果")
        await engine.scan_service(sid, manual=True)
        return {"ok": True, "message": "本次巡检已完成"}

    @application.post("/api/incidents/{iid}/approve")
    async def approve(iid: str, request: Request):
        await request.app.state.engine.approve(iid)
        return {"ok": True, "message": "本次处置已处理，请查看验证状态"}

    @application.post("/api/incidents/{iid}/dismiss")
    async def dismiss(iid: str, body: DismissRequest, request: Request):
        await request.app.state.engine.dismiss(iid, body.reason)
        return {"ok": True, "message": "已停止这次事故的自动处置，健康监测继续"}

    @application.post("/api/demo/fault")
    async def fault(body: FaultRequest, request: Request):
        engine = request.app.state.engine
        if not engine.connectors.enable_demo:
            raise HTTPException(404, "没有启用隔离演练")
        if engine.lock("demo-service").locked():
            raise Conflict("演练服务正在巡检或恢复，请稍后注入")
        async with engine.lock("demo-service"):
            result = await engine.connectors.inject_fault(body.fault)
            engine.store.event("exercise", result["summary"], "demo-service")
            return result

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    return application


app = create_app(data_dir=os.getenv("OPS_DATA_DIR", ".opssentinel"),
                 demo=os.getenv("OPS_DEMO", "0") == "1", config_path=os.getenv("OPS_CONFIG"))
