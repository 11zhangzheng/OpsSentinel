from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Action = Literal["restart_service", "rollback_release", "restore_config", "rotate_logs"]
ACTIONS = {"restart_service", "rollback_release", "restore_config", "rotate_logs"}

TOOL_STATUSES = {"OK", "EMPTY_RESULT", "TOOL_ERROR", "TOOL_TIMEOUT", "INVALID_ARGUMENT", "PERMISSION_DENIED"}
ROOT_CAUSES = {"unknown", "bad_deployment", "dependency_timeout", "connection_exhaustion", "oom",
               "memory_pressure", "cpu_overload", "disk_full", "configuration_error",
               "probe_contract_mismatch", "port_conflict", "process_exit", "managed_log_pressure"}


class EvidenceRecord(BaseModel):
    """Trusted, bounded source record; updates must append rather than rewrite."""
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    evidence_id: str
    incident_id: str
    service_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    source: str
    observed_at: str
    collected_at: str
    status: str
    facts: list[dict] = Field(default_factory=list, max_length=64)
    excerpt: str | None = Field(default=None, max_length=2000)
    truncated: bool = False

    @model_validator(mode="after")
    def valid_record(self):
        import json
        if self.status not in TOOL_STATUSES:
            raise ValueError("Invalid source status")
        ids = set()
        for fact in self.facts:
            if set(fact) != {"fact_id", "locator", "kind", "value"} or fact["kind"] not in {"business", "policy", "historical", "operational"}:
                raise ValueError("Invalid evidence fact")
            if not isinstance(fact["fact_id"], str) or fact["fact_id"] in ids or not isinstance(fact["locator"], str):
                raise ValueError("Invalid or duplicate fact ID")
            ids.add(fact["fact_id"])
        if len(json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False).encode()) > 8192:
            raise ValueError("Evidence record exceeds 8 KiB")
        return self


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    root_cause_code: str
    root_cause: str = Field(min_length=1, max_length=1500)
    claims: list[dict] = Field(default_factory=list, max_length=8)
    hypotheses: list[str] = Field(default_factory=list, max_length=4)
    action: Action | None = None
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_claim_shape(self):
        if self.root_cause_code not in ROOT_CAUSES:
            raise ValueError("Unknown root cause code")
        for claim in self.claims:
            if set(claim) != {"kind", "claim", "citations"} or claim["kind"] not in {"observation", "root_cause", "action_support"}:
                raise ValueError("Invalid claim shape")
            if not isinstance(claim["claim"], str) or not claim["claim"].strip() or len(claim["claim"]) > 1500:
                raise ValueError("Invalid claim text")
            citations = claim["citations"]
            if not isinstance(citations, list) or len(citations) > 16:
                raise ValueError("Invalid citations")
            for citation in citations:
                if not isinstance(citation, dict) or set(citation) != {"evidence_id", "fact_id"} or not all(isinstance(v, str) for v in citation.values()):
                    raise ValueError("Invalid citation")
        if any(len(text) > 1500 for text in [*self.hypotheses, *self.limitations]):
            raise ValueError("Diagnostic text exceeds limit")
        return self


def validate_grounding(candidate: dict, records: list[dict], *, incident_id: str, run_id: str,
                       service_id: str, visible_fact_ids: set[str]) -> dict:
    """Validate structure/provenance only; this does not prove semantic entailment."""
    try:
        diagnosis = Diagnosis.model_validate(candidate)
    except (ValueError, TypeError):
        return {"status": "FAIL", "errors": ["INVALID_DIAGNOSIS_SCHEMA"]}
    evidence = {r["evidence_id"]: r for r in records}
    errors = []
    kinds = {c["kind"] for c in diagnosis.claims}
    if diagnosis.root_cause_code != "unknown" and "root_cause" not in kinds:
        errors.append("MISSING_ROOT_CAUSE_CLAIM")
    if diagnosis.action and "action_support" not in kinds:
        errors.append("MISSING_ACTION_SUPPORT")
    for claim in diagnosis.claims:
        if not claim["citations"]:
            errors.append("UNGROUNDED_CLAIM")
        business = False
        for citation in claim["citations"]:
            record = evidence.get(citation["evidence_id"])
            if record is None:
                errors.append("UNKNOWN_EVIDENCE_ID")
                continue
            if record["incident_id"] != incident_id or record["service_id"] != service_id:
                errors.append("CROSS_INCIDENT_REFERENCE")
            if record["run_id"] != run_id:
                errors.append("CROSS_RUN_REFERENCE")
            if record["status"] != "OK":
                errors.append("FAILED_SOURCE_REFERENCE")
            fact = next((f for f in record["facts"] if f["fact_id"] == citation["fact_id"]), None)
            if fact is None:
                errors.append("UNKNOWN_FACT_ID")
                continue
            if f"{record['evidence_id']}:{fact['fact_id']}" not in visible_fact_ids:
                errors.append("UNSEEN_REFERENCE")
            business |= fact["kind"] == "business"
            if fact["kind"] == "operational":
                errors.append("FAILED_SOURCE_REFERENCE")
        if claim["kind"] in {"root_cause", "action_support"} and not business:
            errors.append("UNGROUNDED_CLAIM")
    return {"status": "FAIL" if errors else "PASS", "errors": sorted(set(errors))}


class HttpProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    timeout_seconds: float = Field(default=5, ge=1, le=30)
    expected_status: int | None = Field(default=None, ge=100, le=599)
    body_contains: str = Field(default="", max_length=500)


class ResourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric: Literal["latency_ms", "cpu_percent", "memory_percent", "disk_percent"]
    above: float = Field(gt=0, le=60000)
    recover_below: float = Field(ge=0, le=60000)
    for_checks: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def ordered_thresholds(self):
        if self.recover_below >= self.above:
            raise ValueError("恢复阈值必须低于触发阈值")
        if self.metric != "latency_ms" and self.above > 100:
            raise ValueError("使用率阈值不能超过 100%")
        return self


def unique_rules(rules):
    if len({rule.metric for rule in rules}) != len(rules):
        raise ValueError("同一指标只能配置一条预警规则")
    return rules


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
    source_status: dict[str, str] = Field(default_factory=dict)

    @field_validator("source_status")
    @classmethod
    def valid_source_status(cls, value):
        if any(status not in TOOL_STATUSES for status in value.values()):
            raise ValueError("Invalid observation source status")
        return value


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
    http_probe: HttpProbe = Field(default_factory=HttpProbe)
    resource_rules: list[ResourceRule] = Field(default_factory=list, max_length=4)

    _unique_rules = field_validator("resource_rules")(unique_rules)

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
        if self.connector != "agent" and any(rule.metric != "latency_ms" for rule in self.resource_rules):
            raise ValueError("主机资源预警需要接入 Linux 主机 Agent")
        if self.connector != "http" and self.http_probe != HttpProbe():
            raise ValueError("HTTP 探测契约仅适用于 HTTP 连接器")
        self.auto_actions = list(dict.fromkeys(self.auto_actions))
        return self


class ServicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    auto_actions: list[Action] | None = None
    http_probe: HttpProbe | None = None
    resource_rules: list[ResourceRule] | None = Field(default=None, max_length=4)

    @field_validator("resource_rules")
    @classmethod
    def distinct_metrics(cls, value):
        return unique_rules(value) if value is not None else value


class MaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(ge=1, le=10080)
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError("请输入维护原因")
        return value.strip()


class AlertAcknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="已查看，将继续跟踪", min_length=1, max_length=500)


class FaultRequest(BaseModel):
    fault: Literal["bad_release", "bad_config", "process_exit", "log_pressure", "recover"]


class DismissRequest(BaseModel):
    reason: str = Field(default="操作员停止自动处置", min_length=1, max_length=500)
