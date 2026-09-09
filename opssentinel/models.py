from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Action = Literal["restart_service", "rollback_release", "restore_config", "rotate_logs"]
ACTIONS = {"restart_service", "rollback_release", "restore_config", "rotate_logs"}


class HealthCheck(BaseModel):
    name: str = Field(max_length=200)
    ok: StrictBool
    detail: str = Field(default="", max_length=3000)


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    healthy: StrictBool
    reachable: StrictBool = False
    summary: str = Field(default="健康状态未知", max_length=3000)
    latency_ms: float | None = Field(default=None, ge=0)
    checks: list[HealthCheck] = Field(default_factory=list, max_length=30)
    metrics: dict[str, float | None] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list, max_length=100)
    facts: dict = Field(default_factory=dict)


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    connector: Literal["http", "agent"] = "http"
    target: str = Field(max_length=2048)
    interval_seconds: int = Field(default=15, ge=5, le=3600)
    failure_threshold: int = Field(default=2, ge=1, le=20)
    recovery_threshold: int = Field(default=2, ge=2, le=20)
    auto_actions: list[Action] = Field(default_factory=list)
    agent_service: str = Field(default="", max_length=80, pattern=r"^[a-zA-Z0-9_.-]*$")
    agent_token: str = Field(default="", max_length=4096)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("服务名称不能为空")
        return value.strip()

    @field_validator("target")
    @classmethod
    def valid_target(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("目标必须是完整的 HTTP 或 HTTPS URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("URL 不可包含凭据或片段")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("URL 端口无效") from exc
        return value.rstrip("/") if parsed.path in {"", "/"} else value

    @model_validator(mode="after")
    def valid_connector(self):
        if self.connector == "http" and self.auto_actions:
            raise ValueError("HTTP 监测不具备执行权限，请接入主机 Agent")
        if self.connector == "agent" and (not self.agent_service or not self.agent_token):
            raise ValueError("主机 Agent 需要服务标识和访问令牌")
        self.auto_actions = list(dict.fromkeys(self.auto_actions))
        return self


class ServicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    auto_actions: list[Action] | None = None


class FaultRequest(BaseModel):
    fault: Literal["bad_release", "bad_config", "process_exit", "log_pressure", "recover"]


class DismissRequest(BaseModel):
    reason: str = Field(default="操作员停止自动处置", min_length=1, max_length=500)
